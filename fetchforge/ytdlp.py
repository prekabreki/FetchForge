"""yt-dlp integration: tool resolution, argv pieces, format selectors, output
detection, download completeness and format listing."""
import asyncio
import functools
import importlib.util
import json
import logging
import os
import re
import shutil
import site
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from . import config, cookies, sections

IS_WINDOWS = sys.platform.startswith("win")

logger = logging.getLogger("fetchforge")


def _resolve_tool(name: str, win_bundled: Path) -> str:
    """Locate an external binary.

    Windows keeps the bundled `_internal/*.exe` / `yt-dlp.exe` it always shipped.
    Linux/macOS resolve from PATH (system package or the project .venv), matching
    the repo's "binaries are per-machine, do not track" convention.
    """
    if IS_WINDOWS and win_bundled.exists():
        return str(win_bundled)
    found = shutil.which(name)
    if found:
        return found
    # Console scripts installed into this interpreter's venv (e.g. yt-dlp) live
    # in sys.prefix/bin and resolve even when the venv isn't on PATH.
    bindir = "Scripts" if IS_WINDOWS else "bin"
    for exe in (name, name + ".exe"):
        cand = Path(sys.prefix) / bindir / exe
        if cand.exists():
            return str(cand)
    if win_bundled.exists():        # last resort (e.g. a manually-dropped binary)
        return str(win_bundled)
    raise RuntimeError(
        f"Required tool '{name}' not found on PATH. "
        f"On Linux: activate the project .venv (pip installs yt-dlp) and install ffmpeg."
    )


@functools.cache
def get_ffmpeg() -> str:
    return _resolve_tool("ffmpeg", config.PKG_DIR / "_internal" / "ffmpeg.exe")


@functools.cache
def get_ffprobe() -> str:
    return _resolve_tool("ffprobe", config.PKG_DIR / "_internal" / "ffprobe.exe")


@functools.cache
def get_ytdlp_argv() -> list:
    """argv prefix that runs yt-dlp — this interpreter's own copy when it has
    one, else a console script found on PATH.

    yt-dlp is a pip dependency, but where pip puts its console script depends on
    the install shape: a venv's Scripts/bin, `--user`'s site base, or (Windows
    Store Python) a redirected LocalCache dir that is on none of the paths a
    tool search would guess. `python -m yt_dlp` is equivalent and needs no path
    guessing, so it's the fallback rather than an error.

    Order matters because a system yt-dlp is often on PATH while the app was
    installed into an inactive venv: the interpreter's own copy (console script
    in sys.prefix, or the importable module) must win so `/update-ytdlp` upgrades
    the one we actually run. PATH is consulted only when this interpreter has no
    yt-dlp of its own.
    """
    bundled = config.PKG_DIR / "yt-dlp.exe"
    if IS_WINDOWS and bundled.exists():
        return [str(bundled)]
    bindir = "Scripts" if IS_WINDOWS else "bin"
    for exe in ("yt-dlp", "yt-dlp.exe"):
        cand = Path(sys.prefix) / bindir / exe
        if cand.exists():
            return [str(cand)]
    if importlib.util.find_spec("yt_dlp") is not None:
        return [sys.executable, "-m", "yt_dlp"]
    found = shutil.which("yt-dlp")
    if found:
        return [found]
    if bundled.exists():        # last resort (e.g. a manually-dropped binary)
        return [str(bundled)]
    raise RuntimeError(
        "yt-dlp not found. It ships as a dependency of this package — "
        "reinstall with `pip install -U fetchforge` (or `pip install -U yt-dlp[default]` "
        "into the same interpreter) and restart FetchForge."
    )


def _ytdlp_in_this_env(ytdlp_argv: list) -> bool:
    """True when the resolved yt-dlp lives inside the running interpreter's
    environment (venv / pip install) — i.e. `pip install -U` against
    sys.executable will actually upgrade the copy the app uses."""
    if ytdlp_argv[:1] == [sys.executable]:      # `python -m yt_dlp`: by definition ours
        return True
    # sys.prefix covers venv/system installs; the user base covers `pip install
    # --user`, which pip also upgrades in place from this interpreter.
    roots = [sys.prefix, site.getuserbase()]
    for root in roots:
        try:
            if Path(ytdlp_argv[0]).resolve().is_relative_to(Path(root).resolve()):
                return True
        except (OSError, ValueError, IndexError):
            continue
    return False


def _ytdlp_upgrade_cmd() -> list | None:
    """Command that upgrades the pip-dependency yt-dlp in this interpreter, or
    None when neither pip nor uv is available to do it.

    Mirrors launch.sh's pip -> uv ladder: a venv created by `uv venv`/`uv sync`
    has no pip, so fall back to uv. uv is always pinned to this interpreter's
    `--python` so the upgrade lands in the env yt-dlp actually runs from, not
    whatever env uv would infer from the cwd."""
    if importlib.util.find_spec("pip") is not None:
        return [sys.executable, "-m", "pip", "install", "-U", "yt-dlp[default]"]
    uv = shutil.which("uv")
    if uv:
        return [uv, "pip", "install", "--python", sys.executable, "-U", "yt-dlp[default]"]
    return None


async def read_ytdlp_version(argv: list) -> str:
    """`yt-dlp --version` for *argv*, or "" if it cannot be run."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
    except OSError:
        return ""
    return stdout.decode(errors="replace").strip()


def summarize_ytdlp_update(before: str, after: str, returncode: int, output: str) -> str:
    """One log line for an update run (#130). The raw updater output (a dozen
    pip "Requirement already satisfied" lines) belongs in server.log, not here."""
    if returncode != 0:
        lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
        reason = next((ln for ln in lines if "error" in ln.lower()),
                      lines[-1] if lines else f"exit code {returncode}")
        return f"yt-dlp update failed: {reason[:200]}"
    if before and after and before != after:
        return f"yt-dlp updated {before} -> {after}"
    version = after or before
    return f"yt-dlp up to date ({version})" if version else "yt-dlp up to date"


def _resolve_node_args() -> list:
    """yt-dlp's JS runtime for solving challenges. Resolve node from PATH;
    fall back to the canonical Windows install location it always used."""
    node = shutil.which("node")
    if not node and IS_WINDOWS:
        node = r"C:\Program Files\nodejs\node.exe"
    return ["--js-runtimes", f"node:{node}"] if node else []


NODE_ARGS = _resolve_node_args()


def is_http_url(url: str) -> bool:
    """True only for http/https URLs. yt-dlp treats any argv beginning with '-'
    as a flag (e.g. --exec=…, --config-location=…), so a crafted URL is remote
    code execution. Callers reject non-http(s) here AND append '--' before the
    URL so it can never be parsed as an option."""
    try:
        return urlparse((url or "").strip()).scheme in ("http", "https")
    except Exception:
        return False


# Windows illegal chars + tilde + control chars
ILLEGAL_RE = re.compile(r'[<>:"/\\|?*~\x00-\x1f]')


def sanitize(name: str) -> str:
    name = ILLEGAL_RE.sub("_", name)
    name = re.sub(r"_+", "_", name)
    return name.strip("_. ")[:200]


try:
    # The authoritative function yt-dlp itself uses for --restrict-filenames.
    # Available when yt-dlp is pip-installed (Linux/macOS, project .venv). On a
    # Windows bundled yt-dlp.exe the package may be absent — fall back below.
    from yt_dlp.utils import sanitize_filename as _ytdlp_sanitize_filename
except Exception:
    _ytdlp_sanitize_filename = None


def _restrict_filename(title: str) -> str:
    """Mirror yt-dlp's --restrict-filenames transform on a title. Uses yt-dlp's
    own sanitizer when importable (exact: handles accents like café→cafe, emoji,
    consecutive separators); otherwise a close ASCII approximation."""
    if _ytdlp_sanitize_filename is not None:
        try:
            return _ytdlp_sanitize_filename(title, restricted=True)
        except Exception:
            pass
    return re.sub(r'[^A-Za-z0-9_.\-]', '_', title)


def _output_stem(downloaded_stem: str, suffix: str = "") -> str:
    """Output stem for a downloaded file. A time-range *suffix* (which the yt-dlp
    -o template appended to the downloaded name) is kept intact even when the
    title is long enough for sanitize()'s 200-char cap to bite — otherwise a
    clip of a long-titled video would truncate to the full video's name."""
    if not suffix:
        return sanitize(downloaded_stem)
    if downloaded_stem.endswith(suffix):
        downloaded_stem = downloaded_stem[:-len(suffix)]
    return sanitize(downloaded_stem)[:200 - len(suffix)].rstrip("_. ") + suffix


def _predict_output_stem(title: str, suffix: str = "") -> str:
    """Predict the output file stem for a video title: yt-dlp's --restrict-filenames
    transform followed by our sanitize() — exactly what the post-download path
    derives from the real downloaded filename. Used to skip already-encoded videos
    without diverging from yt-dlp's actual output (H-8)."""
    return _output_stem(_restrict_filename(title), suffix)


# Audio-stream containers yt-dlp may produce when downloading audio-only formats.
_AUDIO_EXTS = {".webm", ".m4a", ".opus", ".ogg", ".mp3", ".aac", ".wav", ".flac"}


def _newest_new_file(cache_dir: Path, pre_existing: set, pattern="*.mkv"):
    """The most-recently-modified file matching `pattern` that appeared in
    `cache_dir` since `pre_existing` (a set of Paths) was snapshotted. Lets the
    pipeline locate THIS download's output without grabbing a sibling video's
    file already sitting in the shared cache (issue #8). None if nothing new.

    `pattern` may be one glob or an iterable of globs: audio downloads can land
    with any `_AUDIO_EXTS` suffix, which no single glob covers."""
    patterns = [pattern] if isinstance(pattern, str) else list(pattern)
    candidates = set()
    for pat in patterns:
        candidates.update(cache_dir.glob(pat))
    new = sorted(candidates - set(pre_existing), key=os.path.getmtime)
    return new[-1] if new else None


def _cache_snapshot_patterns(is_audio: bool) -> list:
    """Every file type yt-dlp may write for this download mode. Audio has many
    possible suffixes, so the snapshot/fallback must cover all of `_AUDIO_EXTS`
    rather than only `*.mkv` (issue #92)."""
    if is_audio:
        return ["*" + ext for ext in sorted(_AUDIO_EXTS)]
    return ["*.mkv"]


def _video_format_selector(vf: str, af: str, prefer_picked: bool) -> str:
    """Build yt-dlp's -f for a video download.

    `prefer_picked` (a single video, or a batch item that carries its own
    per-video format choice) honors the exact itags the user selected, but always
    appends a best-available fallback so a video missing those itags degrades
    gracefully instead of hard-failing with "Requested format is not available".

    Otherwise — a playlist, whose one picked format was read from only the FIRST
    video and can't represent a mixed 720p/1080p list — we always take the highest
    available stream per video (`bv*+ba/b`)."""
    fallback = "bv*+ba/b"
    if prefer_picked and vf and af:
        return "{}+{}/{}".format(vf, af, fallback)
    return fallback


def _audio_format_selector(af: str) -> str:
    """Build yt-dlp's -f for an audio-only download.

    The video path has had a best-available fallback since #13; audio never did,
    so a bare itag went straight to yt-dlp and any video lacking it hard-failed
    with "Requested format is not available" in about four seconds. That is not
    hypothetical: a playlist's audio pick is read from the FIRST entry only, and
    the `-drc` (dynamic-range-compressed) variants YouTube offers on some videos
    are absent on most, so one picked `140-drc` can fail a whole 110-item queue.

    Unlike resolution, audio itags are near-universal (140/251 are on almost
    everything), so the pick is usually representative and worth honouring even
    for a playlist -- we keep it and append the fallback, rather than discarding
    it the way the video selector must for a mixed 720p/1080p list."""
    fallback = "ba/b"
    return "{}/{}".format(af, fallback) if af else fallback


def _parse_items_payload(items_json: str) -> list:
    """Parse the client `items` payload into normalized per-item dicts. Every
    field the old tuple-of-structures model carried is kept on the item itself.
    Raises ValueError on a non-http url (mirrors the single-url guard) or a bad
    time range."""
    items = json.loads(items_json)
    parsed = []
    for it in items:
        u = it["url"]
        if not is_http_url(u):
            raise ValueError("URL must be http or https: {}".format(u))
        parsed.append({
            "url": u,
            "title": it.get("title", "") or "",
            "duration": it.get("duration") or 0,
            "video_format": it.get("video_format", "") or "",
            "audio_format": it.get("audio_format", "") or "",
            "expected_size": int(it.get("expected_size") or 0),
            "output_dir": it.get("output_dir", "") or "",
            "tune_mode": it.get("tune_mode", "uhq") or "uhq",
            "section": sections.parse_section(str(it.get("section_start") or ""),
                                              str(it.get("section_end") or "")),
        })
    return parsed


def _resolve_batch_items(items_json: str):
    """Backward-compatible view over `_parse_items_payload`: returns
    (video_urls, video_titles, video_durations, per_item)."""
    parsed = _parse_items_payload(items_json)
    video_urls = [it["url"] for it in parsed]
    video_titles = {it["url"]: it["title"] for it in parsed}
    video_durations = {it["url"]: it["duration"] for it in parsed}
    return video_urls, video_titles, video_durations, parsed


async def probe_video(path: Path) -> dict:
    """Deep-probe a video file and return source metadata."""
    proc = await asyncio.create_subprocess_exec(
        get_ffprobe(), "-v", "quiet",
        "-print_format", "json",
        "-show_streams", "-show_format",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    try:
        info = json.loads(out)
    except Exception:
        return {}

    fmt = info.get("format", {})
    streams = info.get("streams", [])
    vs = next((s for s in streams if s.get("codec_type") == "video"), {})

    duration = float(fmt.get("duration", 0) or 0)
    height = int(vs.get("height", 1080) or 1080)
    try:
        num, den = vs.get("r_frame_rate", "30/1").split("/")
        fps = int(num) / max(int(den), 1)
    except Exception:
        fps = 30.0

    # Per-stream bitrate is often absent from MKV; fall back to format total minus ~192k audio
    video_bps = int(vs.get("bit_rate", 0) or 0)
    if not video_bps:
        total_bps = int(fmt.get("bit_rate", 0) or 0)
        video_bps = max(total_bps - 192_000, 0)

    return {
        "duration": duration,
        "start_time": float(fmt.get("start_time", 0) or 0),
        "height": height,
        "fps": fps,
        "codec": vs.get("codec_name", "h264"),
        "pix_fmt": vs.get("pix_fmt", "yuv420p"),
        "color_transfer": vs.get("color_transfer", ""),
        "video_bitrate_kbps": video_bps // 1000,
    }


async def _download_is_complete(path: Path, expected_bytes: int,
                                expected_duration: Optional[float] = None,
                                min_ratio: float = 0.85) -> bool:
    """Decide whether a downloaded media file is actually complete after yt-dlp
    exited non-zero. With a known size, use the byte-ratio check. Without one
    (live streams, premieres, age-restricted — YouTube reports no filesize),
    do NOT trust the old existence+1MB heuristic: a 50MB slice of a 4GB video
    would pass. Instead verify the container's duration against the duration we
    learned at resolve time (±2s). If neither size nor duration is available we
    can't prove completeness, so treat it as incomplete (H-9)."""
    if not path.exists():
        return False
    if expected_bytes > 0:
        return path.stat().st_size >= expected_bytes * min_ratio
    if expected_duration and expected_duration > 0:
        info = await probe_video(path)
        actual = info.get("duration") or 0
        return actual > 0 and abs(actual - expected_duration) <= 2.0
    return False


_YTDLP_REASON_MAX = 300


def _ytdlp_error_reason(stderr_text: str, returncode: int) -> str:
    """Short, DOM-safe reason for a failed yt-dlp call: its last ``ERROR:`` line,
    else its last non-empty line, capped at _YTDLP_REASON_MAX chars. Raw stderr
    can run to pages of warnings, so it is never passed through wholesale."""
    lines = [ln.strip() for ln in stderr_text.splitlines() if ln.strip()]
    errors = [ln for ln in lines if ln.startswith("ERROR:")]
    reason = (errors or lines or [""])[-1]
    if reason.startswith("ERROR:"):
        reason = reason[len("ERROR:"):].strip()
    if not reason:
        reason = "yt-dlp exited with code {}".format(returncode)
    if len(reason) > _YTDLP_REASON_MAX:
        reason = reason[:_YTDLP_REASON_MAX - 1] + "…"
    return reason


async def _get_formats(url: str) -> dict:
    args = [
        *get_ytdlp_argv(),
        "-J",
        "--no-download",
        "--no-playlist",
        *NODE_ARGS,
    ]
    args += cookies.cookie_args()
    args += ["--", url]

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        # #67: never fail silently — an empty pair with no reason renders as two
        # blank dropdowns and hides bot checks, expired cookies, private videos.
        reason = _ytdlp_error_reason(stderr.decode(errors="replace"), proc.returncode)
        logger.warning("format fetch failed for %s: %s", url, reason)
        return {"video_formats": [], "audio_formats": [], "error": reason}

    info = json.loads(stdout)
    formats = info.get("formats", [])
    video_formats = []
    audio_formats = []

    for f in formats:
        fid = f.get("format_id", "")
        ext = f.get("ext", "")
        vcodec = f.get("vcodec", "none")
        acodec = f.get("acodec", "none")
        height = f.get("height")
        fps = f.get("fps")
        tbr = f.get("tbr")
        filesize = f.get("filesize") or f.get("filesize_approx")
        note = f.get("format_note", "")

        if vcodec != "none" and acodec == "none":
            label = f"{height}p" if height else ""
            if fps and fps > 30:
                label += f"{int(fps)}"
            if tbr:
                label += f" ~{int(tbr)}k"
            if filesize:
                label += f" ({filesize / 1024**3:.1f}GB)"
            video_formats.append({
                "id": fid, "ext": ext, "label": label,
                "height": height or 0, "fps": fps or 0,
                "vcodec": vcodec, "note": note,
                "filesize": filesize or 0,
            })
        elif acodec != "none" and vcodec == "none":
            abr = f.get("abr", 0)
            label = f"{acodec} {int(abr)}k" if abr else acodec
            if filesize:
                label += f" ({filesize / 1024**2:.0f}MB)"
            # Multi-language videos (YouTube auto-dubs) carry one format per
            # track. yt-dlp puts the track name first in format_note
            # ("Spanish (Spain), medium", "English (US) original (default), low"),
            # so lead the label with it or the tracks are indistinguishable.
            language = f.get("language")
            if language:
                track = note.split(",")[0].strip() or language
                label = f"{track} · {label}"
            audio_formats.append({
                "id": fid, "ext": ext, "label": label,
                "abr": abr or 0, "acodec": acodec, "note": note,
                "filesize": filesize or 0, "language": language,
                "language_preference": f.get("language_preference") or 0,
            })

    video_formats.sort(key=lambda x: (x["height"], x["fps"]), reverse=True)
    # Original-language track first (yt-dlp: 10 original, 5 default, -1 dub),
    # so the pre-selected audio is never an auto-dub that happens to have a
    # higher bitrate.
    audio_formats.sort(key=lambda x: (x["language_preference"], x["abr"]), reverse=True)
    return {"video_formats": video_formats, "audio_formats": audio_formats}
