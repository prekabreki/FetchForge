"""Startup ffmpeg capability probes and the report the UI shows (#47 / #99)."""
import asyncio
import logging
import re
import subprocess

from . import ytdlp

logger = logging.getLogger("fetchforge")


def _ffmpeg_filter_names() -> set:
    """Every filter name this ffmpeg build lists, from one `-filters` call."""
    try:
        out = subprocess.run(
            [ytdlp.get_ffmpeg(), "-hide_banner", "-filters"],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except Exception:
        return set()
    names = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            names.add(parts[1])
    return names


def _ffmpeg_has_filter(name: str) -> bool:
    """Whether this ffmpeg build provides a given filter."""
    return name in _ffmpeg_filter_names()


def _ffmpeg_has_encoder(name: str) -> bool:
    """Whether this ffmpeg build lists a given encoder (e.g. hevc_nvenc)."""
    try:
        out = subprocess.run(
            [ytdlp.get_ffmpeg(), "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except Exception:
        return False
    return any(line.split()[1:2] == [name] for line in out.splitlines() if line.strip())


# GPU-resident pixel-format conversion needs the scale_cuda filter (libnpp).
# The bundled Windows ffmpeg (gyan.dev) has it; Fedora/Nobara builds ship
# --disable-libnpp, so it's absent there even though hevc_nvenc encoding works.
#
# These are filled in by the one startup probe in `lifespan` (#99), never at
# import: computing them here used to spawn ffmpeg twice on every import of the
# server module. HAS_NVENC starts optimistic so a handler invoked outside
# `lifespan` (tests, embedders) behaves as it always did; the filters start
# pessimistically because their only consumers are encode-argument builders.
HAS_SCALE_CUDA = False
HAS_LIBPLACEBO = False
HAS_NVENC = True

# Verified at startup by _probe_nvenc_tune(); falls back to "hq" whenever the probe
# fails, whatever the reason (old ffmpeg build, driver rejection, or anything else).
_nvenc_tune: str = "uhq"
# Why the uhq probe landed where it did — one of the _TUNE_* constants below. Stored
# alongside _nvenc_tune so the cause can be surfaced later without re-probing.
_nvenc_tune_cause: str = "supported"

# Probe outcomes. "ffmpeg_too_old" and "driver_rejected" have the same effect (fall
# back to "hq") but completely different remedies, so they must not be conflated:
#   ffmpeg_too_old  — ffmpeg rejected the *option name*; the build predates the uhq
#                     NVENC tune (added in ffmpeg 7.1). The driver never saw it.
#   driver_rejected — ffmpeg knows -tune uhq; the driver/GPU refused the config.
#   probe_failed    — the probe failed for some other/unrecognised reason. Degrade to
#                     "hq" anyway: never leave "uhq" on a setup that just failed.
_TUNE_SUPPORTED = "supported"
_TUNE_FFMPEG_TOO_OLD = "ffmpeg_too_old"
_TUNE_DRIVER_REJECTED = "driver_rejected"
_TUNE_PROBE_FAILED = "probe_failed"
_TUNE_NVENC_MISSING = "nvenc_missing"


def _classify_tune_probe(stderr: str) -> str:
    """Classify the -tune uhq probe's stderr into one of the _TUNE_* outcomes. Pure —
    no I/O, no globals — so every branch is unit-testable without ffmpeg or a GPU.

    Order matters: an ffmpeg that predates the uhq tune rejects the *option name*
    ("Unable to parse option value" / "Error setting option tune") and the driver is
    never consulted, so that signature is checked first. Only once ffmpeg has accepted
    the option can "InitializeEncoder failed" / "invalid param" mean the driver said no.

    Anything else that still looks like a failed run is _TUNE_PROBE_FAILED, not
    supported — an unrecognised failure must never be read as a working uhq."""
    err = stderr or ""
    if "Unable to parse option value" in err or "Error setting option tune" in err:
        return _TUNE_FFMPEG_TOO_OLD
    if "InitializeEncoder failed" in err or "invalid param" in err:
        return _TUNE_DRIVER_REJECTED
    # Generic ffmpeg failure markers: the probe blew up in a way we don't have a
    # specific remedy for. Still a failure, so still degrade to "hq".
    generic_failures = (
        "Conversion failed",
        "Error while opening encoder",
        "Error initializing output stream",
        "Unknown encoder",
        "Cannot load nvcuda",
        "No NVENC capable devices found",
    )
    if any(marker in err for marker in generic_failures):
        return _TUNE_PROBE_FAILED
    return _TUNE_SUPPORTED


async def _probe_nvenc_tune():
    """Test whether hevc_nvenc accepts -tune uhq.
    Note: -bf at any value (even 0) is incompatible with uhq on all tested NVENC GPUs
    (RTX 3060, RTX 4080). uhq manages B-frames internally; passing -bf overrides this
    and causes InitializeEncoder failed: invalid param (8). The probe tests uhq without
    -bf, which is how it's actually used in encodes."""
    global _nvenc_tune, _nvenc_tune_cause
    if not HAS_NVENC:
        _nvenc_tune = "hq"
        _nvenc_tune_cause = _TUNE_NVENC_MISSING
        logger.info("hevc_nvenc is not available in this ffmpeg — video conversion "
                    "is disabled; download-only and audio extraction still work.")
        return
    proc = await asyncio.create_subprocess_exec(
        ytdlp.get_ffmpeg(), "-f", "lavfi", "-i", "nullsrc=s=1920x1080:d=0.1",
        "-vframes", "3",
        "-c:v", "hevc_nvenc",
        "-preset", "p4",
        "-tune", "uhq",
        "-rc", "vbr",
        "-cq", "26",
        "-b:v", "0",
        "-maxrate", "7M",
        "-bufsize", "14M",
        "-rc-lookahead", "16",
        "-f", "null", "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    err = stderr.decode(errors="replace")
    cause = _classify_tune_probe(err)
    # Belt and braces: a non-zero exit means the probe failed even if none of the
    # known markers appeared in stderr. Never report "supported" on a failed run.
    if cause == _TUNE_SUPPORTED and proc.returncode not in (0, None):
        cause = _TUNE_PROBE_FAILED

    _nvenc_tune_cause = cause
    if cause == _TUNE_SUPPORTED:
        _nvenc_tune = "uhq"
        logger.info("hevc_nvenc: 'uhq' supported")
    else:
        _nvenc_tune = "hq"
        if cause == _TUNE_FFMPEG_TOO_OLD:
            logger.info("hevc_nvenc: this ffmpeg build does not know the 'uhq' tune "
                        "(added in ffmpeg 7.1) — using 'hq' + AQ. Update ffmpeg to "
                        "enable it; your GPU driver is not the problem.")
        elif cause == _TUNE_DRIVER_REJECTED:
            logger.info("hevc_nvenc: 'uhq' not supported on this driver — using 'hq' + AQ. "
                        "Update the NVIDIA driver to enable it.")
        else:
            logger.info("hevc_nvenc: 'uhq' probe failed for an unrecognised reason — "
                        "using 'hq' + AQ")


async def _probe_ffmpeg_capabilities():
    """One startup probe for every ffmpeg capability the app branches on (#99).

    Runs from `lifespan`, never at import, so importing this module spawns no
    subprocess. Records whether hevc_nvenc is listed at all (if not, the uhq
    probe is skipped and video conversion is refused before any download), the
    uhq tune outcome, and the two optional filters."""
    global HAS_NVENC, HAS_SCALE_CUDA, HAS_LIBPLACEBO
    HAS_NVENC = await asyncio.to_thread(_ffmpeg_has_encoder, "hevc_nvenc")
    filters = await asyncio.to_thread(_ffmpeg_filter_names)
    HAS_SCALE_CUDA = "scale_cuda" in filters
    HAS_LIBPLACEBO = "libplacebo" in filters
    await _probe_nvenc_tune()


# ── Startup capability report (#47) ──────────────────────────────────────────
# The startup probes in _probe_ffmpeg_capabilities() each quietly select a lesser
# path when they fail: _probe_nvenc_tune() (the uhq archival tune), HAS_SCALE_CUDA
# (GPU-resident pixel-format conversion) and HAS_LIBPLACEBO (the ewa_lanczos
# scaler). None of that ever reached the UI, so an ffmpeg predating a feature
# could cost output quality for months without a word. The report below turns
# those *already computed* results into something the UI can show. No probe is
# re-run here.
#
# THE PROBES ARE THE ONLY GATE. The version string is descriptive — it explains a
# failure, it never decides one. Distro packages, git-master snapshots and vendor
# builds all version inconsistently, so a version comparison used as the real gate
# would fire on perfectly healthy installs. Probe for behaviour, report the
# version to explain it.

FFMPEG_VERSION_UNKNOWN = "unknown version"
FFMPEG_PATH_UNKNOWN = "unknown path"

_FFMPEG_VERSION_RE = re.compile(r"ffmpeg version\s+(\S+)", re.IGNORECASE)

# Cached at startup by _refresh_capability_report(), served by GET /capabilities.
_capability_report: dict = {}


def _parse_ffmpeg_version(output: str) -> str:
    """Pull the version/build token out of `ffmpeg -version` output. Pure.

    Covers the shapes seen in the wild:
        ffmpeg version 7.1.1-full_build-www.gyan.dev Copyright ...
        ffmpeg version 2023-10-29-git-2532e832d2-full_build-www.gyan.dev ...
        ffmpeg version n7.1 Copyright ...
        ffmpeg version 6.1.1 Copyright ...          (Fedora)

    Returns "" when the output is not recognisably ffmpeg. An unusual build must
    degrade to "unknown version" — never to a bogus one, and never to silence."""
    m = _FFMPEG_VERSION_RE.search(output or "")
    return m.group(1) if m else ""


def _read_ffmpeg_version() -> str:
    """The build string from `ffmpeg -version`, or "" if it cannot be read.

    This is the one extra subprocess the report is allowed (the encode probes are
    emphatically *not* re-run). Returns "" on any failure at all — missing binary,
    timeout, non-zero exit, unparseable output. The caller degrades to
    "unknown version" and still warns on whatever the probes found."""
    try:
        out = subprocess.run([ytdlp.get_ffmpeg(), "-hide_banner", "-version"],
                             capture_output=True, text=True, timeout=15)
    except Exception:
        return ""
    return _parse_ffmpeg_version((out.stdout or "") + "\n" + (out.stderr or ""))


def _uhq_capability(tune_cause: str, has_nvenc: bool = True) -> dict:
    """The uhq-tune entry, built from #44's already-resolved cause. Same symptom,
    different remedies: an ffmpeg that never knew the option and a driver that
    refused it must not be given the same advice.

    With no NVENC at all the tune probe never ran, so reporting the outcome as an
    "unrecognised" probe failure would be misleading — name the real blocker."""
    if not has_nvenc or tune_cause == _TUNE_NVENC_MISSING:
        return {
            "key": "nvenc_uhq_tune",
            "name": "Best-quality encode mode (uhq)",
            "ok": False,
            "cause": _TUNE_NVENC_MISSING,
            "impact": "Video conversion is unavailable.",
            "remedy": ("NVENC is unavailable, so this mode cannot run. Video "
                       "conversion needs an ffmpeg built with NVENC and an "
                       "NVIDIA GPU."),
        }
    if tune_cause == _TUNE_FFMPEG_TOO_OLD:
        remedy = ("Update ffmpeg. This mode arrived in ffmpeg 7.1. "
                  "Your GPU driver is fine.")
    elif tune_cause == _TUNE_DRIVER_REJECTED:
        remedy = ("Update your NVIDIA driver. This ffmpeg has the mode; "
                  "the driver turned it down.")
    elif tune_cause == _TUNE_SUPPORTED:
        remedy = ""
    else:
        remedy = ("Check the startup log for the hevc_nvenc probe output — the "
                  "test failed for a reason we do not recognise.")
    return {
        "key": "nvenc_uhq_tune",
        "name": "Best-quality encode mode (uhq)",
        "ok": tune_cause == _TUNE_SUPPORTED,
        "cause": tune_cause,
        "impact": "Encodes use the faster 'hq' mode instead — slightly lower "
                  "quality.",
        "remedy": remedy,
    }


def build_capability_report(*, ffmpeg_path: str, ffmpeg_version: str,
                            tune_cause: str, has_scale_cuda: bool,
                            has_libplacebo: bool, has_nvenc: bool = True) -> dict:
    """Compose the capability report and its warning. Pure — every input is
    injected, so the whole thing is unit-testable with no ffmpeg, no GPU and no
    subprocess.

    `warning` is None exactly when every probe passed. It is never keyed off
    *ffmpeg_version*: an empty version yields "unknown version" in the message and
    changes nothing about whether the warning exists."""
    caps = [
        {
            "key": "nvenc",
            "name": "NVENC video encoding (hevc_nvenc)",
            "ok": bool(has_nvenc),
            "cause": "supported" if has_nvenc else "encoder_missing",
            "impact": "Video conversion is unavailable — download-only and "
                      "audio extraction still work.",
            "remedy": "" if has_nvenc else
                      ("Install an ffmpeg built with NVENC (--enable-nvenc) on "
                       "a machine with an NVIDIA GPU."),
        },
        _uhq_capability(tune_cause, has_nvenc),
        {
            "key": "scale_cuda",
            "name": "Colour-format conversion on the GPU (scale_cuda)",
            "ok": bool(has_scale_cuda),
            "cause": "supported" if has_scale_cuda else "filter_missing",
            "impact": "That one step runs on the CPU instead — encodes are a "
                      "bit slower.",
            "remedy": "" if has_scale_cuda else
                      ("Only ffmpeg builds made with --enable-libnpp have it. "
                       "The Windows gyan.dev and BtbN builds do; most Linux "
                       "packages do not."),
        },
        {
            "key": "libplacebo",
            "name": "Best-quality resizing (libplacebo)",
            "ok": bool(has_libplacebo),
            "cause": "supported" if has_libplacebo else "filter_missing",
            "impact": "Resizing uses a simpler, slightly softer scaler.",
            "remedy": "" if has_libplacebo else
                      "Only ffmpeg builds made with --enable-libplacebo have it.",
        },
    ]

    path = ffmpeg_path or FFMPEG_PATH_UNKNOWN
    version = ffmpeg_version or FFMPEG_VERSION_UNKNOWN
    # Without NVENC, no video encode can run at all: lead with that fact and keep
    # the optional-filter remedies out of a warning that cannot apply yet.
    failing = [c for c in caps if not c["ok"]] if has_nvenc else \
              [c for c in caps if c["key"] == "nvenc"]

    warning = None
    if failing:
        if not has_nvenc:
            headline = ("Video conversion is unavailable — this ffmpeg has no "
                        "NVENC encoder")
        else:
            n = len(failing)
            noun = "feature" if n == 1 else "features"
            fallback = "a slower fallback" if n == 1 else "slower fallbacks"
            headline = "ffmpeg is missing {} {} — FetchForge is using {}".format(
                n, noun, fallback)
        # The path is the headline fact: the whole confusion this warning exists
        # to end was not knowing *which* ffmpeg was in use.
        text = "{} | ffmpeg: {} ({}) | {}".format(
            headline, path, version,
            " ".join("{} — {}".format(c["name"], c["remedy"]) for c in failing))
        warning = {
            "headline": headline,
            "ffmpeg_path": path,
            "ffmpeg_version": version,
            "items": [{"name": c["name"], "impact": c["impact"],
                       "remedy": c["remedy"]} for c in failing],
            "text": text,
        }
        # Every optional capability is an EXTRA on top of NVENC encoding. Read
        # without this line the banner is easily taken to mean the GPU is not
        # encoding at all — the exact misreading it caused in practice. With no
        # NVENC encoder, that reassurance would simply be false.
        if has_nvenc:
            warning["reassurance"] = ("GPU encoding itself is working — "
                                      "these are extras on top of it.")

    return {
        "ffmpeg_path": path,
        "ffmpeg_version": version,
        "capabilities": caps,
        "warning": warning,
    }


def _refresh_capability_report() -> dict:
    """Build the report from results already computed at startup, cache it, return it.

    DANGER ZONE: called from `lifespan`. A diagnostic must never stop the server
    booting, so every step that touches the outside world is individually wrapped
    and the whole body is wrapped again. Worst case is a report that says it is
    unavailable, next to a server that started fine.

    Note the version read is caught *separately* from the report build: a version
    that cannot be read degrades to "unknown version" and must never suppress an
    otherwise-valid probe warning."""
    global _capability_report
    try:
        try:
            path = ytdlp.get_ffmpeg()
        except Exception:
            path = ""
        try:
            version = _read_ffmpeg_version()
        except Exception:
            version = ""
        report = build_capability_report(
            ffmpeg_path=str(path or ""),
            ffmpeg_version=version,
            tune_cause=_nvenc_tune_cause,
            has_nvenc=HAS_NVENC,
            has_scale_cuda=HAS_SCALE_CUDA,
            has_libplacebo=HAS_LIBPLACEBO,
        )
        if report.get("warning"):
            logger.warning(report["warning"]["text"])
        else:
            logger.info("ffmpeg capabilities: all probes passed ({})".format(
                report.get("ffmpeg_version")))
        _capability_report = report
    except Exception as e:
        logger.warning("capability report unavailable: {}".format(e))
        _capability_report = {
            "ffmpeg_path": FFMPEG_PATH_UNKNOWN,
            "ffmpeg_version": FFMPEG_VERSION_UNKNOWN,
            "capabilities": [],
            "warning": None,
            "error": "capability report unavailable",
        }
    return _capability_report


def get_capability_report() -> dict:
    """The cached report (empty until the startup refresh has run)."""
    return _capability_report
