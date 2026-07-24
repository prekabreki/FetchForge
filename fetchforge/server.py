import asyncio
import datetime
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

IS_WINDOWS = sys.platform.startswith("win")
if IS_WINDOWS:
    import ctypes

# Power management — prevent sleep during long encodes.
# Windows: SetThreadExecutionState, re-asserted on a timer.
# Linux:   hold a `systemd-inhibit` process for the duration.
# The public interface (_keep_awake / _allow_sleep, refcounted) is identical on
# both; only the backend differs.
_ES_CONTINUOUS       = 0x80000000
_ES_SYSTEM_REQUIRED  = 0x00000001
_ES_DISPLAY_REQUIRED = 0x00000002
_AWAKE_FLAGS = _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED | _ES_DISPLAY_REQUIRED

_awake_task: Optional[asyncio.Task] = None      # Windows: re-assertion loop
_awake_inhibitor: Optional[subprocess.Popen] = None   # Linux: systemd-inhibit handle
_awake_refcount = 0

def _set_awake():
    """Single-shot assertion — called repeatedly by the background task (Windows)."""
    ctypes.windll.kernel32.SetThreadExecutionState(_AWAKE_FLAGS)

async def _awake_loop():
    """Re-assert wakefulness every 30s so Windows never overrides it."""
    try:
        while True:
            _set_awake()
            await asyncio.sleep(30)
    except asyncio.CancelledError:
        pass
    finally:
        ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)

def _keep_awake():
    global _awake_task, _awake_inhibitor, _awake_refcount
    _awake_refcount += 1
    if IS_WINDOWS:
        if _awake_task is None or _awake_task.done():
            _awake_task = asyncio.get_event_loop().create_task(_awake_loop())
    else:
        if _awake_inhibitor is None or _awake_inhibitor.poll() is not None:
            inhibit = shutil.which("systemd-inhibit")
            if inhibit:
                # Block idle/sleep until this process is killed in _allow_sleep.
                _awake_inhibitor = subprocess.Popen(
                    [inhibit, "--what=idle:sleep", "--who=FetchForge",
                     "--why=Video encoding in progress", "sleep", "infinity"]
                )
            # No systemd-inhibit (rare) → no-op; an active encode keeps the box busy anyway.

def _allow_sleep():
    global _awake_task, _awake_inhibitor, _awake_refcount
    _awake_refcount = max(0, _awake_refcount - 1)
    if _awake_refcount != 0:
        return
    if _awake_task is not None and not _awake_task.done():
        _awake_task.cancel()
        _awake_task = None
    if _awake_inhibitor is not None and _awake_inhibitor.poll() is None:
        _awake_inhibitor.terminate()
        _awake_inhibitor = None


# Heartbeat watchdog — auto-quit when the browser tab goes away.
# The page pings GET /heartbeat every few seconds; if no ping arrives within
# _HEARTBEAT_TIMEOUT, the server self-exits. A grace window (rather than firing
# on tab-close directly) tolerates page refreshes and multiple open tabs.
#
# IMPORTANT: heartbeat silence alone must NOT kill the server while real work is
# in flight. Browsers throttle setInterval in backgrounded/hidden tabs (clamped
# to ~once/min, and frozen entirely after a few minutes), so a long queue run in
# a non-focused window would otherwise breach the 30s timeout and self-exit
# mid-encode. Two guards prevent that:
#   1. _active_jobs > 0 — a /download or /convert-local SSE stream is actively
#      running (download/encode in progress). Definitive proof the user is here;
#      never exit. Covers ~all of a queue's wall-clock (incl. pipeline mode,
#      where one request spans the whole playlist).
#   2. _INTER_ITEM_GRACE after the last job ends — rides out the gaps between
#      queue items, the client's retry backoffs (up to 45s), and the client-side
#      60s shutdown countdown, so a throttled tab can't kill the server in those
#      windows. Bounded so a truly-dead tab still auto-quits ~2 min after work.
_HEARTBEAT_TIMEOUT = 30          # seconds of silence before shutting down
_INTER_ITEM_GRACE = 120          # seconds to stay alive after a job ends (> 45s backoff + 60s shutdown countdown)
_last_heartbeat: float = 0.0     # monotonic timestamp of the last ping
_active_jobs = 0                 # count of /download + /convert-local SSE streams currently running
_last_job_end: float = 0.0       # monotonic timestamp when the last job stream finished
_uvicorn_server = None           # set in run_server(); lets the watchdog stop the server

async def _heartbeat_watchdog():
    """Exit the process once no tab has pinged for _HEARTBEAT_TIMEOUT seconds,
    UNLESS a job is running or just finished (see guards above)."""
    global _last_heartbeat
    _last_heartbeat = time.monotonic()    # start the clock at launch
    while True:
        await asyncio.sleep(5)
        now = time.monotonic()
        # A download/encode is actively streaming — the user is here even if the
        # backgrounded tab has throttled its heartbeat. Never exit.
        if _active_jobs > 0:
            continue
        # Just finished a job: ride out inter-item gaps, retry backoffs, and the
        # client's 60s shutdown countdown before trusting heartbeat silence.
        if now - _last_job_end < _INTER_ITEM_GRACE:
            continue
        if now - _last_heartbeat > _HEARTBEAT_TIMEOUT:
            if _uvicorn_server is not None:
                _uvicorn_server.should_exit = True
            return


async def _tracked(agen):
    """Wrap an SSE generator so the heartbeat watchdog knows a job is in flight.
    Increments _active_jobs for the life of the stream and stamps _last_job_end
    on completion/disconnect (the finally runs when Starlette closes the gen)."""
    global _active_jobs, _last_job_end
    _active_jobs += 1
    try:
        async for chunk in agen:
            yield chunk
    finally:
        _active_jobs -= 1
        _last_job_end = time.monotonic()

import secrets
from contextlib import asynccontextmanager
from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# The app binds to 127.0.0.1 only, but any web page the user visits can still
# issue cross-origin requests to it. Without an origin check those drive-by
# requests succeed (CSRF) — e.g. powering off the box via /shutdown-now or
# triggering downloads. These two origins are the only ones the real UI uses.
ALLOWED_ORIGINS = ["http://localhost:8765", "http://127.0.0.1:8765"]

# Per-process secret, injected into the served HTML and required as a header on
# the most dangerous endpoint (/shutdown-now). Regenerated every launch.
_SESSION_TOKEN = secrets.token_urlsafe(32)

@asynccontextmanager
async def lifespan(app: FastAPI):
    await _probe_nvenc_tune()
    watchdog = asyncio.create_task(_heartbeat_watchdog())
    yield
    watchdog.cancel()

# Hide the interactive API surface — this is a single-user local tool, not an API.
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def _origin_guard(request: Request, call_next):
    """Reject cross-origin state-changing requests. A browser always sends an
    Origin header on cross-origin POST/DELETE/PUT/PATCH; a drive-by page on
    evil.com cannot forge it to our allowlisted value, so this defangs CSRF.
    Same-origin UI requests carry an allowlisted Origin and pass through.
    Requests with no Origin at all (curl, the page's own EventSource GET) are
    not browser cross-origin attacks and are allowed."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("origin")
        if origin is not None and origin not in ALLOWED_ORIGINS:
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

PKG_DIR = Path(__file__).parent          # code + index.html template (read-only)
STATE_DIR = Path.cwd()                    # all writable runtime state


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


import functools

@functools.cache
def get_ffmpeg() -> str:
    return _resolve_tool("ffmpeg", PKG_DIR / "_internal" / "ffmpeg.exe")

@functools.cache
def get_ffprobe() -> str:
    return _resolve_tool("ffprobe", PKG_DIR / "_internal" / "ffprobe.exe")

@functools.cache
def get_ytdlp() -> str:
    return _resolve_tool("yt-dlp", PKG_DIR / "yt-dlp.exe")


def _ytdlp_in_this_env(ytdlp_path: str) -> bool:
    """True when the resolved yt-dlp lives inside the running interpreter's
    environment (venv / pip install) — i.e. `pip install -U` against
    sys.executable will actually upgrade the copy the app uses."""
    try:
        return Path(ytdlp_path).resolve().is_relative_to(Path(sys.prefix).resolve())
    except (OSError, ValueError):
        return False


def _resolve_node_args() -> list:
    """yt-dlp's JS runtime for solving challenges. Resolve node from PATH;
    fall back to the canonical Windows install location it always used."""
    node = shutil.which("node")
    if not node and IS_WINDOWS:
        node = r"C:\Program Files\nodejs\node.exe"
    return ["--js-runtimes", f"node:{node}"] if node else []


NODE_ARGS = _resolve_node_args()
COOKIES_PATH = STATE_DIR / "cookies.txt"

# Intermediate webm/mkv — cleared weekly. Override with FETCHFORGE_CACHE_DIR
# (the legacy BOP_CACHE_DIR is still honored so existing overrides don't break).
_cache_override = os.getenv("FETCHFORGE_CACHE_DIR") or os.getenv("BOP_CACHE_DIR")
if _cache_override:
    CACHE_DIR = Path(_cache_override)
elif IS_WINDOWS:
    CACHE_DIR = Path(r"E:/Cache/ytdlp") if Path(r"E:/").exists() else Path(r"C:/cache/ytdlp")
else:
    CACHE_DIR = Path(os.getenv("XDG_CACHE_HOME", Path.home() / ".cache")) / "fetchforge" / "ytdlp"
OUTPUT_DIR = STATE_DIR / "downloads"    # final converted mp4s
CONVERTED_DIR = OUTPUT_DIR / "converted"
HISTORY_PATH = STATE_DIR / "history.json"
LOGS_DIR = STATE_DIR / "logs"
from fetchforge import __version__ as APP_VERSION


import logging
from logging.handlers import RotatingFileHandler

logger = logging.getLogger("fetchforge")


def _setup_logging() -> None:
    """Route our own output and uvicorn's loggers into a rotating logs/server.log
    (5MB × 3) so headless launches (launch-hidden.vbs / launch.sh with no console)
    still leave a debuggable trail — without it, a crash before the browser
    connects is silent. The visible-console launchers keep their stdout too."""
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(
        LOGS_DIR / "server.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    logger.setLevel(logging.INFO)
    logger.handlers = [file_handler, stream_handler]
    logger.propagate = False
    # Tee uvicorn's loggers into the same file (they keep their own console handlers).
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).addHandler(file_handler)


def _ensure_runtime_dirs() -> None:
    """Create the writable runtime dirs (cache, downloads, logs). Called from
    run_server() — NOT at import time — so a bare `import fetchforge.server`
    (tests, third parties) has no filesystem side effects in the importer's cwd."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)
    CONVERTED_DIR.mkdir(exist_ok=True)
    LOGS_DIR.mkdir(exist_ok=True)


def _poweroff():
    """Shut the machine down — the UI's optional 'power off when done'.
    Windows: `shutdown /s /t 0`. Linux: `systemctl poweroff` (logind allows the
    active-session user without a password), falling back to `shutdown -h now`."""
    if IS_WINDOWS:
        subprocess.run(["shutdown", "/s", "/t", "0"])
        return
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "poweroff"])
    else:
        subprocess.run(["shutdown", "-h", "now"])


_history_lock = asyncio.Lock()


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text via a temp file + os.replace so a crash mid-write can't truncate
    the real file (os.replace is atomic on the same filesystem)."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _read_history_sync() -> list:
    if HISTORY_PATH.exists():
        try:
            return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


async def _load_history() -> list:
    # Off the event loop — a synchronous read here jitters any in-flight SSE stream.
    return await asyncio.to_thread(_read_history_sync)


async def _save_history_entry(entry: dict):
    # Lock the whole read-modify-write so two concurrent /history POSTs can't lose
    # each other's entry; write atomically so a crash can't truncate the file.
    async with _history_lock:
        history = await asyncio.to_thread(_read_history_sync)
        history = [h for h in history if h.get("url") != entry["url"]]
        history.insert(0, entry)
        payload = json.dumps(history[:50], ensure_ascii=False, indent=2)
        await asyncio.to_thread(_atomic_write_text, HISTORY_PATH, payload)


# Windows illegal chars + tilde + control chars
ILLEGAL_RE = re.compile(r'[<>:"/\\|?*~\x00-\x1f]')

# Set when yt-dlp reports the loaded cookies have been rotated/invalidated. Once
# that happens the cookies are dead, and passing them is strictly worse than
# passing none (forces a degraded client + hard format failures), so we drop them
# for the rest of the run. Persists across the client's retry rounds; reset when
# the user loads fresh cookies (upload/paste), which deserve a new chance.
_cookies_disabled = False

# yt-dlp's stale-cookie warning, e.g. "The provided YouTube account cookies are no
# longer valid. They have likely been rotated in the browser as a security measure."
_STALE_COOKIE_MARKERS = ("no longer valid", "have likely been rotated")

def cookie_args() -> list:
    if _cookies_disabled:
        return []
    return ["--cookies", str(COOKIES_PATH)] if COOKIES_PATH.exists() else []


def _maybe_flag_stale_cookies(line: str) -> Optional[str]:
    """If a yt-dlp output line signals rotated/invalid cookies, disable cookies for
    the rest of the run and return a one-time user-facing warning to emit; else None."""
    global _cookies_disabled
    if _cookies_disabled or not COOKIES_PATH.exists():
        return None
    low = line.lower()
    if "cookie" in low and any(m in low for m in _STALE_COOKIE_MARKERS):
        _cookies_disabled = True
        return ("Your cookies appear stale (rotated/invalidated by the browser). "
                "Continuing this run without cookies — public videos still download. "
                "For private/age-restricted videos, re-export cookies from a fresh "
                "private window and reload them.")
    return None


def is_http_url(url: str) -> bool:
    """True only for http/https URLs. yt-dlp treats any argv beginning with '-'
    as a flag (e.g. --exec=…, --config-location=…), so a crafted URL is remote
    code execution. Callers reject non-http(s) here AND append '--' before the
    URL so it can never be parsed as an option."""
    try:
        return urlparse((url or "").strip()).scheme in ("http", "https")
    except Exception:
        return False


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


def _predict_output_stem(title: str) -> str:
    """Predict the output file stem for a video title: yt-dlp's --restrict-filenames
    transform followed by our sanitize() — exactly what the post-download path
    derives from the real downloaded filename. Used to skip already-encoded videos
    without diverging from yt-dlp's actual output (H-8)."""
    return sanitize(_restrict_filename(title))


def _resolve_batch_items(items_json: str):
    """Parse the client `items` payload into the structures the pipeline
    consumes. Returns (video_urls, video_titles, video_durations, per_item)
    where per_item[i] carries the per-video format/output-dir/tune. Raises
    ValueError on a non-http url (mirrors the single-url guard)."""
    items = json.loads(items_json)
    video_urls, video_titles, video_durations, per_item = [], {}, {}, []
    for it in items:
        u = it["url"]
        if not is_http_url(u):
            raise ValueError("URL must be http or https: {}".format(u))
        video_urls.append(u)
        video_titles[u] = it.get("title", "")
        video_durations[u] = it.get("duration") or 0
        per_item.append({
            "video_format": it.get("video_format", ""),
            "audio_format": it["audio_format"],
            "expected_size": int(it.get("expected_size") or 0),
            "output_dir": it.get("output_dir", "") or "",
            "tune_mode": it.get("tune_mode", "uhq"),
        })
    return video_urls, video_titles, video_durations, per_item


# ── SSE event builders (single source of truth for the wire format) ───────────
# Every event is `data: {json}\n\n`. Centralizing this kills the f-string-vs-.format
# split that the file used to carry (the f-string-backslash gotcha is avoided here
# once, in _sse) and gives each event type one definition. The UI splits `phase`
# msgs on ": " to separate header from filename — sse_phase enforces that contract.
def _sse(obj: dict) -> str:
    return "data: {}\n\n".format(json.dumps(obj))

def sse_log(msg: str) -> str:
    return _sse({"type": "log", "msg": msg})

def sse_error(msg: str) -> str:
    return _sse({"type": "error", "msg": msg})

def sse_cookie_warning(msg: str) -> str:
    return _sse({"type": "cookie_warning", "msg": msg})

def sse_cancelled(msg: str = "Cancelled by user.") -> str:
    return _sse({"type": "cancelled", "msg": msg})

def sse_done(msg: str = "All done!") -> str:
    return _sse({"type": "done", "msg": msg})

def sse_shutdown(msg: str = "Shutting down in 60 seconds...") -> str:
    return _sse({"type": "shutdown", "msg": msg})

def sse_phase(header: str, filename: str = "") -> str:
    # UI contract: "header: filename" — split on the first ": " in index.html.
    msg = "{}: {}".format(header, filename) if filename else header
    return _sse({"type": "phase", "msg": msg})

def sse_video_start(current: int, total: int) -> str:
    return _sse({"type": "video_start", "current": current, "total": total})

def sse_source_size(size_bytes: int) -> str:
    return _sse({"type": "source_size", "bytes": size_bytes})

def sse_progress(pct: float, overall: float, speed: str, eta: str, size: str, output_bytes: int = 0) -> str:
    return _sse({"type": "progress", "pct": pct, "overall": overall,
                 "speed": speed, "eta": eta, "size": size, "output_bytes": output_bytes})

def sse_dl_progress(pct: float, size: str, speed: str, eta: str, idx: int, total: int) -> str:
    return _sse({"type": "dl_progress", "pct": pct, "size": size,
                 "speed": speed, "eta": eta, "idx": idx, "total": total})

def sse_item_done(idx: int, total: int) -> str:
    return _sse({"type": "item_done", "idx": idx, "total": total})

def sse_item_failed(idx: int, total: int, msg: str) -> str:
    return _sse({"type": "item_failed", "idx": idx, "total": total, "msg": msg})

def sse_size_info(source_bytes: int, output_bytes: int) -> str:
    bloat = (output_bytes - source_bytes) / source_bytes * 100 if source_bytes > 0 else 0.0
    return _sse({"type": "size_info",
                 "source_mb": round(source_bytes / 1024**2, 1),
                 "output_mb": round(output_bytes / 1024**2, 1),
                 "bloat_pct": round(bloat, 1)})


_CUVID_DECODERS = {
    "vp9":  "vp9_cuvid",
    "h264": "h264_cuvid",
    "hevc": "hevc_cuvid",
    "av1":  "av1_cuvid",
    "vp8":  "vp8_cuvid",
}


def _ffmpeg_has_filter(name: str) -> bool:
    """Whether this ffmpeg build provides a given filter."""
    try:
        out = subprocess.run(
            [get_ffmpeg(), "-hide_banner", "-filters"],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except Exception:
        return False
    return any(line.split()[1:2] == [name] for line in out.splitlines() if line.strip())


# GPU-resident pixel-format conversion needs the scale_cuda filter (libnpp).
# The bundled Windows ffmpeg (gyan.dev) has it; Fedora/Nobara builds ship
# --disable-libnpp, so it's absent there even though hevc_nvenc encoding works.
HAS_SCALE_CUDA = _ffmpeg_has_filter("scale_cuda")


def _decode_filter_args(input_file: Path, pix_fmt: str, cuvid: Optional[str]) -> list:
    """Decode + pixel-format args feeding the hevc_nvenc encode.

    Fast path (scale_cuda present): NVDEC decode into CUDA memory and convert
    the pixel format on the GPU — zero CPU roundtrip. This is what Windows used.
    Fallback (no scale_cuda): NVDEC decode but let ffmpeg download frames so a
    CPU `format` filter can run; hevc_nvenc re-uploads and encodes. Slower by a
    PCIe copy, but works on stock Linux ffmpeg. hevc_nvenc does the heavy lifting
    either way."""
    if HAS_SCALE_CUDA:
        return [
            "-hwaccel", "cuda", "-hwaccel_output_format", "cuda",
            *(["-c:v", cuvid] if cuvid else []),
            "-i", str(input_file),
            "-vf", f"scale_cuda=format={pix_fmt}",
        ]
    return [
        "-hwaccel", "cuda",
        "-i", str(input_file),
        "-vf", f"format={pix_fmt}",
    ]

# Audio-stream containers yt-dlp may produce when downloading audio-only formats.
_AUDIO_EXTS = {".webm", ".m4a", ".opus", ".ogg", ".mp3", ".aac", ".wav", ".flac"}


def audio_output_ext(preset: str) -> str:
    return "wav" if preset == "wav" else "mp3"


def build_audio_ffmpeg(input_file: Path, output_file: Path, preset: str) -> list:
    """ffmpeg args for audio extraction. -vn drops video; -progress pipe:1
    matches the video encode flow so the UI parser stays unified."""
    if preset == "wav":
        codec_args = ["-c:a", "pcm_s16le"]
    else:
        codec_args = ["-c:a", "libmp3lame", "-b:a", "320k"]
    return [
        get_ffmpeg(), "-y",
        "-loglevel", "info",
        "-i", str(input_file),
        "-vn",
        *codec_args,
        "-progress", "pipe:1",
        "-nostats",
        str(output_file),
    ]


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
        "height": height,
        "fps": fps,
        "codec": vs.get("codec_name", "h264"),
        "pix_fmt": vs.get("pix_fmt", "yuv420p"),
        "color_transfer": vs.get("color_transfer", ""),
        "video_bitrate_kbps": video_bps // 1000,
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
) -> dict:
    """
    Derive NVENC encode parameters from source analysis.
    cq_override=0  → auto-select by resolution.
    maxrate_override="" → auto-calculate from source bitrate + codec efficiency.
    """
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
    res_ceil = next(
        (v for k, v in [(2160, 20), (1440, 12), (1080, 7), (720, 4)] if height >= k), 4
    )
    if fps > 35 and height < 2160:  # 60fps headroom for sub-4K only; 4K stays at 20M ceiling
        res_ceil = round(res_ceil * 1.40)

    # Per-resolution minimum — ensures CQ always has enough headroom to hit its target
    res_min = next(
        (v for k, v in [(2160, 8), (1440, 5), (1080, 3), (720, 2)] if height >= k), 2
    )

    # Source-derived average bitrate target and bloom-capped ceiling.
    # Using a real b_v average + maxrate ceiling lets NVENC vary around the average
    # and burst above it for complex frames — proper VBR instead of de-facto CBR.
    derived_mbps = round(video_bitrate_kbps * efficiency / 1000, 1) if video_bitrate_kbps > 0 else None

    if maxrate_override and maxrate_override not in ("", "0"):
        # User-specified ceiling: pure CQ mode, no average target
        maxrate_mbps = int(maxrate_override.replace("M", ""))
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
    if nvenc_tune == "hq" and video_bitrate_kbps > 0 and not (maxrate_override and maxrate_override not in ("", "0")):
        source_cap_mbps = video_bitrate_kbps / 1000
        maxrate_mbps = max(min(maxrate_mbps, source_cap_mbps), res_min)

    # HDR / 10-bit detection
    hdr_transfers = {"smpte2084", "arib-std-b67", "smpte428", "bt2020-10", "bt2020-12"}
    hdr = color_transfer in hdr_transfers
    ten_bit = "10le" in pix_fmt or "10be" in pix_fmt or hdr
    pix_out = "yuv420p10le" if ten_bit else "yuv420p"

    return {
        "cq": cq,
        # hq: p2 freely for all 60fps (no uhq restriction). uhq: p2 rejected by uhq at 4K;
        # p3 for 4K 60fps, p2 for sub-4K 60fps (handled at encode time for p2→p4 guard).
        "preset": ("p2" if fps > 35 else "p4") if nvenc_tune == "hq" else (
            "p2" if (fps > 35 and height < 2160) else ("p3" if fps > 35 else "p4")
        ),
        "maxrate": f"{maxrate_mbps}M",
        "bufsize": f"{maxrate_mbps * 2}M",
        "pix_fmt": pix_out,
        "profile": "main10" if ten_bit else "main",
        "hdr": hdr,
        "ten_bit": ten_bit,
        "bf": 3,
        "b_ref_mode": "middle",
    }

current_process: Optional[asyncio.subprocess.Process] = None
current_dl_process: Optional[asyncio.subprocess.Process] = None
# Sticky cancel flag. /cancel sets it; every download/encode loop checks it at the
# top of each iteration and inside the subprocess read loops, so a cancel stops the
# WHOLE job (no next item, no retry) — not just the one process it happened to catch.
# Cleared at the start of every new /download and /convert-local stream.
_cancel_requested = asyncio.Event()
_nvenc_tune: str = "uhq"  # Verified at startup; falls back to "hq" on older drivers
_MAX_CONSECUTIVE_ENCODE_FAILURES = 3  # pipeline mode: bail out if the encoder is fundamentally broken


async def _probe_nvenc_tune():
    """Test whether hevc_nvenc accepts -tune uhq.
    Note: -bf at any value (even 0) is incompatible with uhq on all tested NVENC GPUs
    (RTX 3060, RTX 4080). uhq manages B-frames internally; passing -bf overrides this
    and causes InitializeEncoder failed: invalid param (8). The probe tests uhq without
    -bf, which is how it's actually used in encodes."""
    global _nvenc_tune
    proc = await asyncio.create_subprocess_exec(
        get_ffmpeg(), "-f", "lavfi", "-i", "nullsrc=s=1920x1080:d=0.1",
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
    if "InitializeEncoder failed" in err or "Unable to parse option value" in err or "invalid param" in err:
        _nvenc_tune = "hq"
        logger.info("hevc_nvenc: 'uhq' not supported on this driver — using 'hq' + AQ")
    else:
        _nvenc_tune = "uhq"
        logger.info("hevc_nvenc: 'uhq' supported")


def build_video_ffmpeg_args(input_file: Path, output_file: Path, params: dict,
                            effective_tune: str, codec: str) -> list:
    """Construct the hevc_nvenc ffmpeg argv. Pure (no I/O beyond the module-level
    capability probes) so it can be unit-tested with a fixture params dict. Single
    source of truth for the three former copies (H-2)."""
    # uhq requires p4+ minimum; p2 rejected — guard only needed for the uhq path.
    effective_preset = "p4" if (effective_tune == "uhq" and params["preset"] == "p2") else params["preset"]
    cuvid = _CUVID_DECODERS.get((codec or "").lower())
    return [
        get_ffmpeg(), "-y",
        "-loglevel", "info",
        *_decode_filter_args(input_file, params["pix_fmt"], cuvid),
        "-c:v", "hevc_nvenc",
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
        *(["-spatial_aq", "1", "-aq-strength", "8", "-temporal_aq", "1"] if effective_tune != "uhq" else []),
        "-c:a", "aac",
        "-b:a", "192k",
        "-progress", "pipe:1",
        "-nostats",
        str(output_file),
    ]


async def run_encode(ffmpeg_args: list, *, duration_secs, idx: int, total: int,
                     result: dict):
    """Run one ffmpeg encode/extract, yielding SSE strings for progress + log.

    Drains stdout (the -progress key=value stream) and stderr (ffmpeg's human
    log) CONCURRENTLY via a single queue, so stderr lines reach the UI within
    ~100ms even when no stdout arrives during a long codec init (H-4) — no more
    O(n) list.pop(0) gated on stdout. Sets current_process so /cancel can
    terminate it, and honours the sticky _cancel_requested flag. The ffmpeg
    return code is written to result["returncode"]; this is the single encode
    loop the three call sites share (H-2)."""
    global current_process
    proc = await asyncio.create_subprocess_exec(
        *ffmpeg_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    current_process = proc

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
            if _cancel_requested.is_set() and not terminated:
                proc.terminate()
                terminated = True
                # keep draining both pipes to EOF so the pump tasks finish cleanly
                continue
            line = raw.decode(errors="replace").rstrip()
            if tag == "err":
                if line:
                    yield sse_log(line)
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
                    yield sse_progress(file_pct, overall, speed_str, fps_str + " fps", live_size, live_bytes)
                    progress_buf = {}
            elif line:
                yield sse_log(line)
    finally:
        await t_out
        await t_err
        await proc.wait()
        current_process = None
        result["returncode"] = proc.returncode


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = PKG_DIR / "index.html"
    html = await asyncio.to_thread(html_path.read_text, encoding="utf-8")
    # Hand the page this launch's session token (consumed by /shutdown-now).
    html = html.replace("__DLPR_TOKEN__", _SESSION_TOKEN)
    return HTMLResponse(html)


@app.post("/upload-cookies")
async def upload_cookies(file: UploadFile = File(...)):
    global _cookies_disabled
    content = await file.read()
    await asyncio.to_thread(COOKIES_PATH.write_bytes, content)
    _cookies_disabled = False    # fresh cookies deserve a new chance
    return {"status": "ok", "message": "cookies.txt saved"}


def _validate_cookies_text(text: str) -> dict:
    """Parse a Netscape cookie file and report counts. Does not save."""
    text = (text or "").strip()
    if not text:
        return {"valid": False, "reason": "empty input"}

    lines = text.splitlines()
    cookie_lines = [l for l in lines if l.strip() and not l.lstrip().startswith("#")]
    if not cookie_lines:
        return {"valid": False, "reason": "no cookie entries found"}

    valid_cookies = 0
    youtube_cookies = 0
    for l in cookie_lines:
        parts = l.split("\t")
        if len(parts) >= 7:
            valid_cookies += 1
            domain = parts[0].lower()
            if "youtube" in domain or "google" in domain:
                youtube_cookies += 1

    if valid_cookies == 0:
        return {"valid": False, "reason": "lines are not tab-separated Netscape cookies"}

    return {"valid": True, "cookies": valid_cookies, "youtube_cookies": youtube_cookies}


@app.post("/paste-cookies")
async def paste_cookies(text: str = Form(...)):
    global _cookies_disabled
    result = _validate_cookies_text(text)
    if not result.get("valid"):
        return result

    body = text.strip()
    if not any("netscape http cookie file" in l.lower() for l in body.splitlines()[:5]):
        body = "# Netscape HTTP Cookie File\n" + body
    await asyncio.to_thread(COOKIES_PATH.write_text, body, encoding="utf-8")
    _cookies_disabled = False    # fresh cookies deserve a new chance
    return result


@app.get("/check-cookies")
async def check_cookies():
    if not COOKIES_PATH.exists():
        return {"exists": False}
    try:
        text = await asyncio.to_thread(COOKIES_PATH.read_text, encoding="utf-8", errors="replace")
        info = _validate_cookies_text(text)
        return {"exists": True, **info}
    except Exception as e:
        return {"exists": True, "valid": False, "reason": str(e)}


@app.delete("/cookies")
async def clear_cookies():
    """Remove the loaded cookies.txt so downloads run cookie-free. Public content
    needs no auth, and stale cookies are worse than none (see issue #9). Guarded
    against cross-origin callers by _origin_guard like other mutating routes."""
    existed = COOKIES_PATH.exists()
    await asyncio.to_thread(COOKIES_PATH.unlink, missing_ok=True)
    return {"status": "ok", "existed": existed}


@app.get("/history")
async def get_history():
    return await _load_history()


@app.post("/history")
async def add_history(
    url: str = Form(...),
    title: str = Form(""),
    thumbnail: str = Form(""),
    is_playlist: str = Form("false"),
    count: str = Form("1"),
):
    entry = {
        "url": url,
        "title": title,
        "thumbnail": thumbnail,
        "is_playlist": is_playlist == "true",
        "count": int(count) if count.isdigit() else 1,
        "added": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    await _save_history_entry(entry)
    return {"status": "ok"}


@app.delete("/history")
async def clear_history():
    async with _history_lock:
        if HISTORY_PATH.exists():
            await asyncio.to_thread(HISTORY_PATH.unlink)
    return {"status": "ok"}


@app.get("/ytdlp-version")
async def ytdlp_version():
    proc = await asyncio.create_subprocess_exec(
        str(get_ytdlp()), "--version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return {"version": stdout.decode().strip()}


@app.post("/update-ytdlp")
async def update_ytdlp():
    """Update yt-dlp using the mechanism that matches how it was installed.

    yt-dlp's own `-U` self-updater refuses for pip / package-manager installs, so
    it's only correct for the Windows bundled standalone exe. For the normal
    pip-dependency case we upgrade via pip against this interpreter; a system
    binary we don't own can't be updated in place, so we say so."""
    ytdlp = get_ytdlp()
    bundled = PKG_DIR / "yt-dlp.exe"
    if IS_WINDOWS and str(bundled) == ytdlp:
        cmd = [ytdlp, "-U"]
    elif _ytdlp_in_this_env(ytdlp):
        cmd = [sys.executable, "-m", "pip", "install", "-U", "yt-dlp[default]"]
    else:
        return {"output": (
            "yt-dlp here is a system/package-managed binary at {}.\n"
            "FetchForge can't update it in place — update it with your OS package "
            "manager (e.g. `sudo dnf upgrade yt-dlp` or `sudo apt upgrade yt-dlp`), "
            "or run FetchForge from a virtualenv where yt-dlp is a pip dependency."
        ).format(ytdlp)}
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    # get_ytdlp() is cached to a path; a pip upgrade replaces the package in place,
    # so the path stays valid and /ytdlp-version (a fresh subprocess) reports the
    # new version without a server restart.
    return {"output": stdout.decode(errors="replace").strip()}


@app.get("/video-info")
async def video_info(url: str):
    if not is_http_url(url):
        return JSONResponse({"error": "URL must be http or https"}, status_code=400)
    # Use --flat-playlist first to detect playlist vs single
    args = [
        str(get_ytdlp()),
        "--flat-playlist",
        "-J",
        "--no-download",
        *NODE_ARGS,
    ]
    args += cookie_args()
    args += ["--", url]

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode(errors="replace")
        return JSONResponse({"error": err}, status_code=400)

    info = json.loads(stdout)
    is_playlist = info.get("_type") == "playlist"

    if is_playlist:
        entries = info.get("entries", [])
        # For playlists, fetch format info from the first entry
        first_url = entries[0].get("url") or entries[0].get("webpage_url") if entries else url
        format_info = await _get_formats(first_url)
        return {
            "is_playlist": True,
            "title": info.get("title", "Playlist"),
            "uploader": info.get("uploader") or info.get("channel"),
            "count": len(entries),
            "thumbnail": entries[0].get("thumbnails", [{}])[-1].get("url") if entries else None,
            **format_info,
        }
    else:
        format_info = await _get_formats(url)
        return {
            "is_playlist": False,
            "title": info.get("title", "Unknown"),
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "uploader": info.get("uploader"),
            **format_info,
        }


async def _get_formats(url: str) -> dict:
    args = [
        str(get_ytdlp()),
        "-J",
        "--no-download",
        "--no-playlist",
        *NODE_ARGS,
    ]
    args += cookie_args()
    args += ["--", url]

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        return {"video_formats": [], "audio_formats": []}

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
            audio_formats.append({
                "id": fid, "ext": ext, "label": label,
                "abr": abr or 0, "acodec": acodec, "note": note,
                "filesize": filesize or 0,
            })

    video_formats.sort(key=lambda x: (x["height"], x["fps"]), reverse=True)
    audio_formats.sort(key=lambda x: x["abr"], reverse=True)
    return {"video_formats": video_formats, "audio_formats": audio_formats}


@app.post("/download")
async def download(
    url: str = Form(...),
    video_format: str = Form(""),
    audio_format: str = Form(...),
    convert: str = Form("true"),
    cq: str = Form("0"),
    maxrate: str = Form(""),
    pipeline: str = Form("false"),
    delete_cache: str = Form("true"),
    expected_size: str = Form("0"),  # expected MKV size in bytes (video+audio from YouTube metadata)
    shutdown: str = Form("false"),
    output_dir: str = Form(""),
    tune_mode: str = Form("uhq"),
    mode: str = Form("video"),       # "video" → H.265 MP4; "audio" → WAV/MP3 extract
    audio_preset: str = Form("mp3"), # "wav" or "mp3"; only used when mode == "audio"
    items: str = Form(""),           # JSON array of per-item {url,video_format,audio_format,expected_size,output_dir,tune_mode,title,duration}
):
    async def stream():
        global current_process
        _cancel_requested.clear()   # fresh job — drop any stale cancel
        _keep_awake()
        _job_start = datetime.datetime.now()
        _job_lines = []

        def _jlog(level, msg):
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            _job_lines.append("[{}] [{}] {}".format(ts, level, msg))

        try:
            async for chunk in _download_stream():
                if chunk.startswith("data: "):
                    try:
                        evt = json.loads(chunk[6:].strip())
                        t = evt.get("type", "")
                        if t == "phase":
                            _jlog("PHASE", evt.get("msg", ""))
                        elif t == "log":
                            _jlog("LOG", evt.get("msg", ""))
                        elif t == "error":
                            _jlog("ERROR", evt.get("msg", ""))
                        elif t == "done":
                            _jlog("DONE", evt.get("msg", ""))
                        elif t == "video_start":
                            _jlog("VIDEO", "Video {current}/{total}".format(**evt))
                        elif t == "size_info":
                            _jlog("SIZE", "source={source_mb}MB  output={output_mb}MB  bloat={bloat_pct:+.1f}%".format(**evt))
                    except Exception:
                        pass
                yield chunk
        finally:
            elapsed = datetime.datetime.now() - _job_start
            status = "CANCELLED"
            for ln in reversed(_job_lines):
                if "[ERROR]" in ln:
                    status = "FAILED"
                    break
                if "[DONE]" in ln:
                    status = "SUCCESS"
                    break
            ts_file = _job_start.strftime("%Y-%m-%d_%H-%M-%S")
            log_path = LOGS_DIR / "{}_download.txt".format(ts_file)
            header_lines = [
                "=== FetchForge Job Log ===",
                "Started : {}".format(_job_start.strftime("%Y-%m-%d %H:%M:%S")),
                "Type    : YouTube Download",
                "URL     : {}".format(url),
                "Elapsed : {}".format(str(elapsed).split(".")[0]),
                "Status  : {}".format(status),
                "",
                "--- Events ---",
                "",
            ]
            try:
                await asyncio.to_thread(
                    log_path.write_text,
                    "\n".join(header_lines + _job_lines + [""]),
                    encoding="utf-8",
                )
            except Exception as exc:
                logger.warning("Log write failed: %s", exc)
            _allow_sleep()

    async def _download_stream():
        global current_process

        dest_dir = Path(output_dir) if output_dir else CONVERTED_DIR
        dest_dir.mkdir(parents=True, exist_ok=True)

        is_audio = mode == "audio"
        out_ext = audio_output_ext(audio_preset) if is_audio else "mp4"
        out_suffix = "" if is_audio else "_h265"  # video keeps the _h265 marker

        def _predicted_output(stem: str) -> Path:
            return dest_dir / "{}{}.{}".format(stem, out_suffix, out_ext)

        # In audio mode the user-selected audio_format is the only stream we pull;
        # no merge to MKV. Output filename detection swaps from [Merger] to [download] Destination.
        def _yt_format_args() -> list:
            if is_audio:
                return ["-f", audio_format]
            return ["-f", "{}+{}".format(video_format, audio_format)]

        def _yt_merge_args() -> list:
            return [] if is_audio else ["--merge-output-format", "mkv"]

        def _glob_downloaded(cache: Path) -> list:
            if is_audio:
                return sorted(
                    [f for f in cache.iterdir() if f.is_file() and f.suffix.lower() in _AUDIO_EXTS],
                    key=os.path.getmtime,
                )
            return sorted(cache.glob("*.mkv"), key=os.path.getmtime)

        # ── Step 1: resolve batch / playlist / single video ───────────────────
        # Batch mode: the client handed us a ready list of independent items.
        # Skip the playlist resolve entirely — metadata came from /video-info.
        batch = items.strip() != ""
        per_item = []
        if batch:
            try:
                video_urls, video_titles, video_durations, per_item = _resolve_batch_items(items)
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                yield sse_error("Bad batch payload: {}".format(exc))
                return
            total_videos = len(video_urls)
            if total_videos == 0:
                yield sse_error("Batch had no items.")
                return
            yield sse_log("Batch: {} video(s) queued — pipelined.".format(total_videos))
        else:
            if not is_http_url(url):
                yield sse_error('URL must be http or https.')
                return

            yield sse_phase('Resolving...')

            flat_args = [str(get_ytdlp()), "--flat-playlist", "-J", "--no-download", *NODE_ARGS]
            flat_args += cookie_args()
            flat_args += ["--", url]

            flat_proc = await asyncio.create_subprocess_exec(
                *flat_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            flat_out, flat_err = await flat_proc.communicate()

            if flat_proc.returncode != 0:
                yield sse_error('Could not fetch video info: ' + flat_err.decode(errors='replace')[:300])
                return

            try:
                info = json.loads(flat_out)
            except Exception:
                yield sse_error('Failed to parse video info.')
                return

            if info.get("_type") == "playlist":
                entries = [e for e in info.get("entries", []) if e]
                video_urls = []
                video_titles = {}     # url → title, used to skip already-encoded videos
                video_durations = {}  # url → duration (s), used to verify download completeness
                for e in entries:
                    vid_url = e.get("webpage_url") or e.get("url") or ""
                    if vid_url and not vid_url.startswith("http"):
                        vid_url = f"https://www.youtube.com/watch?v={vid_url}"
                    if vid_url:
                        video_urls.append(vid_url)
                        video_titles[vid_url] = e.get("title", "")
                        video_durations[vid_url] = e.get("duration") or 0
                total_videos = len(video_urls)
                yield sse_log(f'Playlist: {total_videos} video(s) queued — processing one at a time.')
            else:
                video_urls = [url]
                video_titles = {url: info.get("title") or ""}
                video_durations = {url: info.get("duration") or 0}
                total_videos = 1

        # ── Step 2 ────────────────────────────────────────────────────────────
        if (pipeline == "true" or batch) and total_videos >= 1 and convert == "true" and not is_audio and (batch or total_videos > 1):
            # ── Pipeline mode: downloader and encoder run concurrently ─────────
            yield sse_log('Pipeline mode: next download starts while current video encodes.')

            file_q: asyncio.Queue = asyncio.Queue(maxsize=1)
            msg_q: asyncio.Queue = asyncio.Queue()
            abort = asyncio.Event()        # set on hard failure (stops the pipeline)
            enc_gone = asyncio.Event()     # set when enc_worker exits (unblocks dl_worker)

            def _stopping() -> bool:
                return abort.is_set() or _cancel_requested.is_set() or enc_gone.is_set()

            async def _put_file(item) -> bool:
                """Hand a downloaded MKV to the encoder over the maxsize=1 queue,
                but never block forever if the encoder has stopped consuming
                (abort, cancel, or encoder exited). Returns False if we gave up
                without enqueuing. Fixes the dl_worker↔enc_worker deadlock (H-7)."""
                while not _stopping():
                    try:
                        file_q.put_nowait(item)
                        return True
                    except asyncio.QueueFull:
                        await asyncio.sleep(0.1)
                return False

            async def _send_sentinel():
                """Deliver the None stop-sentinel to enc_worker so it can break out
                of file_q.get(). Keep trying while the encoder is alive (even on
                cancel/abort — it still needs the sentinel to exit), but give up the
                moment it has already exited so we never block on a full queue."""
                while not enc_gone.is_set():
                    try:
                        file_q.put_nowait(None)
                        return
                    except asyncio.QueueFull:
                        await asyncio.sleep(0.1)

            async def dl_worker():
                global current_dl_process
                try:
                    for vid_idx, vid_url in enumerate(video_urls, 1):
                        if _stopping():
                            break
                        dl_label = "[DL {}/{}] ".format(vid_idx, total_videos)

                        # Per-item lookups (batch mode) vs request-level scalars.
                        cfg = per_item[vid_idx - 1] if batch else None
                        vf = cfg["video_format"] if cfg else video_format
                        af = cfg["audio_format"] if cfg else audio_format
                        item_dest = Path(cfg["output_dir"]) if (cfg and cfg["output_dir"]) else dest_dir
                        item_dest.mkdir(parents=True, exist_ok=True)
                        exp_bytes = cfg["expected_size"] if cfg else (int(expected_size) if expected_size else 0)

                        # Skip download if output MP4 already exists and looks complete
                        min_output = max(1024 * 1024, int(exp_bytes * 0.30))
                        vid_title = video_titles.get(vid_url, "")
                        if vid_title:
                            predicted = item_dest / "{}_h265.mp4".format(_predict_output_stem(vid_title))
                            if predicted.exists() and predicted.stat().st_size >= min_output:
                                await msg_q.put(sse_log("{}Skipping \u2014 already encoded: {}".format(dl_label, predicted.name)))
                                if batch:
                                    await msg_q.put(sse_item_done(vid_idx, total_videos))
                                continue

                        # NOTE: pipeline mode only — do NOT emit a `phase` event here.
                        # The main progress bar tracks the concurrent ENCODE (sse_progress);
                        # pipeline download progress is shown separately via sse_dl_progress
                        # (the dl-status-row). A `phase` event would flip the main bar to
                        # indeterminate/scrolling with nothing to clear it during the download.
                        # The title still surfaces here in the log and in the dl-status-row.
                        if vid_title:
                            await msg_q.put(sse_log("{}Downloading: {}".format(dl_label, vid_title)))
                        else:
                            await msg_q.put(sse_log("{}Downloading...".format(dl_label)))

                        dl_args = [
                            str(get_ytdlp()),
                            "-f", "{}+{}".format(vf, af),
                            *NODE_ARGS,
                            "--no-playlist",
                            "--no-overwrites",
                            "--merge-output-format", "mkv",
                            "--concurrent-fragments", "2",
                            "--retries", "25",
                            "--fragment-retries", "25",
                            "--newline",
                            "--restrict-filenames",
                            "--paths", str(CACHE_DIR),
                            "-o", "%(title)s.%(ext)s",
                        ]
                        dl_args += cookie_args()
                        dl_args += ["--", vid_url]

                        proc = await asyncio.create_subprocess_exec(
                            *dl_args,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.STDOUT,
                        )
                        current_dl_process = proc
                        output_paths = set()

                        async for raw in proc.stdout:
                            if _stopping():
                                proc.terminate()
                                break
                            line = raw.decode(errors="replace").rstrip()
                            _cw = _maybe_flag_stale_cookies(line)
                            if _cw:
                                await msg_q.put(sse_cookie_warning(_cw))
                            m = re.search(
                                r"\[download\]\s+([\d.]+)%\s+of\s+(\S+)\s+at\s+(.+?)\s+ETA\s+(\S+)",
                                line
                            )
                            if m:
                                dl_pct, dl_size, dl_speed, dl_eta = m.groups()
                                await msg_q.put(sse_dl_progress(float(dl_pct), dl_size, dl_speed, dl_eta, vid_idx, total_videos))
                            else:
                                merger = re.search(r'\[Merger\] Merging formats into "(.+?)"', line)
                                already = re.search(r'\[download\] (.+?) has already been downloaded', line)
                                if merger:
                                    output_paths.add(merger.group(1))
                                elif already:
                                    output_paths.add(already.group(1))
                                await msg_q.put(sse_log(line))

                        await proc.wait()
                        current_dl_process = None

                        if _cancel_requested.is_set():
                            break   # user cancelled — not a download failure

                        mkv_files = []
                        if output_paths:
                            mkv_files = sorted(
                                [Path(p) for p in output_paths if Path(p).exists() and p.endswith(".mkv")],
                                key=os.path.getmtime
                            )
                            if not mkv_files:
                                for p in output_paths:
                                    candidate = Path(p) if Path(p).suffix else Path(p).with_suffix(".mkv")
                                    if candidate.exists():
                                        mkv_files = [candidate]
                                        break
                        if not mkv_files:
                            mkv_files = sorted(CACHE_DIR.glob("*.mkv"), key=os.path.getmtime)

                        expected_dur = video_durations.get(vid_url) or 0

                        if proc.returncode != 0:
                            if mkv_files and await _download_is_complete(mkv_files[0], exp_bytes, expected_dur):
                                await msg_q.put(sse_log("{}yt-dlp exited with errors but output file verified complete \u2014 continuing.".format(dl_label)))
                            else:
                                # Batch: skip this bad video and keep going; single/playlist: abort.
                                await msg_q.put(sse_item_failed(vid_idx, total_videos, "download failed"))
                                size_note = ""
                                if mkv_files and exp_bytes > 0:
                                    actual = mkv_files[0].stat().st_size
                                    size_note = " (got {:.0f}MB of expected {:.0f}MB)".format(actual / 1024**2, exp_bytes / 1024**2)
                                if not batch:
                                    abort.set()
                                    await msg_q.put(sse_error("{}Download failed{}.".format(dl_label, size_note)))
                                    break
                                await msg_q.put(sse_error("{}Download failed{} \u2014 skipping.".format(dl_label, size_note)))
                                continue

                        if not mkv_files:
                            await msg_q.put(sse_item_failed(vid_idx, total_videos, "no mkv"))
                            if not batch:
                                abort.set()
                                await msg_q.put(sse_error("{}No .mkv found after download.".format(dl_label)))
                                break
                            await msg_q.put(sse_error("{}No .mkv found after download \u2014 skipping.".format(dl_label)))
                            continue

                        await msg_q.put(sse_log("{}Queued for encoding.".format(dl_label)))
                        if not await _put_file((vid_idx, mkv_files[0])):
                            break   # encoder stopped consuming (abort/cancel) — don't hang

                        if vid_idx < total_videos and not _stopping():
                            sleep_s = random.uniform(3, 8)
                            await msg_q.put(sse_log("Next download in {:.1f}s...".format(sleep_s)))
                            await asyncio.sleep(sleep_s)

                finally:
                    # Always make sure the encoder receives its stop-sentinel (even on
                    # cancel/abort it may be idle on file_q.get()), but without blocking
                    # if it has already exited. Its own finally sends the msg_q sentinel
                    # the main loop counts.
                    await _send_sentinel()
                    await msg_q.put(None)

            async def enc_worker():
                global current_process
                consecutive_failures = 0
                try:
                    while True:
                        item = await file_q.get()
                        if item is None:
                            break
                        if _cancel_requested.is_set():
                            break
                        vid_idx, input_file = item
                        await msg_q.put(sse_video_start(vid_idx, total_videos))
                        enc_label = "[ENC {}/{}] ".format(vid_idx, total_videos)

                        # Per-item output-dir + tune (batch mode) vs request-level.
                        cfg = per_item[vid_idx - 1] if batch else None
                        item_dest = Path(cfg["output_dir"]) if (cfg and cfg["output_dir"]) else dest_dir
                        item_tune = cfg["tune_mode"] if cfg else tune_mode
                        item_dest.mkdir(parents=True, exist_ok=True)

                        safe_stem = sanitize(input_file.stem)
                        output_file = item_dest / "{}_h265.mp4".format(safe_stem)
                        source_size = input_file.stat().st_size if input_file.exists() else 0
                        min_output = max(1024 * 1024, int(source_size * 0.30))

                        if output_file.exists():
                            out_size = output_file.stat().st_size
                            if out_size < min_output:
                                await msg_q.put(sse_log("{}Removing incomplete output ({:.0f} MB, expected >={:.0f} MB) \u2014 re-encoding.".format(enc_label, out_size / 1024**2, min_output / 1024**2)))
                                output_file.unlink()
                            else:
                                await msg_q.put(sse_log("{}Skipping \u2014 output already exists: {} ({:.0f} MB)".format(enc_label, output_file.name, out_size / 1024**2)))
                                if delete_cache == "true":
                                    input_file.unlink(missing_ok=True)
                                if batch:
                                    await msg_q.put(sse_item_done(vid_idx, total_videos))
                                continue

                        await msg_q.put(sse_phase("{}Converting: {}".format(enc_label, input_file.name)))
                        await msg_q.put(sse_source_size(source_size))

                        src = await probe_video(input_file)
                        duration_secs = src.get("duration") or None
                        if duration_secs:
                            await msg_q.put(sse_log("Duration: {:.1f}s".format(duration_secs)))
                        else:
                            await msg_q.put(sse_log("Could not read duration \u2014 progress bar will be indeterminate"))

                        effective_tune = "hq" if item_tune == "hq" else _nvenc_tune
                        params = calc_encode_params(
                            height=src.get("height", 1080),
                            fps=src.get("fps", 30.0),
                            video_bitrate_kbps=src.get("video_bitrate_kbps", 0),
                            codec=src.get("codec", "h264"),
                            pix_fmt=src.get("pix_fmt", "yuv420p"),
                            color_transfer=src.get("color_transfer", ""),
                            cq_override=int(cq) if cq.isdigit() else 0,
                            maxrate_override=maxrate,
                            nvenc_tune=effective_tune,
                        )
                        enc_log = "Encode params: CQ={} maxrate={} preset={} tune={} pix={}".format(
                            params["cq"], params["maxrate"], params["preset"], effective_tune, params["pix_fmt"]
                        )
                        await msg_q.put(sse_log(enc_log))

                        enc_args = build_video_ffmpeg_args(
                            input_file, output_file, params, effective_tune, src.get("codec", "")
                        )
                        enc_res: dict = {}
                        async for _s in run_encode(enc_args, duration_secs=duration_secs,
                                                   idx=vid_idx, total=total_videos, result=enc_res):
                            await msg_q.put(_s)

                        if _cancel_requested.is_set():
                            # User cancelled mid-encode — discard the partial output,
                            # don't treat it as a failure or retry.
                            if output_file.exists():
                                output_file.unlink(missing_ok=True)
                            break

                        if enc_res["returncode"] != 0:
                            # Remove partial output so retries don't falsely skip.
                            if output_file.exists():
                                output_file.unlink(missing_ok=True)
                            consecutive_failures += 1
                            # H-6: a single encode failure must NOT abort the pipeline —
                            # that would kill dl_worker's in-flight download. Surface the
                            # error and move on to the next queued MKV. Only give up once
                            # the encoder looks fundamentally broken (N in a row).
                            await msg_q.put(sse_error("{}Conversion failed: {}".format(enc_label, input_file.name)))
                            await msg_q.put(sse_item_failed(vid_idx, total_videos, "encode failed"))
                            if consecutive_failures >= _MAX_CONSECUTIVE_ENCODE_FAILURES:
                                abort.set()
                                await msg_q.put(sse_error("Aborting pipeline: {} consecutive encode failures.".format(consecutive_failures)))
                                break
                            continue

                        consecutive_failures = 0
                        source_bytes = input_file.stat().st_size if input_file.exists() else 0
                        if delete_cache == "true":
                            input_file.unlink(missing_ok=True)
                        output_bytes = output_file.stat().st_size
                        size_mb = output_bytes / 1024 ** 2
                        cache_note = "cache cleared" if delete_cache == "true" else "cache kept"
                        await msg_q.put(sse_log("Done: {} \u2014 {:.0f} MB ({})".format(output_file.name, size_mb, cache_note)))
                        if source_bytes > 0:
                            await msg_q.put(sse_size_info(source_bytes, output_bytes))
                        await msg_q.put(sse_item_done(vid_idx, total_videos))

                finally:
                    # Unblock any pending _put_file so dl_worker can't hang on the
                    # maxsize=1 queue now that we're gone (H-7). Distinct from `abort`
                    # so a clean finish still reports success.
                    enc_gone.set()
                    # Drain queue without deleting — queued MKVs may still be valid.
                    while True:
                        try:
                            file_q.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    await msg_q.put(None)

            dl_task = asyncio.create_task(dl_worker())
            enc_task = asyncio.create_task(enc_worker())

            done_count = 0
            while done_count < 2:
                msg = await msg_q.get()
                if msg is None:
                    done_count += 1
                    continue
                yield msg

            # return_exceptions: a worker dying on an unexpected error must surface as
            # a clean error event, not crash the SSE stream (both workers already sent
            # their msg_q sentinel via finally, so the loop above has exited).
            for _r in await asyncio.gather(dl_task, enc_task, return_exceptions=True):
                if isinstance(_r, Exception):
                    abort.set()
                    yield sse_error("Pipeline worker error: {}".format(_r))

            if _cancel_requested.is_set():
                yield sse_cancelled('Cancelled by user.')
            elif not abort.is_set():
                yield sse_done('All done!')
                if shutdown == "true":
                    # Shutdown is client-driven: emit the event, the UI runs the
                    # visible countdown and calls POST /shutdown-now itself.
                    yield sse_shutdown('Shutting down in 60 seconds...')

        else:
            # ── Sequential: download → convert → delete, one at a time ─────────
            for vid_idx, vid_url in enumerate(video_urls, 1):
                if _cancel_requested.is_set():
                    break   # cancel stops the whole queue, not just the current proc
                label = f"[{vid_idx}/{total_videos}] " if total_videos > 1 else ""
                yield sse_video_start(vid_idx, total_videos)

                # Skip download if output already exists and looks complete.
                # Video: H.265 is 40-100% of source — 30% floor. Audio: WAV/MP3 size
                # is unrelated to source bytes (WAV often >> source), so existence + 1MB suffices.
                expected_bytes = int(expected_size) if expected_size else 0
                min_output = 1024 * 1024 if is_audio else max(1024 * 1024, int(expected_bytes * 0.30))
                vid_title = video_titles.get(vid_url, "")
                if vid_title:
                    predicted = _predicted_output(_predict_output_stem(vid_title))
                    if predicted.exists() and predicted.stat().st_size >= min_output:
                        yield sse_log(f'{label}Skipping \u2014 already encoded: {predicted.name}')
                        continue

                yield sse_phase("{}Downloading".format(label), vid_title)

                dl_args = [
                    str(get_ytdlp()),
                    *_yt_format_args(),
                    *NODE_ARGS,
                    "--no-playlist",
                    "--no-overwrites",
                    *_yt_merge_args(),
                    "--concurrent-fragments", "2",
                    "--retries", "25",
                    "--fragment-retries", "25",
                    "--newline",
                    "--restrict-filenames",
                    "--paths", str(CACHE_DIR),
                    "-o", "%(title)s.%(ext)s",
                ]
                dl_args += cookie_args()
                dl_args += ["--", vid_url]

                proc = await asyncio.create_subprocess_exec(
                    *dl_args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                current_process = proc
                output_paths = set()

                async for raw in proc.stdout:
                    if _cancel_requested.is_set():
                        proc.terminate()
                        break
                    line = raw.decode(errors="replace").rstrip()
                    _cw = _maybe_flag_stale_cookies(line)
                    if _cw:
                        yield sse_cookie_warning(_cw)
                    m = re.search(
                        r"\[download\]\s+([\d.]+)%\s+of\s+([\S]+)\s+at\s+([\S]+)\s+ETA\s+([\S]+)",
                        line
                    )
                    if m:
                        pct, size, speed, eta = m.groups()
                        yield _sse({"type": "progress", "pct": float(pct), "size": size, "speed": speed, "eta": eta})
                    else:
                        merger = re.search(r'\[Merger\] Merging formats into "(.+?)"', line)
                        # Audio mode has no merger — capture the destination line instead.
                        dest = re.search(r'\[download\] Destination: (.+)', line) if is_audio else None
                        already = re.search(r'\[download\] (.+?) has already been downloaded', line)
                        if merger:
                            output_paths.add(merger.group(1))
                        elif dest:
                            output_paths.add(dest.group(1).strip())
                        elif already:
                            output_paths.add(already.group(1))
                        yield sse_log(line)

                await proc.wait()
                current_process = None

                if _cancel_requested.is_set():
                    break   # user cancelled — not a download failure

                # Resolve downloaded file(s) before checking return code — yt-dlp can
                # exit non-zero from transient fragment errors even when the file was
                # fully downloaded.
                allowed_suffixes = _AUDIO_EXTS if is_audio else {".mkv"}
                mkv_files = []
                if output_paths:
                    mkv_files = sorted(
                        [Path(p) for p in output_paths if Path(p).exists() and Path(p).suffix.lower() in allowed_suffixes],
                        key=os.path.getmtime
                    )
                    if not mkv_files and not is_audio:
                        for p in output_paths:
                            candidate = Path(p) if Path(p).suffix else Path(p).with_suffix('.mkv')
                            if candidate.exists():
                                mkv_files = [candidate]
                                break
                if not mkv_files:
                    mkv_files = _glob_downloaded(CACHE_DIR)

                expected_bytes = int(expected_size) if expected_size else 0
                expected_dur = video_durations.get(vid_url) or 0

                if proc.returncode != 0:
                    if mkv_files and await _download_is_complete(mkv_files[0], expected_bytes, expected_dur):
                        yield sse_log(f'{label}yt-dlp exited with errors but output file verified complete — continuing.')
                    else:
                        size_note = ""
                        if mkv_files and expected_bytes > 0:
                            actual = mkv_files[0].stat().st_size
                            size_note = " (got {:.0f}MB of expected {:.0f}MB)".format(actual / 1024**2, expected_bytes / 1024**2)
                        yield sse_error("{}Download failed{}.".format(label, size_note))
                        return

                if not mkv_files:
                    yield sse_error(f'{label}No .mkv found after download.')
                    return

                if convert == "true" or is_audio:
                    input_file = mkv_files[0]
                    source_size = input_file.stat().st_size
                    safe_stem = sanitize(input_file.stem)
                    output_file = _predicted_output(safe_stem)

                    # Video: H.265 should be at least 30% of source. Audio: WAV/MP3 size
                    # is unrelated to source bytes, so just require existence + 1 MB.
                    min_output = 1024 * 1024 if is_audio else max(1024 * 1024, int(source_size * 0.30))

                    if output_file.exists():
                        out_size = output_file.stat().st_size
                        if out_size < min_output:
                            msg = "{}Removing incomplete output ({:.0f} MB, expected >={:.0f} MB) — re-encoding.".format(
                                label, out_size / 1024**2, min_output / 1024**2)
                            yield sse_log(msg)
                            output_file.unlink()
                        else:
                            msg = "{}Skipping \u2014 output already exists: {} ({:.0f} MB)".format(label, output_file.name, out_size / 1024**2)
                            yield sse_log(msg)
                            if delete_cache == "true":
                                input_file.unlink(missing_ok=True)
                            continue

                    phase_verb = "Extracting audio" if is_audio else "Converting"
                    yield sse_phase(f'{label}{phase_verb}: {input_file.name}')
                    yield sse_source_size(source_size)

                    src = await probe_video(input_file)
                    duration_secs = src.get("duration") or None
                    if duration_secs:
                        yield sse_log(f'Duration: {duration_secs:.1f}s')
                    else:
                        yield sse_log('Could not read duration — progress bar will be indeterminate')

                    if is_audio:
                        ffmpeg_args = build_audio_ffmpeg(input_file, output_file, audio_preset)
                        enc_log = "Audio extract: {} → {}".format(audio_preset.upper(), output_file.name)
                        yield sse_log(enc_log)
                    else:
                        effective_tune = "hq" if tune_mode == "hq" else _nvenc_tune
                        params = calc_encode_params(
                            height=src.get("height", 1080),
                            fps=src.get("fps", 30.0),
                            video_bitrate_kbps=src.get("video_bitrate_kbps", 0),
                            codec=src.get("codec", "h264"),
                            pix_fmt=src.get("pix_fmt", "yuv420p"),
                            color_transfer=src.get("color_transfer", ""),
                            cq_override=int(cq) if cq.isdigit() else 0,
                            maxrate_override=maxrate,
                            nvenc_tune=effective_tune,
                        )
                        enc_log = "Encode params: CQ={} maxrate={} preset={} tune={} pix={}".format(
                            params["cq"], params["maxrate"], params["preset"], effective_tune, params["pix_fmt"]
                        )
                        yield sse_log(enc_log)

                        ffmpeg_args = build_video_ffmpeg_args(
                            input_file, output_file, params, effective_tune, src.get("codec", "")
                        )

                    enc_res: dict = {}
                    async for _s in run_encode(ffmpeg_args, duration_secs=duration_secs,
                                               idx=vid_idx, total=total_videos, result=enc_res):
                        yield _s

                    if _cancel_requested.is_set():
                        if output_file.exists():
                            output_file.unlink(missing_ok=True)
                        break   # user cancelled mid-encode — not a failure
                    if enc_res["returncode"] != 0:
                        # Remove partial output so retries don't falsely skip
                        if output_file.exists():
                            output_file.unlink(missing_ok=True)
                        fail_verb = "Audio extract" if is_audio else "Conversion"
                        yield sse_error(f'{label}{fail_verb} failed: {input_file.name}')
                        return

                    source_bytes = input_file.stat().st_size
                    if delete_cache == "true":
                        input_file.unlink(missing_ok=True)
                    output_bytes = output_file.stat().st_size
                    size_mb = output_bytes / 1024 ** 2
                    cache_note = "cache cleared" if delete_cache == "true" else "cache kept"
                    yield sse_log("Done: {} \u2014 {:.0f} MB ({})".format(output_file.name, size_mb, cache_note))
                    if source_bytes > 0:
                        yield sse_size_info(source_bytes, output_bytes)

                else:
                    for f in mkv_files:
                        yield sse_log(f'Saved (no conversion): {f}')

            if _cancel_requested.is_set():
                yield sse_cancelled('Cancelled by user.')
            else:
                yield sse_done('All done!')
                if shutdown == "true":
                    # Client-driven: UI runs the visible countdown and calls /shutdown-now.
                    yield sse_shutdown('Shutting down in 60 seconds...')

    return StreamingResponse(_tracked(stream()), media_type="text/event-stream")


@app.post("/cancel")
async def cancel():
    global current_process, current_dl_process
    # Sticky flag first: the running job loops poll this to stop launching the
    # next item / retry, even for processes not captured in the globals yet.
    _cancel_requested.set()
    cancelled = False
    for proc in [current_process, current_dl_process]:
        if proc:
            try:
                proc.terminate()
            except Exception:
                pass
            cancelled = True
    current_process = None
    current_dl_process = None
    return {"status": "cancelled" if cancelled else "nothing running"}


@app.post("/probe-file")
async def probe_file(path: str = Form(...)):
    p = Path(path)
    if not p.exists():
        return JSONResponse({"error": "File not found"}, status_code=400)
    proc = await asyncio.create_subprocess_exec(
        get_ffprobe(), "-v", "quiet",
        "-print_format", "json",
        "-show_streams", "-select_streams", "v:0",
        str(p),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    try:
        stream = json.loads(out)["streams"][0]
        height = stream.get("height", 0)
        num, den = stream.get("r_frame_rate", "30/1").split("/")
        fps = int(num) / max(int(den), 1)
        return {"height": height, "fps": round(fps, 3)}
    except Exception:
        return JSONResponse({"error": "Could not probe file"}, status_code=400)


@app.post("/scan-folder")
async def scan_folder(folder: str = Form(...)):
    p = Path(folder)
    if not p.exists() or not p.is_dir():
        return JSONResponse({"error": f"Folder not found: {folder}"}, status_code=400)
    extensions = {".mkv", ".mp4", ".webm", ".avi", ".mov", ".m4v"}
    files = [
        {"name": f.name, "path": str(f), "size_gb": round(f.stat().st_size / 1024**3, 2)}
        for f in sorted(p.iterdir())
        if f.suffix.lower() in extensions
    ]
    return {"files": files, "folder": str(p)}


@app.post("/convert-local")
async def convert_local(
    files: str = Form(...),       # JSON array of absolute file paths
    cq: str = Form("0"),
    maxrate: str = Form(""),
    shutdown: str = Form("false"),
    output_dir: str = Form(""),
    tune_mode: str = Form("uhq"),
    mode: str = Form("video"),       # "video" → H.265 MP4; "audio" → WAV/MP3 extract
    audio_preset: str = Form("mp3"), # "wav" or "mp3"; only used when mode == "audio"
):
    async def stream():
        global current_process
        _cancel_requested.clear()   # fresh job — drop any stale cancel
        _keep_awake()
        _job_start = datetime.datetime.now()
        _job_lines = []

        def _jlog(level, msg):
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            _job_lines.append("[{}] [{}] {}".format(ts, level, msg))

        try:
            async for chunk in _convert_local_stream():
                if chunk.startswith("data: "):
                    try:
                        evt = json.loads(chunk[6:].strip())
                        t = evt.get("type", "")
                        if t == "phase":
                            _jlog("PHASE", evt.get("msg", ""))
                        elif t == "log":
                            _jlog("LOG", evt.get("msg", ""))
                        elif t == "error":
                            _jlog("ERROR", evt.get("msg", ""))
                        elif t == "done":
                            _jlog("DONE", evt.get("msg", ""))
                        elif t == "video_start":
                            _jlog("VIDEO", "Video {current}/{total}".format(**evt))
                        elif t == "size_info":
                            _jlog("SIZE", "source={source_mb}MB  output={output_mb}MB  bloat={bloat_pct:+.1f}%".format(**evt))
                    except Exception:
                        pass
                yield chunk
        finally:
            elapsed = datetime.datetime.now() - _job_start
            status = "CANCELLED"
            for ln in reversed(_job_lines):
                if "[ERROR]" in ln:
                    status = "FAILED"
                    break
                if "[DONE]" in ln:
                    status = "SUCCESS"
                    break
            ts_file = _job_start.strftime("%Y-%m-%d_%H-%M-%S")
            log_path = LOGS_DIR / "{}_local.txt".format(ts_file)
            try:
                file_list = json.loads(files)
            except Exception:
                file_list = [files]
            header_lines = [
                "=== FetchForge Job Log ===",
                "Started : {}".format(_job_start.strftime("%Y-%m-%d %H:%M:%S")),
                "Type    : Local Convert",
                "Files   : {}".format(", ".join(str(f) for f in file_list)),
                "Elapsed : {}".format(str(elapsed).split(".")[0]),
                "Status  : {}".format(status),
                "",
                "--- Events ---",
                "",
            ]
            try:
                await asyncio.to_thread(
                    log_path.write_text,
                    "\n".join(header_lines + _job_lines + [""]),
                    encoding="utf-8",
                )
            except Exception as exc:
                logger.warning("Log write failed: %s", exc)
            _allow_sleep()

    async def _convert_local_stream():
        global current_process

        is_audio = mode == "audio"
        out_ext = audio_output_ext(audio_preset) if is_audio else "mp4"
        out_suffix = "" if is_audio else "_h265"

        file_paths = [Path(f) for f in json.loads(files)]
        file_paths = [f for f in file_paths if f.exists()]
        total = len(file_paths)

        if not file_paths:
            yield sse_error('No valid files found.')
            return

        custom_dest = Path(output_dir) if output_dir else None
        if custom_dest:
            custom_dest.mkdir(parents=True, exist_ok=True)

        action_label = "audio extraction" if is_audio else "conversion"
        yield sse_log(f'{total} file(s) queued for {action_label}.')

        for idx, input_file in enumerate(file_paths, 1):
            if _cancel_requested.is_set():
                break   # cancel stops the whole queue
            yield sse_video_start(idx, total)
            dest_dir = custom_dest or (input_file.parent / "converted")
            dest_dir.mkdir(parents=True, exist_ok=True)
            safe_stem = sanitize(input_file.stem)
            output_file = dest_dir / "{}{}.{}".format(safe_stem, out_suffix, out_ext)

            if output_file.exists():
                size_mb = output_file.stat().st_size / 1024 ** 2
                if size_mb < 1:
                    yield sse_log(f'Removing incomplete output ({size_mb * 1024:.0f} KB) — re-encoding.')
                    output_file.unlink()
                else:
                    yield sse_log(f'Skipping — output already exists: {output_file.name} ({size_mb:.0f} MB)')
                    continue

            phase_verb = "Extracting audio" if is_audio else "Converting"
            yield sse_phase(f'{phase_verb} {idx}/{total}: {input_file.name}')
            _src_bytes = input_file.stat().st_size if input_file.exists() else 0
            yield sse_source_size(_src_bytes)

            # Deep-probe source for smart encode parameters
            src = await probe_video(input_file)
            duration_secs = src.get("duration") or None
            if duration_secs:
                yield sse_log(f'Duration: {duration_secs:.1f}s')

            if is_audio:
                ffmpeg_args = build_audio_ffmpeg(input_file, output_file, audio_preset)
                enc_log = "Audio extract: {} → {}".format(audio_preset.upper(), output_file.name)
                yield sse_log(enc_log)
            else:
                effective_tune = "hq" if tune_mode == "hq" else _nvenc_tune
                params = calc_encode_params(
                    height=src.get("height", 1080),
                    fps=src.get("fps", 30.0),
                    video_bitrate_kbps=src.get("video_bitrate_kbps", 0),
                    codec=src.get("codec", "h264"),
                    pix_fmt=src.get("pix_fmt", "yuv420p"),
                    color_transfer=src.get("color_transfer", ""),
                    cq_override=int(cq) if cq.isdigit() else 0,
                    maxrate_override=maxrate,
                    nvenc_tune=effective_tune,
                )
                enc_log = "Encode params: CQ={} maxrate={} preset={} tune={} pix={}".format(
                    params["cq"], params["maxrate"], params["preset"], effective_tune, params["pix_fmt"]
                )
                yield sse_log(enc_log)

                ffmpeg_args = build_video_ffmpeg_args(
                    input_file, output_file, params, effective_tune, src.get("codec", "")
                )

            enc_res: dict = {}
            async for _s in run_encode(ffmpeg_args, duration_secs=duration_secs,
                                       idx=idx, total=total, result=enc_res):
                yield _s

            if _cancel_requested.is_set():
                if output_file.exists():
                    output_file.unlink(missing_ok=True)
                break   # user cancelled mid-encode — not a failure
            if enc_res["returncode"] != 0:
                # Remove partial output so retries don't falsely skip
                if output_file.exists():
                    output_file.unlink(missing_ok=True)
                fail_verb = "Audio extract" if is_audio else "Conversion"
                yield sse_error(f'{fail_verb} failed: {input_file.name}')
                return

            source_bytes = input_file.stat().st_size
            output_bytes = output_file.stat().st_size
            yield sse_log(f'Done: {output_file.name}')
            if source_bytes > 0:
                yield sse_size_info(source_bytes, output_bytes)

        if _cancel_requested.is_set():
            yield sse_cancelled('Cancelled by user.')
        else:
            yield sse_done('All done!')
            if shutdown == "true":
                # Client-driven: UI runs the visible countdown and calls /shutdown-now.
                yield sse_shutdown('Shutting down in 60 seconds...')

    return StreamingResponse(_tracked(stream()), media_type="text/event-stream")


def _open_folder_dialog(title: str = "Select Output Folder") -> str:
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.askdirectory(title=title)
    root.destroy()
    return path or ""


def _open_file_dialog() -> list:
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    extensions = [".mkv", ".mp4", ".webm", ".avi", ".mov", ".m4v"]
    filetypes = [("Video files", " ".join("*" + e for e in extensions)), ("All files", "*.*")]
    paths = filedialog.askopenfilenames(title="Select Video Files", filetypes=filetypes)
    root.destroy()
    return list(paths) if paths else []


@app.get("/version")
async def get_version():
    return {"version": APP_VERSION}


@app.get("/heartbeat")
async def heartbeat():
    """Keep-alive ping from the open browser tab. When pings stop (tab closed),
    the watchdog self-exits the server after _HEARTBEAT_TIMEOUT seconds."""
    global _last_heartbeat
    _last_heartbeat = time.monotonic()
    return {"ok": True, "timeout": _HEARTBEAT_TIMEOUT}


@app.get("/browse-folder")
async def browse_folder():
    loop = asyncio.get_event_loop()
    path = await loop.run_in_executor(None, _open_folder_dialog)
    return {"path": path}


@app.get("/browse-files")
async def browse_files():
    loop = asyncio.get_event_loop()
    paths = await loop.run_in_executor(None, _open_file_dialog)
    files = []
    for p in paths:
        fp = Path(p)
        if fp.exists():
            files.append({"name": fp.name, "path": str(fp), "size_gb": round(fp.stat().st_size / 1024**3, 2)})
    return {"files": files}


@app.get("/browse-source-folder")
async def browse_source_folder():
    loop = asyncio.get_event_loop()
    path = await loop.run_in_executor(None, lambda: _open_folder_dialog("Select Folder to Scan"))
    return {"path": path}


@app.post("/shutdown-now")
async def shutdown_now(request: Request):
    """Immediate system shutdown — called by the UI after its 60s countdown.
    Requires the per-session token (in addition to the global origin guard) so
    that even a same-origin mishap can't power off the machine accidentally."""
    if request.headers.get("x-dlpr-token") != _SESSION_TOKEN:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    _poweroff()
    return {"ok": True}


def run_server(open_browser: bool = True) -> None:
    global _uvicorn_server
    _ensure_runtime_dirs()   # create cache/downloads/logs before anything writes to them
    _setup_logging()         # deferred here (not import time) so a bare import is side-effect-free
    logger.info("Starting FetchForge at http://localhost:8765")
    if open_browser:
        import threading, webbrowser
        threading.Timer(1.5, lambda: webbrowser.open("http://localhost:8765")).start()
    config = uvicorn.Config(app, host="127.0.0.1", port=8765)
    _uvicorn_server = uvicorn.Server(config)
    try:
        _uvicorn_server.run()
    except Exception:
        # On a headless (no-console) launch this is the only place the failure is
        # visible — make sure it lands in logs/server.log before we die.
        logger.exception("Server exited with an unhandled exception")
        raise


if __name__ == "__main__":
    import sys
    from fetchforge.cli import main
    sys.exit(main())
