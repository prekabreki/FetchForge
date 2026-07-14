# FetchForge

> **Requires an NVIDIA GPU with NVENC.** The H.265 transcode uses hardware
> `hevc_nvenc` and has no CPU-encode fallback — without an NVIDIA card the
> transcode path cannot run (download + audio-extract still work). macOS has no
> NVIDIA GPUs, so the transcode is Windows/Linux only.

FetchForge is a localhost web app that downloads YouTube videos and playlists
and transcodes them to H.265/HEVC using NVIDIA NVENC hardware encoding. Single
videos, full playlists with pipeline mode, and local-file conversion. Smart
auto-analysis picks CQ, maxrate, and preset from the source.

## Install & launch

**With pip (recommended):**

```
pip install fetchforge
fetchforge
```

The browser opens automatically at `http://localhost:8765`. On Windows, if an
NVENC-capable ffmpeg isn't already present, FetchForge downloads one on first
run. On Linux, install ffmpeg from your package manager first
(`sudo dnf install ffmpeg` / `sudo apt install ffmpeg`) and make sure the NVIDIA
driver is loaded. macOS has no NVIDIA GPU, so the H.265 transcode can't run
there, but download and audio-extract still work and just need ffmpeg
(`brew install ffmpeg`).

**Output files land in the current directory** — run `fetchforge` from wherever
you want `downloads/` to appear.

**From source (clone):**

```
git clone https://github.com/prekabreki/fetchforge
cd fetchforge
./launch.sh        # Windows: launch.bat
```

> "Update yt-dlp" in the UI is a no-op for pip installs — update with
> `pip install -U fetchforge` (or `pip install -U "yt-dlp[default]"`).

## Stack

- **Backend** — `fetchforge/server.py` (FastAPI + uvicorn, fully async)
- **Frontend** — `fetchforge/index.html` (single file, vanilla JS + SSE, no build step)
- **CLI entry point** — `fetchforge/cli.py`, exposed as the `fetchforge` console script (also runnable as `python -m fetchforge`)
- **Downloader** — `yt-dlp[default]`, a regular dependency (pulled in by pip, not bundled)
- **Encoder** — ffmpeg + `hevc_nvenc` (NVENC); auto-downloaded to a per-user cache on Windows, expected on `PATH` on Linux
- **Packaging** — `pyproject.toml`; `pip install fetchforge` for the package, `pip install -e .` for an editable clone
- **Launchers** — `launch.bat` (Windows) / `launch.sh` (Linux/macOS) for the clone workflow: create/reuse a venv, `pip install -e .`, open the browser, run `python -m fetchforge`

## What's NOT in the repo (gitignored)

These are restored locally — they're either rebuildable, regenerable, or sensitive.

| Path | Why ignored |
|------|-------------|
| `_internal/` | Optional manual ffmpeg override for a source clone on Windows — pip installs auto-provision ffmpeg into `%LOCALAPPDATA%\FetchForge\ffmpeg` instead |
| `yt-dlp.exe` | Optional manual override; the normal path gets yt-dlp as a pip dependency, updated via `pip install -U`, not a dropped binary |
| `cookies.txt` | YouTube auth cookies (sensitive — never commit) |
| `history.json` | Last 50 downloaded URLs (per-machine state) |
| `logs/` | Runtime logs |
| `downloads/` | Output MP4s |

`cookies.txt`, `history.json`, `logs/`, and `downloads/` are runtime state and
live under the current working directory you launch `fetchforge` from.
`_internal/` and `yt-dlp.exe` are the two exceptions: they're optional manual
overrides FetchForge looks for next to the installed package itself, not the
cwd — the normal pip install path never needs them.

## First-run setup on a new machine

1. `pip install fetchforge`.
2. Run `fetchforge`. On Windows it downloads an NVENC-capable ffmpeg build automatically the first time it's needed. On Linux, install `ffmpeg` from your package manager first and confirm the NVIDIA driver is loaded — FetchForge won't fetch a build for you there.
3. (Optional) Upload a `cookies.txt` via the **Authentication** card if you need age-restricted, private, or members-only content — see [Cookies](#cookies) below for how to get one.

Working from a clone instead: run `./launch.sh` (or `launch.bat` on Windows) — it sets up a `.venv`, installs the package in editable mode, and starts the server the same way.

## Cookies

Public videos need no cookies. Cookies are only necessary for age-restricted,
private, or members-only videos, which require a logged-in YouTube session.

To get a `cookies.txt`:

1. Install a cookies-export browser extension — **Get cookies.txt LOCALLY**
   for Chrome/Edge, or **cookies.txt** for Firefox.
2. Log in to YouTube in that browser.
3. Open the extension on a `youtube.com` tab and export in **Netscape**
   format — that's your `cookies.txt`.
4. Upload the file via the **Authentication** card in the FetchForge UI.

The file grants access to your YouTube account, so treat it as a secret: it's
gitignored and stays on your machine — don't share or commit it. Re-export it
if it stops working; YouTube rotates cookies periodically.

## Features

- **Sequential or pipelined playlist processing** — pipeline mode runs download and encode concurrently with a `maxsize=1` queue (downloader stays at most one video ahead of the encoder)
- **Smart pre-download skip** — checks for existing `<title>_h265.mp4` before downloading, skips if ≥85% of expected size
- **Two encode tunes** — `uhq` (Smart Auto, archival quality) and `hq` (NLE-friendly, ~4–6× speed, capped at source bitrate)
- **HDR / 10-bit passthrough** — auto-detected; outputs `yuv420p10le` + `main10`
- **Wake-lock during jobs** — ref-counted; Windows `SetThreadExecutionState` re-asserted every 30s, Linux holds a `systemd-inhibit` process
- **Post-job shutdown** — UI countdown with cancel button; `shutdown /s /t 0` (Windows) / `systemctl poweroff` (Linux) if not cancelled
- **Local conversion mode** — point at files or folders, batch-encode without downloading

## Repository conventions

- **Issue tracking**: GitHub Issues. Run `python tools/issue-ready.py` for available work (open issues, dependencies satisfied), or `gh issue list`.
- **Personal-use only** — no auth, no multi-user, no cloud deploy.
- **Per-user runtime state is gitignored** (see table above) and preserved locally, never pushed.

See `CLAUDE.md` for full architecture: endpoint table, SSE event types, encode parameter logic, and the Python <3.12 f-string gotcha that bit this codebase.
