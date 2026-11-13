"""Encode planning and execution: override validation, NVENC parameters, ffmpeg
argv builders and the single encode loop."""
import asyncio
import re
from pathlib import Path
from typing import Optional

from . import capabilities, events, jobs, sections, ytdlp

_CUVID_DECODERS = {
    "vp9":  "vp9_cuvid",
    "h264": "h264_cuvid",
    "hevc": "hevc_cuvid",
    "av1":  "av1_cuvid",
    "vp8":  "vp8_cuvid",
}


def _effective_target_height(source_height: int, target_height: int) -> int:
    """A requested target equal to the source height is a same-size resample --
    essentially a no-op scale (#35). Zero it so callers route _decode_filter_args
    to the cheaper passthrough+sharpen path instead of a full same-size scaler
    (e.g. libplacebo=w=-2:h=1080 when the source is already 1080p). Sharpen is
    unaffected -- it's carried by _decode_filter_args's own *sharpen* flag."""
    if target_height > 0 and source_height > 0 and target_height == source_height:
        return 0
    return target_height


def _decode_filter_args(input_file: Path, pix_fmt: str, cuvid: Optional[str],
                        target_height: int = 0, sharpen: bool = False) -> list:
    """Decode + pixel-format (+ optional scale) args for hevc_nvenc encode.

    When *target_height* > 0 the filter chain includes a scale to that height
    (even-dimension-safe, aspect-ratio-preserving).  Scaler engine selection:
    libplacebo (ewa_lanczos via Vulkan) → scale_cuda (NPP lanczos) → CPU
    (scale=…:flags=lanczos).  target_height=0 produces byte-identical output
    to the original two-branch logic.

    When *sharpen* is True a gentle Contrast Adaptive Sharpen (cas) filter is
    appended to -vf after any scaling, with an explicit hwdownload for GPU
    filter paths."""
    CAS_STRENGTH = "0.72"
    cuvid_args = ["-c:v", cuvid] if cuvid else []
    if target_height > 0:
        if capabilities.HAS_LIBPLACEBO:
            vf = (f"libplacebo=w=-2:h={target_height}"
                  f":upscaler=ewa_lanczos:downscaler=ewa_lanczos"
                  f":format={pix_fmt}:tonemapping=none")
            if sharpen:
                vf += f",cas={CAS_STRENGTH}"
            return [
                "-hwaccel", "cuda",
                *cuvid_args,
                "-i", str(input_file),
                "-vf", vf,
            ]
        if capabilities.HAS_SCALE_CUDA:
            vf = f"scale_cuda=format={pix_fmt}:w=-2:h={target_height}"
            if sharpen:
                vf += f",hwdownload,format={pix_fmt},cas={CAS_STRENGTH}"
            return [
                "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
                *cuvid_args,
                "-i", str(input_file),
                "-vf", vf,
            ]
        vf = f"scale=w=-2:h={target_height}:flags=lanczos,format={pix_fmt}"
        if sharpen:
            vf += f",cas={CAS_STRENGTH}"
        return [
            "-hwaccel", "cuda",
            *cuvid_args,
            "-i", str(input_file),
            "-vf", vf,
        ]
    # Passthrough — byte-identical to original two-branch logic.
    if capabilities.HAS_SCALE_CUDA:
        vf = f"scale_cuda=format={pix_fmt}"
        if sharpen:
            vf += f",hwdownload,format={pix_fmt},cas={CAS_STRENGTH}"
        return [
            "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
            *cuvid_args,
            "-i", str(input_file),
            "-vf", vf,
        ]
    vf = f"format={pix_fmt}"
    if sharpen:
        vf += f",cas={CAS_STRENGTH}"
    return [
        "-hwaccel", "cuda",
        "-i", str(input_file),
        "-vf", vf,
    ]


def audio_output_ext(preset: str) -> str:
    return "wav" if preset == "wav" else "mp3"


def build_audio_ffmpeg(input_file: Path, output_file: Path, preset: str,
                       input_opts: list = ()) -> list:
    """ffmpeg args for audio extraction. -vn drops video; -progress pipe:1
    matches the video encode flow so the UI parser stays unified. *input_opts*
    (e.g. section_input_seek) go immediately before -i."""
    if preset == "wav":
        codec_args = ["-c:a", "pcm_s16le"]
    else:
        codec_args = ["-c:a", "libmp3lame", "-b:a", "320k"]
    return sections._with_input_options([
        ytdlp.get_ffmpeg(), "-y",
        "-loglevel", "info",
        "-i", str(input_file),
        "-vn",
        *codec_args,
        "-progress", "pipe:1",
        "-nostats",
        str(output_file),
    ], list(input_opts))


# ── User-supplied encode overrides (#50) ──────────────────────────────────────
# Every value below arrives as text from a form field and ends up in a subprocess
# argv, so validation lives in exactly ONE place: validate_encode_overrides().
# Anything unparseable, out of range or structurally wrong falls back to auto — an
# override must never produce an argv NVENC rejects at encoder init, because that
# happens AFTER the download has finished and costs the user the whole job.
_OVERRIDE_CQ_RANGE = (16, 34)        # -cq; -b:v must stay 0 (NVENC rejects the pair)
_OVERRIDE_MAXRATE_RANGE = (1, 25)    # Mbps, before the per-resolution clamp
_OVERRIDE_PRESETS = ("p1", "p2", "p3", "p4", "p5", "p6", "p7")
# ll / ull / lossless are deliberately absent: the latency tunes wreck archival
# quality and lossless conflicts with the -cq VBR model this encoder uses.
_OVERRIDE_TUNES = ("hq", "uhq")
_OVERRIDE_PIX_FMTS = ("yuv420p", "yuv420p10le")

_OVERRIDE_INT_RE = re.compile(r"-?\d+")
_OVERRIDE_MBPS_RE = re.compile(r"(-?\d+)[Mm]?")


def _res_ceiling_mbps(height: int, fps: float) -> int:
    """Absolute maxrate ceiling for an OUTPUT resolution + framerate, in Mbps.
    4K stays at 20M whatever the framerate — 28M causes
    'InitializeEncoder failed: invalid param (8)'. Single source of truth, shared
    by the auto path and the override clamp so the two cannot drift apart."""
    res_ceil = next(
        (v for k, v in [(2160, 20), (1440, 12), (1080, 7), (720, 4)] if height >= k), 4
    )
    if fps > 35 and height < 2160:  # 60fps headroom for sub-4K only; 4K stays at 20M
        res_ceil = round(res_ceil * 1.40)
    return res_ceil


def _override_requested(raw) -> bool:
    """Did the user actually ask for something? "" / "0" / None / 0 mean "auto"."""
    if raw is None:
        return False
    if isinstance(raw, str):
        return raw.strip() not in ("", "0")
    if isinstance(raw, bool):
        return True          # a bool is nobody's encode setting — report it, reject it
    if isinstance(raw, (int, float)):
        return raw != 0
    return True              # lists, objects, …: junk, but junk that was passed in


def _override_int(raw) -> Optional[int]:
    """Strict int coercion for form text. Rejects None, bools, floats, unit
    suffixes and internally-spaced junk — anything ambiguous becomes None."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if not isinstance(raw, str):
        return None          # floats included: 22.5 is not a CQ, do not truncate it
    text = raw.strip()
    return int(text) if _OVERRIDE_INT_RE.fullmatch(text) else None


def _override_mbps(raw) -> Optional[int]:
    """As _override_int, but tolerating the "20M"/"20m" suffix the UI sends.
    "20 M", "7.5M" and "1e3M" are junk and become None."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if not isinstance(raw, str):
        return None
    m = _OVERRIDE_MBPS_RE.fullmatch(raw.strip())
    return int(m.group(1)) if m else None


def _override_word(raw) -> str:
    """Normalise an enum-ish override to a bare lowercase token, or "" if it is
    not one. Only whole tokens survive, so nothing can smuggle whitespace or a
    separator into an argv."""
    return raw.strip().lower() if isinstance(raw, str) else ""


def _override_text(raw) -> str:
    """A short, safe rendering of a rejected value for the report."""
    return (raw if isinstance(raw, str) else repr(raw)).strip()[:32]


def validate_encode_overrides(
    height: int,
    fps: float,
    cq_override=0,
    maxrate_override="",
    preset_override="",
    tune_override="",
    pix_fmt_override="",
    auto_tune: str = "uhq",
    probe_tune: str = "uhq",
) -> dict:
    """Coerce, clamp and (on failure) discard user-supplied encode overrides.

    Pure: no globals, no I/O. The NVENC capability probe result is *injected* as
    ``probe_tune`` (call sites pass the module-level ``_nvenc_tune``) rather than
    read here, so every branch is unit-testable without ffmpeg or a GPU.

    ``height``/``fps`` are the OUTPUT resolution and framerate (i.e. after
    ``target_res`` has been applied), so the maxrate ceiling matches what the
    encoder will actually be asked to produce.

    Returns ``{"cq", "maxrate_mbps", "preset", "pix_fmt", "tune", "report"}``
    where ``0`` / ``""`` mean "no override, use the auto path" and ``tune`` is
    always resolved. ``report`` maps each *requested* override to
    ``{requested, applied, status}`` with status one of
    applied / clamped / downgraded / rejected — a silently rewritten value
    (25M → 20M on a 4K source) must stay distinguishable from one that was
    ignored, so the UI can eventually say which happened.
    """
    report: dict = {}

    def note(name, requested, applied, status):
        report[name] = {"requested": requested, "applied": applied, "status": status}

    # ── cq: 0 = auto, else 16–34 ───────────────────────────────────────────
    cq = 0
    if _override_requested(cq_override):
        parsed = _override_int(cq_override)
        lo, hi = _OVERRIDE_CQ_RANGE
        if parsed is not None and lo <= parsed <= hi:
            cq = parsed
            note("cq", str(parsed), str(cq), "applied")
        else:
            note("cq", _override_text(cq_override), "auto", "rejected")

    # ── maxrate: ""/"0" = auto, else 1M–25M clamped to the per-res ceiling ──
    maxrate_mbps = 0
    if _override_requested(maxrate_override):
        parsed = _override_mbps(maxrate_override)
        lo, hi = _OVERRIDE_MAXRATE_RANGE
        if parsed is not None and lo <= parsed <= hi:
            maxrate_mbps = min(parsed, _res_ceiling_mbps(height, fps))
            note("maxrate", "{}M".format(parsed), "{}M".format(maxrate_mbps),
                 "applied" if maxrate_mbps == parsed else "clamped")
        else:
            note("maxrate", _override_text(maxrate_override), "auto", "rejected")

    def enum_override(name, raw, allowed):
        """"" if not requested or not one of *allowed*, else the bare token."""
        if not _override_requested(raw):
            return ""
        word = _override_word(raw)
        if word in allowed:
            note(name, word, word, "applied")
            return word
        note(name, _override_text(raw), "auto", "rejected")
        return ""

    # ── preset: "" = auto, else p1–p7 ──────────────────────────────────────
    preset = enum_override("preset", preset_override, _OVERRIDE_PRESETS)

    # ── pix_fmt: "" = auto, else yuv420p | yuv420p10le ─────────────────────
    pix_out = enum_override("pix_fmt", pix_fmt_override, _OVERRIDE_PIX_FMTS)

    # ── tune: "" = auto, else hq | uhq, downgraded if the probe says so ────
    tune = enum_override("tune", tune_override, _OVERRIDE_TUNES) or auto_tune
    if tune == "uhq" and "tune" in report and probe_tune != "uhq":
        # The startup probe already decided uhq is unavailable; emitting it anyway
        # fails at encoder init. Read the probe result, never re-run the probe.
        # Only an *explicit* override is downgraded — an auto tune is the caller's
        # business and already reflects the probe.
        tune = probe_tune if probe_tune in _OVERRIDE_TUNES else "hq"
        report["tune"].update(applied=tune, status="downgraded")

    return {
        "cq": cq,
        "maxrate_mbps": maxrate_mbps,
        "preset": preset,
        "pix_fmt": pix_out,
        "tune": tune,
        "report": report,
    }


def calc_encode_params(
    height: int,
    fps: float,
    video_bitrate_kbps: int,
    codec: str,
    pix_fmt: str,
    color_transfer: str,
    cq_override: int = 0,
    maxrate_override: str = "",
    nvenc_tune: str = "uhq",
    target_res: int = 0,
    sharpen: bool = False,
    preset_override: str = "",
    tune_override: str = "",
    pix_fmt_override: str = "",
    probe_tune: str = "uhq",
) -> dict:
    """
    Derive NVENC encode parameters from source analysis.
    cq_override=0  → auto-select by resolution.
    maxrate_override="" → auto-calculate from source bitrate + codec efficiency.
    preset_override/tune_override/pix_fmt_override="" → auto (see the matrix in #50).
    probe_tune  → the startup NVENC probe result (_nvenc_tune); an explicit "uhq"
                  override is downgraded to it when uhq is unsupported.
    sharpen=True or upscaling enforces a quality floor (CQ≤24, maxrate≥3M, preset≥p4).

    With every override empty this is byte-identical to the pre-#50 behaviour;
    tests/test_encode_overrides.py pins that against a frozen implementation.
    """
    source_height = height
    if target_res and target_res > 0:
        height = target_res

    # Every user-supplied value is coerced/clamped here, before any of them is
    # used. Nothing below this line touches a raw override (#50).
    ov = validate_encode_overrides(
        height=height, fps=fps,
        cq_override=cq_override, maxrate_override=maxrate_override,
        preset_override=preset_override, tune_override=tune_override,
        pix_fmt_override=pix_fmt_override,
        auto_tune=nvenc_tune, probe_tune=probe_tune,
    )
    cq_override = ov["cq"]                       # int, 0 = auto
    nvenc_tune = ov["tune"]                      # explicit override wins, post-downgrade
    has_maxrate_override = bool(ov["maxrate_mbps"])

    # Quality target: lower = better quality / larger file
    if cq_override > 0:
        cq = cq_override
    elif height >= 2160:
        cq = 22
    elif height >= 1440:
        cq = 24
    elif height >= 1080:
        cq = 26
    else:
        cq = 28

    # HQ mode gets a 2-point quality boost (source bitrate cap prevents file bloat).
    if nvenc_tune == "hq" and cq_override == 0:
        cq = max(cq - 2, 16)

    # How much smaller H.265 NVENC should be vs the source codec at equal perceptual quality.
    # Fast motion (game footage) narrows the gap — temporal prediction helps both codecs equally.
    efficiency = {
        "h264":  0.55,   # H.265 ≈ 45% smaller than H.264
        "vp9":   0.80,   # H.265 ≈ 20% smaller than VP9 (closer parity, especially at 60fps)
        "av1":   0.92,   # AV1 and H.265 are nearly equivalent
        "hevc":  0.88,   # Re-encoding H.265 → diminishing returns
    }.get(codec.lower(), 0.70)

    # Absolute resolution+fps ceiling (hard cap regardless of source)
    res_ceil = _res_ceiling_mbps(height, fps)

    # Per-resolution minimum — ensures CQ always has enough headroom to hit its target
    res_min = next(
        (v for k, v in [(2160, 8), (1440, 5), (1080, 3), (720, 2)] if height >= k), 2
    )

    # Source-derived average bitrate target and bloom-capped ceiling.
    # Using a real b_v average + maxrate ceiling lets NVENC vary around the average
    # and burst above it for complex frames — proper VBR instead of de-facto CBR.
    derived_mbps = round(video_bitrate_kbps * efficiency / 1000, 1) if video_bitrate_kbps > 0 else None

    if has_maxrate_override:
        # User-specified ceiling: pure CQ mode, no average target. Already coerced
        # and clamped to res_ceil by validate_encode_overrides.
        maxrate_mbps = ov["maxrate_mbps"]
    elif derived_mbps:
        # Burst headroom keyed off derived (expected output) bitrate, not source.
        # source * 1.875 was too generous for H.264 (0.55 efficiency) — it allowed
        # burst headroom of 3.4× the expected output, causing H.264 sources to produce
        # outputs larger than the source. 2× derived gives reasonable burst room while
        # keeping average bitrate close to the expected output.
        bloom_cap_mbps = max(round(derived_mbps) + 1, round(derived_mbps * 2))
        maxrate_mbps = min(bloom_cap_mbps, res_ceil)
    else:
        # Bitrate unknown: fall back to half the resolution ceiling
        maxrate_mbps = max(res_min, res_ceil // 2)

    # HQ mode: cap at source bitrate — re-encoding a lossy source can't recover quality
    # beyond what the source contains, so exceeding source bitrate only bloats the file.
    # An explicit maxrate still opts out of this cap — only the ceiling clamp is new.
    if nvenc_tune == "hq" and video_bitrate_kbps > 0 and not has_maxrate_override:
        source_cap_mbps = video_bitrate_kbps / 1000
        maxrate_mbps = max(min(maxrate_mbps, source_cap_mbps), res_min)

    # HDR / 10-bit detection
    hdr_transfers = {"smpte2084", "arib-std-b67", "smpte428", "bt2020-10", "bt2020-12"}
    hdr = color_transfer in hdr_transfers
    ten_bit = "10le" in pix_fmt or "10be" in pix_fmt or hdr
    pix_out = "yuv420p10le" if ten_bit else "yuv420p"

    if ov["pix_fmt"]:
        # profile follows pix_fmt so the pair can never be mismatched.
        pix_out = ov["pix_fmt"]
        ten_bit = pix_out.endswith("10le")
        if hdr and not ten_bit:
            # Permitted — 8-bit output from an HDR source is a real choice — but
            # recorded so the UI can warn about the loss. "hdr" stays True: it is
            # a fact about the source, not about the output.
            ov["report"]["pix_fmt"]["warning"] = "hdr_source_forced_8bit"

    if nvenc_tune == "hq":
        preset = "p2" if fps > 35 else "p4"
    elif fps > 35:
        preset = "p2" if height < 2160 else "p3"
    else:
        preset = "p4"

    if ov["preset"]:
        preset = ov["preset"]
        if preset == "p2" and nvenc_tune == "uhq":
            # uhq rejects p2. build_video_ffmpeg_args enforces the same rule as a
            # backstop for the auto path; resolving it here keeps the *reported*
            # preset honest about what will actually be encoded.
            preset = "p4"
            ov["report"]["preset"].update(applied="p4", status="clamped")

    is_upscale = target_res > 0 and target_res > source_height
    if sharpen or is_upscale:
        cq = min(cq, 24)
        maxrate_mbps = max(maxrate_mbps, 3)
        if preset == "p2":
            preset = "p4"

    # Final ceiling guard, applied AFTER the sharpen/upscale floor so the two can
    # never disagree about the same value. A no-op for every auto path (all of
    # them already sit at or below res_ceil, and the 3M floor is below the 4M
    # smallest ceiling) — it exists so no ordering change can leak an over-ceiling
    # maxrate into the argv (#50).
    if maxrate_mbps > res_ceil:
        maxrate_mbps = res_ceil
        if "maxrate" in ov["report"]:
            ov["report"]["maxrate"].update(applied="{}M".format(res_ceil), status="clamped")

    return {
        "cq": cq,
        "preset": preset,
        "maxrate": f"{maxrate_mbps}M",
        "bufsize": f"{maxrate_mbps * 2}M",
        "pix_fmt": pix_out,
        "profile": "main10" if ten_bit else "main",
        "hdr": hdr,
        "ten_bit": ten_bit,
        "bf": 3,
        "b_ref_mode": "middle",
        # The tune the encode will actually use, after override + probe downgrade.
        "tune": nvenc_tune,
        # {name: {requested, applied, status}} for each override the user asked
        # for; empty when none were. Lets the UI show 25M → 20M instead of
        # silently appearing to ignore the setting.
        "overrides": ov["report"],
    }


# Cancel flag for a bare `run_encode()` call. Real jobs pass their Job instead:
# the Job owns the cancel event (/cancel sets `job.cancel`) and the subprocess
# handles, so this global is only the default when run_encode runs standalone.
_cancel_requested = asyncio.Event()


def build_video_ffmpeg_args(input_file: Path, output_file: Path, params: dict,
                            effective_tune: str, codec: str,
                            target_height: int = 0, sharpen: bool = False,
                            input_opts: list = ()) -> list:
    """Construct the hevc_nvenc ffmpeg argv. Pure (no I/O beyond the module-level
    capability probes) so it can be unit-tested with a fixture params dict. Single
    source of truth for the three former copies (H-2).

    *target_height* is forwarded to _decode_filter_args for optional scaling.
    When 0 (default) the filter chain is byte-identical to the original.
    *sharpen* appends a gentle CAS filter to the -vf chain.
    *input_opts* (e.g. section_input_seek) go immediately before -i.

    params["tune"] (set by calc_encode_params, absent from hand-built fixtures)
    wins over *effective_tune*, so a validated user tune override cannot be lost
    between calc_encode_params and the argv (#50)."""
    effective_tune = params.get("tune") or effective_tune
    # uhq requires p4+ minimum; p2 rejected — guard only needed for the uhq path.
    effective_preset = "p4" if (effective_tune == "uhq" and params["preset"] == "p2") else params["preset"]
    cuvid = _CUVID_DECODERS.get((codec or "").lower())
    return sections._with_input_options([
        ytdlp.get_ffmpeg(), "-y",
        "-loglevel", "info",
        *_decode_filter_args(input_file, params["pix_fmt"], cuvid, target_height, sharpen),
        "-c:v", "hevc_nvenc",
        # mp4 muxer defaults HEVC to hev1, which Apple players refuse (#129).
        "-tag:v", "hvc1",
        "-preset", effective_preset,
        "-profile:v", params["profile"],
        "-tune", effective_tune,
        "-rc", "vbr",
        "-cq", str(params["cq"]),
        "-b:v", "0",
        "-maxrate", params["maxrate"],
        "-bufsize", params["bufsize"],
        *(["-bf", str(params["bf"]), "-b_ref_mode", params["b_ref_mode"]] if effective_tune == "hq" else []),
        "-rc-lookahead", "16",
        # uhq enables AQ internally; explicit flags cause invalid param on SDK 12+.
        *(["-spatial-aq", "1", "-aq-strength", "8", "-temporal-aq", "1"] if effective_tune != "uhq" else []),
        "-c:a", "aac",
        "-b:a", "192k",
        "-progress", "pipe:1",
        "-nostats",
        str(output_file),
    ], list(input_opts))


def _encode_plan(src: dict, input_file: Path, output_file: Path, opts: dict,
                 input_opts=()) -> tuple:
    """Build the encode params, ffmpeg argv, and log lines for one video job.

    Single source of truth for the three former copies (H-2). *opts* carries the
    request-level (or per-item) knobs: ``tune_mode``, ``cq``, ``maxrate``,
    ``preset``, ``tune``, ``pix_fmt``, ``target_res``, ``sharpen``. *input_opts*
    (e.g. section_input_seek) is forwarded to build_video_ffmpeg_args.

    Returns ``(params, argv, logs)`` where *logs* is the "Encode params: ..."
    line plus one "Override ..." line per non-applied override, in order. The
    helper never emits SSE; callers yield / queue the strings themselves.
    """
    tune_mode = opts.get("tune_mode", "")
    target_res = opts.get("target_res", 0)
    sharpen = opts.get("sharpen", False)
    effective_tune = "hq" if tune_mode == "hq" else capabilities._nvenc_tune
    params = calc_encode_params(
        height=src.get("height", 1080),
        fps=src.get("fps", 30.0),
        video_bitrate_kbps=src.get("video_bitrate_kbps", 0),
        codec=src.get("codec", "h264"),
        pix_fmt=src.get("pix_fmt", "yuv420p"),
        color_transfer=src.get("color_transfer", ""),
        cq_override=opts.get("cq", 0),
        maxrate_override=opts.get("maxrate", ""),
        nvenc_tune=effective_tune,
        target_res=target_res,
        sharpen=sharpen,
        preset_override=opts.get("preset", ""),
        tune_override=opts.get("tune", ""),
        pix_fmt_override=opts.get("pix_fmt", ""),
        probe_tune=capabilities._nvenc_tune,
    )
    effective_tune = params["tune"]   # post-validation/downgrade
    logs = ["Encode params: CQ={} maxrate={} preset={} tune={} pix={}".format(
        params["cq"], params["maxrate"], params["preset"], effective_tune, params["pix_fmt"]
    )]
    for _name, _rep in params["overrides"].items():
        if _rep["status"] != "applied":
            logs.append("Override {}={} {} → {}".format(
                _name, _rep["requested"], _rep["status"], _rep["applied"]))
    argv = build_video_ffmpeg_args(
        input_file, output_file, params, effective_tune, src.get("codec", ""),
        target_height=_effective_target_height(src.get("height", 1080), target_res),
        sharpen=sharpen,
        input_opts=input_opts,
    )
    return params, argv, logs


async def run_encode(ffmpeg_args: list, *, duration_secs, idx: int, total: int,
                     result: dict, job: Optional["jobs.Job"] = None):
    """Run one ffmpeg encode/extract, yielding SSE strings for progress + log.

    Drains stdout (the -progress key=value stream) and stderr (ffmpeg's human
    log) CONCURRENTLY via a single queue, so stderr lines reach the UI within
    ~100ms even when no stdout arrives during a long codec init (H-4) — no more
    O(n) list.pop(0) gated on stdout. When `job` is given, its handle slot makes
    /cancel able to terminate the process and its cancel event stops it; a bare
    call (tests, ad-hoc reuse) falls back to the module-level flag. The ffmpeg
    return code is written to result["returncode"]; this is the single encode
    loop the three call sites share (H-2)."""
    cancel = job.cancel if job is not None else _cancel_requested
    emit = job.emit if job is not None else events._sse
    proc = await asyncio.create_subprocess_exec(
        *ffmpeg_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if job is not None:
        job.ffmpeg = proc

    q: asyncio.Queue = asyncio.Queue()

    async def _pump(stream, tag):
        async for raw in stream:
            await q.put((tag, raw))
        await q.put((tag + "_eof", None))

    t_out = asyncio.create_task(_pump(proc.stdout, "out"))
    t_err = asyncio.create_task(_pump(proc.stderr, "err"))

    progress_buf: dict = {}
    eofs = 0
    terminated = False
    try:
        while eofs < 2:
            tag, raw = await q.get()
            if tag in ("out_eof", "err_eof"):
                eofs += 1
                continue
            if cancel.is_set() and not terminated:
                proc.terminate()
                terminated = True
                # keep draining both pipes to EOF so the pump tasks finish cleanly
                continue
            line = raw.decode(errors="replace").rstrip()
            if tag == "err":
                if line:
                    yield emit(events._ev_log(line))
                continue
            # tag == "out": ffmpeg -progress key=value lines
            if "=" in line:
                k, _, v = line.partition("=")
                progress_buf[k.strip()] = v.strip()
                if k.strip() == "progress":
                    try:
                        out_time_us = int(progress_buf.get("out_time_us", 0) or 0)
                    except (ValueError, TypeError):
                        out_time_us = 0
                    current_secs = out_time_us / 1_000_000
                    speed_str = progress_buf.get("speed", "?")
                    fps_str = progress_buf.get("fps", "?")
                    if duration_secs and duration_secs > 0:
                        file_pct = min(100, (current_secs / duration_secs) * 100)
                        overall = ((idx - 1) / total * 100) + (file_pct / total)
                    else:
                        file_pct = 0.0
                        overall = ((idx - 1) / total * 100)
                    live_bytes = int(progress_buf.get("total_size", 0) or 0)
                    live_size = "{:.0f} MB".format(live_bytes / 1024**2) if live_bytes > 0 else ""
                    yield emit(events._ev_progress(file_pct, overall, speed_str, fps_str + " fps", live_size, live_bytes))
                    progress_buf = {}
            elif line:
                yield emit(events._ev_log(line))
        await t_out
        await t_err
        await proc.wait()
    finally:
        jobs._terminate_if_running(proc)
        if job is not None and job.ffmpeg is proc:
            job.ffmpeg = None
        result["returncode"] = proc.returncode
