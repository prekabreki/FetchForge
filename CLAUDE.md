# FetchForge — YouTube Download + H.265 Convert Tool

Local Python/FastAPI web app, packaged as the `fetchforge` pip package. Downloads YouTube videos (single or playlist) via yt-dlp, then either converts to H.265/MP4 using NVENC (NVIDIA GPU) or extracts audio to WAV/MP3. Runs at `http://localhost:8765`.

## Stack

- **Backend:** `fetchforge/server.py` — FastAPI + uvicorn, all async
- **Frontend:** `fetchforge/index.html` — single-file, no build step, vanilla JS + SSE, served from `PKG_DIR`
- **CLI entry point:** `fetchforge/cli.py` (`main()`, wired as the `fetchforge` console script) runs `provision.ensure_ffmpeg()` preflight, then imports and calls `server.run_server()`. `fetchforge/__main__.py` calls the same `cli.main()` for `python -m fetchforge`.
- **Provisioning:** `fetchforge/provision.py` — on Windows, auto-downloads an NVENC-capable ffmpeg build into `%LOCALAPPDATA%\FetchForge\ffmpeg` on first run if none is found; on Linux, guides the user to install ffmpeg via the package manager (no auto-download — driver/distro-coupled); macOS has no NVENC and raises `ProvisionError` with guidance (download + audio-extract still work without ffmpeg's NVENC encoder).
- **Runtime paths:** `PKG_DIR = Path(__file__).parent` (installed package code + `index.html`, read-only) vs. `STATE_DIR = Path.cwd()` (writable: `downloads/`, `logs/`, `cookies.txt`, `history.json`) — runtime state always lands in whatever directory you launch `fetchforge` from, not next to the package.
- **Downloader:** yt-dlp — a regular pip dependency (`yt-dlp[default]`, pulled in by `pyproject.toml`), resolved lazily by `get_ytdlp_argv()`, which returns an **argv prefix**, not a path: bundled `yt-dlp.exe` next to `PKG_DIR` on Windows (optional manual override), else PATH, else the interpreter's `sys.prefix/{Scripts,bin}` console script, else `[sys.executable, "-m", "yt_dlp"]` when the module is importable. That last fallback is what makes `--user` and Windows Store Python installs work — pip drops their console scripts in a redirected dir that is on neither PATH nor `sys.prefix`.
- **Encoder:** ffmpeg + NVENC (`hevc_nvenc`) via subprocess, resolved lazily by `get_ffmpeg()`/`get_ffprobe()` → `_resolve_tool()` — bundled `_internal/*.exe` next to `PKG_DIR` on Windows (optional manual override; the auto-provisioned cache under `%LOCALAPPDATA%` is what's normally used instead), else PATH on Linux/macOS. `HAS_SCALE_CUDA` is probed at startup (present on the Windows gyan/BtbN build; absent on Fedora/Nobara ffmpeg → CPU-format fallback path). Being lazy (not module-level/eager) means importing `fetchforge.server` no longer requires ffmpeg to be present — only calling `get_ffmpeg()`/`get_ffprobe()` does.
- **Launch:** `pip install fetchforge` then the `fetchforge` command (or `python -m fetchforge`) is the primary flow. From a source clone, `launch.sh` (Linux/macOS) / `launch.bat` (Windows) bootstrap a `.venv` (created if absent, reused if present; `PYTHON` env-var override selects the interpreter used to *create* it), `pip install -e .` into it, then run `python -m fetchforge` from that venv — the server opens the browser itself once listening. On Windows, a `.venv` created from the Microsoft Store build of Python inherits that interpreter's `%LOCALAPPDATA%` filesystem virtualization, which matters because the NVENC-ffmpeg auto-provision cache also lives under `%LOCALAPPDATA%\FetchForge\ffmpeg`.

## File layout

```
FetchForge/                   # repo root
  pyproject.toml               # package metadata, deps, console-script entry point
  fetchforge/                  # the installed package
    __init__.py                 # __version__
    __main__.py                 # python -m fetchforge → cli.main()
    cli.py                      # fetchforge console entry point (ffmpeg preflight + run)
    provision.py                 # NVENC-ffmpeg detection / Windows auto-download
    server.py                    # entire backend
    index.html                   # entire frontend
    yt-dlp.exe                   # optional manual override binary (gitignored)
    _internal/                   # optional manual ffmpeg/ffprobe override (gitignored)
  launch.bat                   # source-clone entry point (Windows, visible console)
  launch-hidden.vbs             # source-clone entry point (Windows, no console — runs launch.bat hidden)
  launch.sh                    # source-clone entry point (Linux/macOS)
  docs/                        # plan/spec docs (superpowers workflow)
  tests/                       # unittest suite
  cookies.txt                  # optional, uploaded via UI (gitignored — sensitive; lives in STATE_DIR/cwd)
  history.json                 # last 50 downloaded URLs, auto-maintained (gitignored; STATE_DIR/cwd)
  downloads/
    converted/                   # final MP4s land here (gitignored; STATE_DIR/cwd)
  logs/                        # server.log, rotating (gitignored; STATE_DIR/cwd)
```

`yt-dlp.exe` and `_internal/` are optional manual overrides FetchForge looks for next to the installed package (`PKG_DIR`) — the normal pip-install path gets yt-dlp as a dependency and ffmpeg via auto-provisioning/package-manager instead, so most installs never have these.

Cache (raw MKVs from yt-dlp, deleted after encode):
- `E:/Cache/ytdlp` if E: exists, otherwise `C:/cache/ytdlp` on Windows; `$XDG_CACHE_HOME/fetchforge/ytdlp` (or `~/.cache/fetchforge/ytdlp`) on Linux/macOS; override with `FETCHFORGE_CACHE_DIR` (the legacy `BOP_CACHE_DIR` is still honored).

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Serves `fetchforge/index.html` |
| GET | `/version` | Returns `APP_VERSION` |
| GET | `/heartbeat` | Keep-alive ping from the open tab; resets the idle watchdog. Returns `{ok, timeout}` |
| GET | `/ytdlp-version` | Returns yt-dlp version string |
| POST | `/update-ytdlp` | Updates yt-dlp via the mechanism matching the install: `pip install -U` when yt-dlp lives in this interpreter's env, the bundled exe's own `-U` on Windows, else a "use your package manager" message for a system binary |
| GET | `/check-cookies` | Returns `{exists, valid, cookies, youtube_cookies, reason?}` — parses cookies.txt and reports counts |
| DELETE | `/cookies` | Removes cookies.txt so downloads run cookie-free; returns `{status, existed}` |
| POST | `/upload-cookies` | Saves uploaded file as cookies.txt |
| POST | `/paste-cookies` | Validates pasted Netscape text, saves to cookies.txt, returns `{valid, cookies, youtube_cookies}` |
| POST | `/fetch-cookies` | Best-effort browser cookie import (#12). No body → scans installed Chromium-family browsers + all profiles + Firefox default, picks the source with the most YouTube cookies, saves it, resets the stale flag. Optional `browser`+`profile` form fields extract one specific source. Returns `{selected, candidates}` (per-candidate `error` on failure, never fatal) |
| GET | `/history` | Returns last 50 history entries |
| POST | `/history` | Saves a history entry (deduped by URL) |
| DELETE | `/history` | Clears all history |
| GET | `/video-info?url=` | Fetches title, duration, formats via yt-dlp. For a playlist also returns `entries` (`[{url, title, duration, index}]`) so the UI can offer a per-video checklist (#15) |
| POST | `/download` | Main SSE stream — download + encode |
| POST | `/convert-local` | SSE stream — encode local files |
| POST | `/probe-file` | ffprobe a local file, returns height + fps |
| POST | `/scan-folder` | Recursively lists video files in a folder |
| POST | `/cancel` | Terminates current ffmpeg + yt-dlp processes |
| POST | `/shutdown-now` | Runs `shutdown /s /t 0` (called by UI after countdown) |
| GET | `/browse-folder` | Opens tkinter folder picker (output dir) |
| GET | `/browse-source-folder` | Opens tkinter folder picker (source scan) |
| GET | `/browse-files` | Opens tkinter multi-file picker |

## How the job queue works (client-side)

The UI maintains a `dlQueue` array. Items are YouTube downloads or local encode jobs. `startDownload()` loops through the queue calling `runOneItem()` for each, then processes a `retryQueue` for items that failed (up to 3 rounds, 15s/30s/45s backoff). The server handles one item at a time via SSE — the queue is entirely a frontend concept.

Each queue item carries: `type` ("dl" or "local"), title, URL or file paths, format selections, `expected_size` in bytes (from YouTube metadata, used for smart skip).

## How the download flow works (per item)

### Step 1 — Resolve
`--flat-playlist -J` fetches playlist metadata without downloading. Single videos return as a one-item list. Builds `video_titles` dict (`url → title`) used for pre-download skip checks.

### Step 2 — Process
Two modes controlled by the `pipeline` form field:

**Sequential (default):** For each video: download → encode → delete MKV → next.

**Pipeline mode** (`pipeline=true`, playlists only): Two concurrent asyncio workers sharing a `Queue(maxsize=1)`:
- `dl_worker` downloads sequentially, puts `(vid_idx, Path)` on `file_q`, sleeps 3–8s random between downloads. Uses `--concurrent-fragments 2` (not 4 — higher values trigger YouTube throttling on large streams).
- `enc_worker` consumes from `file_q`, encodes, deletes MKV, loops.
- `maxsize=1` — downloader stays at most 1 video ahead of encoder, prevents MKV pile-up.
- `abort = asyncio.Event()` — set on dl or enc failure. **A dl failure does NOT kill in-progress encodes.** enc_worker only checks for `None` sentinel to stop, never `abort` during encoding. A dl failure stops future downloads but lets the current encode finish and drains any already-queued MKV.
- enc_worker `finally` block drains `file_q` **without deleting** queued MKVs — they were fully downloaded and should be preserved.
- Both workers send SSE strings to `msg_q`; main generator drains `msg_q` and yields to SSE stream.
- Each worker sends `None` sentinel when done; main generator counts 2 Nones before finishing.

### Pre-download skip check
Before downloading each video, `_predict_output_stem(title)` computes yt-dlp's exact `--restrict-filenames` output (via `yt_dlp.utils.sanitize_filename`, falling back to a regex approximation only when the package isn't importable — e.g. a Windows bundled `yt-dlp.exe`) then our `sanitize()`, and checks if `<dest>/<stem>_h265.mp4` already exists. If complete, the video is skipped. Using yt-dlp's own sanitizer avoids the accent/emoji divergence that could wrongly skip a wanted video.

### `_download_is_complete(path, expected_bytes, expected_duration)` (async)
Decides whether a download that exited non-zero is actually complete. With a known size, file must be ≥85% of `expected_bytes`. With **no** size reported (live streams, premieres, age-restricted — `expected_size=0`), it does NOT trust existence+1MB (a truncated 50MB slice of a 4GB video would pass); instead it ffprobes the container duration and requires it to match the resolve-time `duration` within ±2s. If neither size nor duration is available, the file is treated as incomplete. Per-video durations are captured at resolve into `video_durations`.

## SSE event types

All events: `data: {JSON}\n\n`. **Every event is built by an `sse_*` helper** at the top of `fetchforge/server.py` (`sse_log`, `sse_error`, `sse_phase`, `sse_progress`, `sse_done`, `sse_cancelled`, …) over a single `_sse()` that owns the wire format — never hand-roll a `data:` string. `sse_phase(header, filename="")` enforces the `header: filename` split contract.

| Type | Fields | Purpose |
|------|--------|---------|
| `phase` | `msg` | Operation label — split on `: ` in UI to get header + filename |
| `progress` | `pct, overall, speed, eta, size` | `pct` = per-file %, `overall` = job-level % |
| `video_start` | `current, total` | Start of each video — triggers overall bar |
| `dl_progress` | `pct, size, speed, eta, idx, total` | Download progress (pipeline mode) |
| `size_info` | `source_mb, output_mb, bloat_pct` | Emitted after each successful encode |
| `log` | `msg` | Scrolling log line |
| `error` | `msg` | Shown in red |
| `cancelled` | `msg` | User hit Cancel — distinct from `error`; terminal, no retry |
| `done` | `msg` | Job complete |
| `shutdown` | `msg` | Client starts the visible 60s countdown |
| `cookie_warning` | `msg` | yt-dlp reported the loaded cookies were rotated/invalidated; server has dropped cookies for the rest of the run. UI shows the notice and marks the cookie badge stale |

## Encode parameters

### Tune modes

**`uhq` (Smart Auto ★):** Maximum quality archival. uhq manages B-frames and AQ internally — do NOT pass `-bf`, `-b_ref_mode`, `-spatial_aq`, `-temporal_aq` with uhq on SDK 12+ (causes `InitializeEncoder failed: invalid param (8)`). `_probe_nvenc_tune()` tests uhq at startup; falls back to `hq` automatically on old drivers. ~1.5× encode speed at 4K 60fps.

**`hq` (High Quality ⚡):** Fast mode for NLE editing. Explicit `-spatial_aq 1 -aq-strength 8 -temporal_aq 1` + `-bf 3 -b_ref_mode middle`. Source bitrate ceiling applied — output maxrate capped at source bitrate so files stay ≤ source size. ~4–6× encode speed.

`effective_tune = "hq" if tune_mode == "hq" else _nvenc_tune`

### Key flags

**4K maxrate hard-capped at 20M**: 28M causes `InitializeEncoder failed: invalid param (8)`. The 60fps res_ceil multiplier (×1.40) only applies to sub-4K.

**`-rc vbr -cq N -b:v 0 -maxrate XM -bufsize 2XM`**: Proper VBR. Do NOT set non-zero `-b:v` with `-cq` — NVENC rejects it with uhq.

**Preset selection:**
- hq: p2 for all 60fps, p4 for 30fps
- uhq: p3 for 4K 60fps, p2 for sub-4K 60fps, p4 for 30fps (uhq rejects p2 at 4K)

**`-bf 3 -b_ref_mode middle -rc-lookahead 16`**: hq mode only. Lookahead 16 (not 32) for speed. `strict_gop` omitted.

### `calc_encode_params()` logic

1. CQ by resolution — uhq: 4K=22, 1440p=24, 1080p=26, 720p=28. hq: subtract 2 when auto.
2. Codec efficiency factor (VP9→0.80, H.264→0.55, AV1→0.92, HEVC→0.88).
3. `derived_mbps = source_bitrate_kbps × efficiency / 1000`
4. `bloom_cap = max(derived+1, derived×2)` — keyed off derived (expected output), NOT source.
5. `maxrate = min(bloom_cap, res_ceil)`; for 60fps: res_ceil × 1.40
6. hq only: `maxrate = max(min(maxrate, source_bitrate_mbps), res_min)` — source bitrate ceiling.

### Expected output sizes
VP9 4K 60fps source → H.265 NVENC: roughly equivalent efficiency for fast game content. 600MB source → 1GB output at matching quality is expected and correct.

### HDR / 10-bit
Auto-detected from `color_transfer` and `pix_fmt`. Output is `yuv420p10le` + `main10` if source is HDR or 10-bit. HDR metadata is passthrough only.

## Power management (keep-awake)

Ref-counted wake lock: `_keep_awake()` / `_allow_sleep()` (refcount to 0 releases). Cross-platform backend, identical interface: Windows re-asserts `SetThreadExecutionState` every 30s via `_awake_loop()` (`_ES_DISPLAY_REQUIRED` also prevents display sleep); Linux holds a `systemd-inhibit --what=idle:sleep` process for the encode's duration.

## Cancel

`POST /cancel` sets the sticky module-level `_cancel_requested` Event **and** terminates `current_process` (ffmpeg) and `current_dl_process` (yt-dlp). The flag is cleared at the start of every `/download` and `/convert-local` stream. Every download/encode loop (both pipeline workers and both sequential loops) polls `_cancel_requested` at the top of each iteration and inside the subprocess read loops, so a cancel stops the **whole** job — no next item, no retry — and the stream emits a `cancelled` event (not `error`). Client-side, `cancelJob()` also aborts the in-flight SSE fetch via an `AbortController`; an abort or a `cancelled` event is terminal-no-retry in `runOneItem`.

## Encode helper

The three former copies of the ffmpeg encode loop are unified: `build_video_ffmpeg_args(input, output, params, effective_tune, codec)` (pure, unit-testable) builds the `hevc_nvenc` argv, and `run_encode(args, *, duration_secs, idx, total, result)` is an async generator that runs ffmpeg, **drains stdout and stderr concurrently through one queue** (so stderr/log lines flush live during a long codec init, not only when stdout arrives), honours `_cancel_requested`, sets `current_process`, and writes the return code to `result["returncode"]`. Call sites forward its SSE strings (to `msg_q` in pipeline mode, `yield` otherwise).

## Security (localhost CSRF hardening)

The app binds to `127.0.0.1` but any page the user visits can still issue cross-origin requests. Defenses in `fetchforge/server.py`: CORS restricted to the two localhost origins; an `_origin_guard` http middleware rejects cross-origin `POST/PUT/PATCH/DELETE` (Origin header not in the allowlist → 403); `/docs`, `/redoc`, `/openapi.json` are disabled; every yt-dlp invocation validates the URL is `http(s)` and inserts a `--` end-of-options sentinel before the user URL (blocks `--exec=`-style argv injection / RCE); `/shutdown-now` additionally requires a per-launch `X-DLPR-Token` (generated at startup, injected into `index.html` via the `__DLPR_TOKEN__` placeholder, read from the `dlpr-token` meta tag by the UI).

## Shutdown sequence

UI shows a **visible** 60s countdown overlay (with a Cancel Shutdown button) after all queue items complete. If not cancelled, it calls `POST /shutdown-now` (with the `X-DLPR-Token` header) which runs the platform poweroff. The server-side SSE streams only emit the `shutdown` event — they never sleep+poweroff inline. All shutdown logic is client-driven.

## Launch modes & heartbeat auto-quit

All entry points end up at the same `server.run_server()`:
- `pip install fetchforge` → the `fetchforge` console script (`fetchforge/cli.py:main()`) — runs the ffmpeg preflight (`provision.ensure_ffmpeg()`) then calls `server.run_server()`. `python -m fetchforge` (`fetchforge/__main__.py`) does the same via `cli.main()`.
- `launch.bat` — source-clone Windows entry point; opens a visible console window, creates/reuses `.venv`, `pip install -e .` into it, then runs `python -m fetchforge` from that venv; stop the server with Ctrl+C / close the window.
- `launch-hidden.vbs` — runs `launch.bat` with a hidden window (`WScript.Shell.Run "cmd /c launch.bat", 0, False`). Python still owns a (hidden) console, so ffmpeg/yt-dlp inherit it and never flash their own windows — no `fetchforge/server.py` changes needed.
- `launch.sh` — source-clone Linux/macOS entry point: creates/reuses `.venv`, `pip install -e .`, `python -m fetchforge`.

Because the hidden launcher has no console to close, the server **self-exits when the browser tab goes away**. The page pings `GET /heartbeat` every 5s; `_heartbeat_watchdog()` (started in `lifespan`, runs every 5s) exits via `_uvicorn_server.should_exit = True` once no ping has arrived for `_HEARTBEAT_TIMEOUT` (30s). `_last_heartbeat` is initialized at launch, so the server also quits ~30s after start if no tab ever connects. The grace window (vs firing on tab-close directly) tolerates page refreshes and keeps the server up while any tab is open. `run_server()` builds an explicit `uvicorn.Server` (not `uvicorn.run`) so the watchdog can flip `should_exit`; it also opens the browser itself (1.5s delayed, via `threading.Timer`) so launchers/CLI don't need to. This is unrelated to `/shutdown-now` (which powers off the whole PC).

## yt-dlp setup

- Node.js (yt-dlp JS runtime): `_resolve_node_args()` finds `node` on PATH, falling back to `C:\Program Files\nodejs\node.exe` on Windows; omitted if not found.
- Cookies: optional `cookies.txt` in `STATE_DIR` (the cwd `fetchforge` was launched from), supplied via UI either by file upload or by pasting Netscape-format text into card #01. The paste field captures clipboard via an `onpaste` handler into a JS variable (`_pendingCookies`) and shows a `••• captured N line(s)` placeholder — the raw text is never rendered. Backend `_validate_cookies_text()` parses the Netscape format, counts entries, and reports YouTube-domain cookie counts. `/check-cookies` also returns these stats so card #01 always reflects current validity. A **Clear cookies** button (card #01, `DELETE /cookies`) removes cookies.txt for a cookie-free run. An **Attempt to fetch cookies** button (card #01, `POST /fetch-cookies`, #12) reads cookies straight from installed browsers via `yt_dlp.cookies.extract_cookies_from_browser`: `_browser_roots()`/`_firefox_root()` locate the per-OS data dirs, `_chromium_profiles()` reads each Chromium root's `Local State` → `profile.info_cache` to enumerate profiles with their display names, `_scan_all_browser_cookies()` extracts+counts each (keeping the jar so the winner isn't re-extracted), and the richest YouTube source is saved via `_save_jar()`. Blocking work runs in `asyncio.to_thread`; per-candidate failures (locked keyring/DB, absent browser) are caught. The UI lists ranked sources and lets the user switch to a specific `browser`+`profile`. **Stale-cookie auto-drop:** if yt-dlp reports the loaded cookies were rotated/invalidated mid-run (`_maybe_flag_stale_cookies()` matches "no longer valid" / "have likely been rotated"), the server sets a run-scoped `_cookies_disabled` flag so `cookie_args()` drops `--cookies` for every subsequent video immediately (rescues an in-flight playlist), emits a `cookie_warning` SSE event, and resets the flag when fresh cookies are uploaded/pasted — dead cookies are worse than none for public content.
- Format selection: user picks video + audio format IDs from `/video-info`, passed as `videoId+audioId`
- Always downloads with `--merge-output-format mkv`, then ffmpeg re-encodes to MP4
- `--concurrent-fragments 2` — keep at 2; 4 triggered YouTube throttling on large (7+ GiB) streams
- `--retries 25 --fragment-retries 25`

## UI layout (6 cards)

1. **01 — yt-dlp** — version check + update button
2. **02 — Authentication** — upload cookies.txt
3. **03 — Video URL** — URL input, fetch info, format selection, history panel, add-to-queue. A resolved playlist renders a per-video checklist (`_playlistEntries`, all checked, select all/none/invert — #15); `addToQueue()` expands the checked entries into individual queue items with `video_format=""` so each takes the highest-available stream (#13) via batch/single mode
4. **04 — Conversion** — presets, advanced CQ/maxrate, pipeline/download-only/shutdown toggles, output dir
5. **05 — Local Conversion** — browse files / browse folder / scan folder, file list, add to queue
6. **06 — Progress** — overall job bar, per-video encode bar, phase label + filename, stats row, DL status row, size/bloat row, log box

## UI presets

- **Smart Auto ★** — CQ auto, `uhq` tune, ~1.5× speed
- **High Quality ⚡** — CQ auto (−2 boost), `hq` tune, 4–6× speed, capped at source bitrate

Advanced panel: manual CQ override + maxrate ceiling override.

## Audio extraction mode

The Conversion card has a Video / Audio mode toggle. In Audio mode the H.265 preset cards are replaced by:

- **WAV** — `pcm_s16le`, preserves source sample rate. Auto-selected when best source audio ≥ 160 kbps.
- **MP3 320** — `libmp3lame -b:a 320k` CBR. Auto-selected when best source audio < 160 kbps.

Auto-selection runs from `autoSelectAudioPreset()` in `fetchforge/index.html`, keyed off the `abr` field of the audio formats returned by `/video-info`.

In audio mode:
- The video format dropdown in the Video URL card is hidden (only audio_format is needed).
- Pipeline mode and download-only / convert toggles are hidden — audio extraction always runs after download and is fast enough that pipelining adds no value.
- yt-dlp downloads only the selected audio format; no merge to MKV. Output filename is detected from `[download] Destination:` instead of `[Merger]`.
- ffmpeg uses `-vn` and either `pcm_s16le` (WAV) or `libmp3lame -b:a 320k` (MP3). Output named `<stem>.wav` / `<stem>.mp3` (no `_h265` suffix).
- The `expected_size` skip-ratio (30%) does not apply — WAV size is unrelated to source bytes. Existence + 1 MB floor is used instead.
- `mode=audio` and `audio_preset=wav|mp3` are passed as form fields on `/download` and `/convert-local`.

## Local dev & testing

- **Test suite:** `.venv/bin/python -m unittest discover -s tests -v` (stdlib unittest, no extra deps; needs the venv + ffmpeg/ffprobe on PATH). `tests/conftest.py` inserts the repo root onto `sys.path` so `from fetchforge import server` resolves whether or not the package is pip-installed. Covers pure fns (URL/stem/`calc_encode_params`), `build_video_ffmpeg_args`, SSE builder byte-identity, `run_encode` + `_download_is_complete` against real CPU ffmpeg, history concurrency, CLI preflight ordering, provisioning, and the pipeline deadlock-freedom simulation. ffmpeg-dependent tests self-skip if ffmpeg is absent.
- `python3 -m py_compile fetchforge/server.py` — fast syntax check after edits.
- `.venv/bin/python -c "from fetchforge import server; ..."` — import to unit-test pure funcs/helpers. **Use `.venv/bin/python`, not system python3** — yt-dlp is pip-installed in the venv (so `yt_dlp` is importable, and `_resolve_tool` finds it). Since tool resolution is now lazy, importing `server` no longer eagerly probes ffmpeg — only calling `get_ffmpeg()` (or startup's `HAS_SCALE_CUDA` probe / `run_server()`) does; ffmpeg still needs to be on PATH before exercising those.
- `build_video_ffmpeg_args` / `run_encode` are testable with a **CPU** ffmpeg encode (no NVENC/GPU): make a clip with `ffmpeg -f lavfi -i testsrc=d=5 clip.mkv`, encode with `libx264 ... -progress pipe:1`, pass those args to `run_encode`.
- The pipeline workers are **deadlock-prone**: validate any change to the dl/enc queue coordination with a small async simulation of the primitives (`file_q` maxsize=1, `abort`/`enc_gone`/`_cancel_requested`, sentinels) before shipping.
- Live smoke test: `fetchforge` (or `.venv/bin/python -m fetchforge`), curl `http://127.0.0.1:8765`. **NEVER send a valid `X-DLPR-Token` to `POST /shutdown-now` when testing — it powers off the machine.** Test only the 403 (no/invalid token) path.

Gotchas: this box runs **Linux** (the audit + much of this doc is Windows-flavored; the code was ported). `cookies.txt`, `history.json`, `logs/`, `downloads/` are gitignored and live in `STATE_DIR` (the cwd you launch from) — stage explicit paths (`git add fetchforge/server.py`), never `git add -A`; hitting `/history` etc. during tests pollutes the real `history.json`. Don't push to `master` (gated) — push a branch + PR; `Closes #N` in commits auto-closes issues on merge.

## Key Python gotcha

**Python < 3.12: backslashes cannot appear inside f-string expressions.**
All string formatting inside async generators uses `.format()` or extracts variables first:
```python
# BAD (crashes Python 3.11):
yield f"data: {json.dumps({'msg': params['cq']})}\n\n"

# GOOD:
yield "data: {}\n\n".format(json.dumps({"msg": params["cq"]}))
```
`pyproject.toml` now declares `requires-python = ">=3.12"` (a pip install refuses older interpreters), but the code still follows this convention defensively — a source checkout run directly with an older interpreter bypasses that check, and it costs nothing to keep.

## Versioning

`fetchforge.__version__` in `fetchforge/__init__.py` (currently `"2.1.0"`), imported into `fetchforge/server.py` as `APP_VERSION` (`from fetchforge import __version__ as APP_VERSION`) and surfaced by `pyproject.toml`'s `dynamic = ["version"]` (`attr = "fetchforge.__version__"`) so the pip package version and the running app agree. Bump on every deploy. Displayed in the header as `v 2.1.0` with a green dot fetched from `GET /version` — confirms both HTML and server are fresh after a restart.

<!-- init-workspace:start -->
## Task tracking & work environment

This repo tracks work with **GitHub Issues + the `gh` CLI**, and keeps durable project
knowledge in **`.memories/`** (grep-friendly markdown). This section is managed by the
`init-workspace` skill -- edit between the sentinels, or re-run the skill to refresh it.
Codebase/architecture docs belong elsewhere in CLAUDE.md (run `/init` for those).

### Issues

```bash
gh issue list --state open                       # all open
gh issue list --state open --assignee @me        # your in-progress work
python tools/issue-ready.py                        # ready: unassigned + open + not blocked
gh issue view <N>
gh issue create --title "..." --body "..." --label "P2,bug"
gh issue edit <N> --add-assignee @me              # claim (assignment is the lock)
gh issue close <N> --comment "<reason>"
```

- **Labels:** `P0`-`P4` priority; `bug`/`task`/`chore`/`epic`/`feature` type; `in-progress` status flag.
- **Dependencies:** write `Blocks #N` / `Blocked by #N` lines in the issue body. `issue-ready.py` hides anything blocked by an open issue.
- Use `gh` for task tracking -- not TodoWrite or markdown TODO lists.

### Memories (`.memories/`)

Durable, grep-friendly project knowledge -- one fact per file, committed and shared.

- Each memory is `.memories/<kebab-key>.md`, opening with YAML frontmatter: `description:`
  (one line, required -- feeds the index) and `type:` (optional, free-form).
- `.memories/README.md` is an **auto-generated index** -- never hand-edit it. Add or change
  a memory, then commit: the pre-commit hook runs `tools/memory-index.py` and stages the
  refreshed index. Run it by hand any time.
- Save a memory when a fact cost real effort to learn and isn't obvious from the code or
  git history. One fact, one file. Link related memories with `[[other-key]]`.

### Session completion

Work is not complete until `git push` succeeds.

1. File issues for any remaining follow-up work (`gh issue create`).
2. Run quality gates if code changed (tests, linters, build).
3. Update issue status -- close finished work, un-claim what you did not finish.
4. `git pull --rebase` then `git push`; confirm `git status` is clean.
<!-- init-workspace:end -->

<!-- foreman:start (managed by foreman-init — edits inside will be overwritten) -->
## Foreman pipeline

This repo is onboarded to the foreman two-tier pipeline: Opus (CC) plans,
reviews diffs, and merges; DeepSeek executors execute promoted issues in
background sessions. GitHub labels are the bus. Config: `.foreman.local`
(gitignored). Full rules of engagement: `REFERENCE.md` in the petur-skills
plugin's `foreman-init` skill (locate via the plugin, not a saved path).

- **Labels:** `scoped` → (human promotes) → `ready-for-agent` → `in-progress`
  → PR → merged, or `needs-replan` (+ sticky `bounced`) / `needs-human`
  (intent questions only).
- **Skills:** `gh-issues-writing` (scope), `foreman-dispatch` (launch wave +
  open the wave monitor), `foreman-status` (review REAL diffs, merge, bounce,
  escalate, report).
- **Wave monitor:** dispatch opens a read-only local dashboard
  (`foreman_view.py`, ships with the dispatch skill) at `http://127.0.0.1:8377/`
  showing per-executor liveness, log tails, and PR state. It has no merge/kill
  authority and holds no state — reconciliation is still `foreman-status`.
- **Session-open habit:** if a wave was dispatched last session, run
  `foreman-status` before anything else.
- **Rules that never bend:** executors never merge; Opus never merges without
  reading the diff; danger-zone PRs require independent verification; hand-
  fixing an executor PR is scope creep — bounce it instead. The human gates on
  intent (issue promotion) and drift (the status report), never on code.
- **Branches/dirs:** executor branches are `foreman/issue-<N>`, worktrees under
  `.foreman-worktrees/`, per-issue artifacts at repo root as
  `foreman-issue-<N>.{log,pid,meta}` — all local-excluded, never commit them.
<!-- foreman:end -->
