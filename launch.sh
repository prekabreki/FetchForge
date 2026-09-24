#!/usr/bin/env bash
# FetchForge — YouTube Downloader & H.265 Converter launcher (Linux/macOS twin of launch.bat).
# Bootstraps a self-contained .venv (fastapi/uvicorn/python-multipart + yt-dlp),
# then runs the server, which opens the browser. The server self-exits ~30s after the
# tab closes (heartbeat watchdog), so nothing lingers.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

VENV=".venv"
if [[ ! -d "$VENV" ]]; then
    echo "Creating virtualenv in $VENV ..."
    "${PYTHON:-python3}" -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

# Install deps on first run (or after a venv wipe). yt-dlp lives in the venv on
# Linux — it replaces the bundled yt-dlp.exe Windows ships. The [default] extra
# pulls in yt-dlp-ejs, the local YouTube JS-challenge solver (version-matched to
# yt-dlp), so n-sig solving works with node without fetching code at runtime.
# The installed-check reads package metadata rather than `pip show`, and the
# install falls back to uv / ensurepip: a venv created by `uv venv`/`uv sync`
# has no pip, and relying on it made the launcher exit before starting.
install_fetchforge() {
    if python -m pip --version >/dev/null 2>&1; then
        python -m pip install -e .
    elif command -v uv >/dev/null 2>&1; then
        uv pip install --python "$VENV/bin/python" -e .
    else
        python -m ensurepip --upgrade && python -m pip install -e .
    fi
}
python -I -c "import importlib.metadata as m; m.version('fetchforge')" >/dev/null 2>&1 \
    || install_fetchforge || {
    echo "Setup failed — ensure Python 3.12+ and pip (or uv) are available." >&2; exit 1; }

# ffmpeg/ffprobe come from the system; warn early if absent.
command -v ffmpeg  >/dev/null 2>&1 || echo "WARNING: ffmpeg not found on PATH — install it (e.g. sudo dnf install ffmpeg)."

# The server (run_server) opens the browser itself once it's listening.
exec python -m fetchforge
