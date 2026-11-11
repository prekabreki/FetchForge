"""Runtime paths, logging and OS-level helpers.

``PKG_DIR`` is the read-only package directory (code + ``index.html``);
``STATE_DIR`` is whatever directory ``fetchforge`` was launched from and holds
everything writable. Nothing here has import-time filesystem side effects —
``_ensure_runtime_dirs`` / ``_setup_logging`` are called from ``run_server``.
"""
import logging
import os
import shutil
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import fetchforge

APP_VERSION = fetchforge.__version__

IS_WINDOWS = sys.platform.startswith("win")

PKG_DIR = Path(__file__).parent          # code + index.html template (read-only)
STATE_DIR = Path.cwd()                    # all writable runtime state

# Vendored webfonts, served by /fonts/{name} so the UI never touches a CDN.
# The allowlist is built once from the shipped directory and matched exactly:
# no StaticFiles mount, so nothing outside these filenames is reachable and
# path traversal has nothing to traverse into.
FONTS_DIR = PKG_DIR / "fonts"
FONT_FILES = frozenset(p.name for p in FONTS_DIR.glob("*.woff2")) if FONTS_DIR.is_dir() else frozenset()

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
