---
description: What FetchForge is, how it runs, and the load-bearing constraints
type: project
---

FetchForge is a local single-user Python/FastAPI web app for downloading YouTube videos
(single or playlist) via yt-dlp and converting them to H.265/MP4 with NVENC
(`hevc_nvenc`), or extracting audio to WAV/MP3. It serves a single-file vanilla-JS
frontend at `http://localhost:8765`. Backend is `fetchforge/server.py` (all async);
frontend is `fetchforge/index.html` (no build step, SSE-driven). See `CLAUDE.md` for the
full architecture.

Key constraints a contributor must know on day one:

- **Originally Windows-only, now cross-platform** via runtime OS detection. `launch.sh`
  is the Linux/macOS launcher (bootstraps `.venv`, installs the package, opens the
  browser, runs the server); `launch.bat` / `launch-hidden.vbs` are the Windows twins.
  The packaged route is `pip install fetchforge` → `fetchforge` (or `python -m fetchforge`).
- **Heartbeat auto-quit:** the open tab pings `/heartbeat` every 5s; the server self-exits
  after 30s of silence — UNLESS a `/download`/`/convert-local` SSE stream is active
  (`_active_jobs`) or one finished within `_INTER_ITEM_GRACE` (120s). This guard exists
  because browsers throttle background-tab timers and were killing the server mid-queue.
- **NVENC has sharp edges** (documented in `CLAUDE.md`): `uhq` tune rejects explicit
  `-bf`/AQ flags on SDK 12+; 4K maxrate is hard-capped at 20M; never pair non-zero `-b:v`
  with `-cq`. Violations surface as `InitializeEncoder failed: invalid param (8)`.
- **The job queue is entirely client-side** (`dlQueue` in `fetchforge/index.html`); the
  server processes one item per SSE request. Shutdown-after-conversion is client-driven.

Issue tracking is **GitHub Issues** (`gh issue list`; `python tools/issue-ready.py` for
ready work).
