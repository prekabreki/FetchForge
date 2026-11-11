"""FastAPI app, middleware, routes and the server entry point.

All download/encode plumbing lives in focused modules (``items``, ``encode``,
``ytdlp``, ``jobs``, …); this module wires it to HTTP.
"""
import asyncio
import contextlib
import datetime
import json
import os
import random
import secrets
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from . import capabilities, config, cookies, encode, events, history, items as runner, jobs, sections, ytdlp

APP_VERSION = config.APP_VERSION

# The app binds to 127.0.0.1 only, but any web page the user visits can still
# issue cross-origin requests to it. Without an origin check those drive-by
# requests succeed (CSRF) — e.g. powering off the box via /shutdown-now or
# triggering downloads. These two origins are the only ones the real UI uses.
HOST = "127.0.0.1"
DEFAULT_PORT = 8765
PORT_ATTEMPTS = 20       # 8765..8784: skip ports other programs hold
PORT = DEFAULT_PORT      # the port actually served; run_server() settles it
APP_URL = ""
ALLOWED_ORIGINS: list = []   # mutated in place — CORSMiddleware keeps this list object


def _set_port(port: int) -> None:
    global PORT, APP_URL
    PORT = port
    APP_URL = "http://localhost:{}".format(port)
    ALLOWED_ORIGINS[:] = [APP_URL, "http://{}:{}".format(HOST, port)]


_set_port(DEFAULT_PORT)

# Per-process secret, injected into the served HTML and required as a header on
# the most dangerous endpoint (/shutdown-now). Regenerated every launch.
_SESSION_TOKEN = secrets.token_urlsafe(32)


def __getattr__(name):
    """Read-only view of the current job's live subprocess handles.

    `current_process` / `current_dl_process` were mutable module globals until
    #105; a Job now owns both slots and jobs exposes them. This keeps callers
    that merely *read* the handles (diagnostics, tests) working."""
    if name in ("current_process", "current_dl_process"):
        return getattr(jobs, name)
    raise AttributeError("module {!r} has no attribute {!r}".format(__name__, name))


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    await capabilities._probe_ffmpeg_capabilities()
    # Record what this ffmpeg can actually do (#47). Diagnostics only: it reuses
    # the probe results above plus one `ffmpeg -version`, and is wrapped here as
    # well as internally — a report that fails must never stop the server booting.
    try:
        await asyncio.to_thread(capabilities._refresh_capability_report)
    except Exception:
        config.logger.warning("startup capability report failed", exc_info=True)
    watchdog = asyncio.create_task(jobs._heartbeat_watchdog())
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


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = config.PKG_DIR / "index.html"
    html = await asyncio.to_thread(html_path.read_text, encoding="utf-8")
    # Hand the page this launch's session token (consumed by /shutdown-now).
    html = html.replace("__DLPR_TOKEN__", _SESSION_TOKEN)
    return HTMLResponse(html)


@app.get("/static/{name}")
async def static_asset(name: str):
    """Serve the frontend's CSS or JS. Exact-match allowlist, no StaticFiles mount."""
    media_type = config.STATIC_FILES.get(name)
    if media_type is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(
        config.PKG_DIR / name,
        media_type=media_type,
        # The URL never changes between releases, so a long max-age would keep
        # running an old app.js/app.css against a freshly-served index.html after
        # an update. no-cache forces a revalidation each load; FileResponse already
        # sends ETag + Last-Modified, so an unchanged file is a cheap 304.
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/fonts/{name}")
async def font_asset(name: str):
    """Serve one vendored webfont. Exact-match allowlist, woff2 only."""
    if name not in config.FONT_FILES:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(
        config.FONTS_DIR / name,
        media_type="font/woff2",
        # Immutable content at a stable path: a font only changes when the package
        # does, and a stale face is the one thing this whole change exists to avoid.
        # A day is long enough to survive the page reloads a single session makes.
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.post("/upload-cookies")
async def upload_cookies(file: UploadFile = File(...)):
    content = await file.read()
    await asyncio.to_thread(config.COOKIES_PATH.write_bytes, content)
    cookies._cookies_disabled = False    # fresh cookies deserve a new chance
    return {"status": "ok", "message": "cookies.txt saved"}


@app.post("/paste-cookies")
async def paste_cookies(text: str = Form(...)):
    result = cookies._validate_cookies_text(text)
    if not result.get("valid"):
        return result

    body = text.strip()
    if not any("netscape http cookie file" in l.lower() for l in body.splitlines()[:5]):
        body = "# Netscape HTTP Cookie File\n" + body
    await asyncio.to_thread(config.COOKIES_PATH.write_text, body, encoding="utf-8")
    cookies._cookies_disabled = False    # fresh cookies deserve a new chance
    return result


@app.get("/check-cookies")
async def check_cookies():
    if not config.COOKIES_PATH.exists():
        return {"exists": False}
    try:
        text = await asyncio.to_thread(config.COOKIES_PATH.read_text, encoding="utf-8", errors="replace")
        info = cookies._validate_cookies_text(text)
        return {"exists": True, **info}
    except Exception as e:
        return {"exists": True, "valid": False, "reason": str(e)}


@app.delete("/cookies")
async def clear_cookies():
    """Remove the loaded cookies.txt so downloads run cookie-free. Public content
    needs no auth, and stale cookies are worse than none (see issue #9). Guarded
    against cross-origin callers by _origin_guard like other mutating routes."""
    existed = config.COOKIES_PATH.exists()
    await asyncio.to_thread(config.COOKIES_PATH.unlink, missing_ok=True)
    return {"status": "ok", "existed": existed}


@app.post("/fetch-cookies")
async def fetch_cookies(browser: str = Form(""), profile: str = Form("")):
    """Best-effort: scan installed browsers/profiles for YouTube cookies, pick the
    richest source, and save it as cookies.txt (issue #12). With an explicit
    browser (+optional profile dir), extract just that source instead. A fresh
    save clears the stale-cookie flag (#9). Per-candidate failures are reported,
    never fatal. Guarded against cross-origin callers by _origin_guard."""
    if browser:
        prof = profile or None
        try:
            jar = await asyncio.to_thread(cookies._extract_one, browser, prof)
        except Exception as e:
            label = "{}{}".format(browser, "/" + profile if profile else "")
            root = cookies._browser_roots().get(browser)
            reason = cookies._failure_reason(root, str(e)) if root is not None else ""
            out = {"selected": None, "candidates": [],
                   "error": "Could not read {}: {}".format(label, str(e)[:200])}
            if reason:
                out["reason"] = reason
            return out
        total, yt = cookies._count_youtube_cookies(jar)
        await asyncio.to_thread(cookies._save_jar, jar, config.COOKIES_PATH)
        cookies._cookies_disabled = False
        sel = {"browser": browser, "profile_dir": prof,
               "display_name": profile or "default", "cookies": total, "youtube_cookies": yt}
        return {"selected": sel, "candidates": [sel]}

    results = await asyncio.to_thread(cookies._scan_all_browser_cookies)
    candidates = [entry for entry, _ in results]
    best_entry, best_jar = next(
        ((e, j) for e, j in results if e.get("youtube_cookies", 0) > 0 and j is not None),
        (None, None),
    )
    if best_entry is None:
        return {"selected": None, "candidates": candidates}
    try:
        await asyncio.to_thread(cookies._save_jar, best_jar, config.COOKIES_PATH)
        cookies._cookies_disabled = False
    except Exception as e:
        return {"selected": None, "candidates": candidates,
                "error": "Found cookies but failed to save: {}".format(str(e)[:200])}
    return {"selected": best_entry, "candidates": candidates}


@app.get("/history")
async def get_history():
    return await history._load_history()


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
    await history._save_history_entry(entry)
    return {"status": "ok"}


@app.delete("/history")
async def clear_history():
    async with history._history_lock:
        if config.HISTORY_PATH.exists():
            await asyncio.to_thread(config.HISTORY_PATH.unlink)
    return {"status": "ok"}


@app.get("/ytdlp-version")
async def ytdlp_version():
    proc = await asyncio.create_subprocess_exec(
        *ytdlp.get_ytdlp_argv(), "--version",
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
    ytdlp_argv = ytdlp.get_ytdlp_argv()
    bundled = config.PKG_DIR / "yt-dlp.exe"
    if ytdlp.IS_WINDOWS and ytdlp_argv == [str(bundled)]:
        cmd = [*ytdlp_argv, "-U"]
    elif ytdlp._ytdlp_in_this_env(ytdlp_argv):
        cmd = ytdlp._ytdlp_upgrade_cmd()
        if cmd is None:
            return {"output": (
                "yt-dlp is installed in this interpreter's environment, but "
                "neither pip nor uv is available to upgrade it.\n"
                "Install one of them, then run one of:\n"
                '  {} -m pip install -U "yt-dlp[default]"\n'
                '  uv pip install --python {} -U "yt-dlp[default]"'
            ).format(sys.executable, sys.executable)}
    else:
        return {"output": (
            "yt-dlp here is a system/package-managed binary at {}.\n"
            "FetchForge can't update it in place — update it with your OS package "
            "manager (e.g. `sudo dnf upgrade yt-dlp` or `sudo apt upgrade yt-dlp`), "
            "or run FetchForge from a virtualenv where yt-dlp is a pip dependency."
        ).format(ytdlp_argv[0])}
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    # get_ytdlp_argv() is cached; a successful upgrade can change which yt-dlp
    # the resolver would pick (e.g. a console script appears), so drop the cache
    # and let the next call re-resolve.
    if proc.returncode == 0:
        ytdlp.get_ytdlp_argv.cache_clear()
    return {"output": stdout.decode(errors="replace").strip()}


@app.get("/video-info")
async def video_info(url: str):
    if not ytdlp.is_http_url(url):
        return JSONResponse({"error": "URL must be http or https"}, status_code=400)
    # Use --flat-playlist first to detect playlist vs single
    args = [
        *ytdlp.get_ytdlp_argv(),
        "--flat-playlist",
        "-J",
        "--no-download",
        *ytdlp.NODE_ARGS,
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
        err = stderr.decode(errors="replace")
        return JSONResponse({"error": err}, status_code=400)

    info = json.loads(stdout)
    is_playlist = info.get("_type") == "playlist"

    if is_playlist:
        entries = [e for e in info.get("entries", []) if e]
        # Surface each entry so the UI can offer a per-video checklist (#15).
        entry_list = []
        for idx, e in enumerate(entries):
            vid_url = e.get("webpage_url") or e.get("url") or ""
            if vid_url and not vid_url.startswith("http"):
                vid_url = "https://www.youtube.com/watch?v={}".format(vid_url)
            if not vid_url:
                continue
            entry_list.append({
                "url": vid_url,
                "title": e.get("title") or vid_url,
                "duration": e.get("duration") or 0,
                "index": idx + 1,
            })
        # For playlists, fetch format info from the first entry
        first_url = entries[0].get("url") or entries[0].get("webpage_url") if entries else url
        format_info = await ytdlp._get_formats(first_url)
        return {
            "is_playlist": True,
            "title": info.get("title", "Playlist"),
            "uploader": info.get("uploader") or info.get("channel"),
            "count": len(entry_list),
            "entries": entry_list,
            "thumbnail": entries[0].get("thumbnails", [{}])[-1].get("url") if entries else None,
            **format_info,
        }
    else:
        format_info = await ytdlp._get_formats(url)
        return {
            "is_playlist": False,
            "title": info.get("title", "Unknown"),
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "uploader": info.get("uploader"),
            **format_info,
        }


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
    target_res: str = Form("0"),     # output height in pixels, "0" = off
    sharpen: str = Form("false"),    # "true"/"false"
    # Encode overrides (#50). All default to "" = auto, so existing clients are
    # unaffected. Passed through verbatim — validate_encode_overrides owns every
    # coercion, clamp and fallback; nothing here is trusted.
    preset: str = Form(""),          # "" = auto, else p1–p7
    tune: str = Form(""),            # "" = auto, else hq | uhq
    pix_fmt: str = Form(""),         # "" = auto, else yuv420p | yuv420p10le
    # Time range (#76), whole seconds; "" = from the start / to the end. Single
    # video only — batch items carry their own section_start/section_end.
    section_start: str = Form(""),
    section_end: str = Form(""),
):
    if not jobs._try_admit():
        return JSONResponse({"error": jobs._BUSY_MESSAGE}, status_code=409)
    job = jobs._register_job(jobs.Job("download", url=url))


    async def _download_stream():
        refusal = runner._nvenc_missing_error(mode, convert == "true")
        if refusal:
            yield job.emit(events._ev_error(refusal))
            return

        _target_res = int(target_res) if target_res.lstrip("-").isdigit() else 0
        _sharpen = sharpen == "true"
        _convert = convert == "true"
        _delete_cache = delete_cache == "true"
        _shutdown = shutdown == "true"

        # Fail early on an unusable request-level output dir, before any
        # subprocess runs (unchanged from the pre-items flow).
        base_dest = Path(output_dir) if output_dir else config.CONVERTED_DIR
        base_dest.mkdir(parents=True, exist_ok=True)
        encode_opts = {
            "cq": cq,
            "maxrate": maxrate,
            "preset": preset,
            "tune": tune,
            "pix_fmt": pix_fmt,
            "target_res": _target_res,
            "sharpen": _sharpen,
        }

        # ── Build the items list: `items` when given, else one item from `url` ──
        if items.strip():
            try:
                parsed = ytdlp._parse_items_payload(items)
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                yield job.emit(events._ev_error("Bad batch payload: {}".format(exc)))
                return
            if not parsed:
                yield job.emit(events._ev_error("Batch had no items."))
                return
            download_items = [
                runner._make_download_item(
                    idx, data,
                    mode=mode, audio_preset=audio_preset,
                    convert=_convert, delete_cache=_delete_cache,
                    output_dir=output_dir, encode_opts=encode_opts)
                for idx, data in enumerate(parsed, 1)
            ]
            total_videos = len(download_items)
            yield job.emit(events._ev_log("Batch: {} item(s) queued.".format(total_videos)))
        else:
            if not ytdlp.is_http_url(url):
                yield job.emit(events._ev_error('URL must be http or https.'))
                return
            try:
                section = sections.parse_section(section_start, section_end)
            except ValueError as exc:
                yield job.emit(events._ev_error("Bad time range: {}".format(exc)))
                return

            # Old clients still send a single URL. Resolve its title/duration,
            # but never expand a playlist server-side — the client already does
            # that and a playlist response here would be silently incomplete.
            yield job.emit(events._ev_phase('Resolving...'))
            flat_args = [*ytdlp.get_ytdlp_argv(), "--flat-playlist", "-J", "--no-download", *ytdlp.NODE_ARGS]
            flat_args += cookies.cookie_args()
            flat_args += ["--", url]

            flat_proc = await asyncio.create_subprocess_exec(
                *flat_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                flat_out, flat_err = await flat_proc.communicate()
            finally:
                jobs._terminate_if_running(flat_proc)
                await flat_proc.wait()

            if flat_proc.returncode != 0:
                yield job.emit(events._ev_error('Could not fetch video info: ' + flat_err.decode(errors='replace')[:300]))
                return
            try:
                info = json.loads(flat_out)
            except Exception:
                yield job.emit(events._ev_error('Failed to parse video info.'))
                return

            if info.get("_type") == "playlist":
                yield job.emit(events._ev_error("URL is a playlist. Send one item per video in the items field."))
                return

            total_videos = 1
            download_items = [runner._make_download_item(
                1,
                {"url": url, "title": info.get("title") or "",
                 "duration": info.get("duration") or 0,
                 "video_format": video_format,
                 "audio_format": audio_format,
                 "expected_size": expected_size,
                 "section": section},
                mode=mode, audio_preset=audio_preset,
                convert=_convert, delete_cache=_delete_cache,
                output_dir=output_dir, encode_opts=encode_opts,
            )]

        # ── Execute: pipelined when explicitly requested, else sequential ─────
        if pipeline == "true" and total_videos > 1:
            yield job.emit(events._ev_log('Pipeline mode: next download starts while current video encodes.'))

            async def _fetch_payload(item, emit, stopping):
                path = await runner.fetch_item(item, emit, stopping,
                                              total=total_videos, pipelined=True, job=job)
                return None if path is None else (item, path)

            async def _process_payload(payload, emit):
                item, path = payload
                return await runner.process_item(item, path, emit,
                                                total=total_videos, pipelined=True, job=job)

            async def _throttle(item, emit, stopping):
                if item["idx"] < total_videos and not stopping():
                    sleep_s = random.uniform(3, 8)
                    await emit(events._ev_log("Next download in {:.1f}s...".format(sleep_s)))
                    await asyncio.sleep(sleep_s)

            async with contextlib.aclosing(runner.run_pipelined(
                    download_items, _fetch_payload, _process_payload,
                    job.cancel, throttle=_throttle,
                    shutdown=_shutdown, job=job)) as _pipeline:
                async for _event in _pipeline:
                    yield _event
        else:
            async for _event in runner._iter_sequential(
                    download_items, total=total_videos, shutdown=_shutdown, job=job):
                yield _event

    return StreamingResponse(
        jobs._tracked(jobs._job_stream(job, _download_stream()), job),
        media_type="text/event-stream")


@app.post("/cancel")
async def cancel():
    job = jobs._current_job
    if job is None:
        return {"status": "nothing running"}
    return {"status": "cancelled" if job.cancel_now() else "nothing running"}


@app.post("/probe-file")
async def probe_file(path: str = Form(...)):
    p = Path(path)
    if not p.exists():
        return JSONResponse({"error": "File not found"}, status_code=400)
    proc = await asyncio.create_subprocess_exec(
        ytdlp.get_ffprobe(), "-v", "quiet",
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
    target_res: str = Form("0"),     # output height in pixels, "0" = off
    sharpen: str = Form("false"),    # "true"/"false"
    # Encode overrides (#50) — see /download; "" = auto, validated server-side.
    preset: str = Form(""),          # "" = auto, else p1–p7
    tune: str = Form(""),            # "" = auto, else hq | uhq
    pix_fmt: str = Form(""),         # "" = auto, else yuv420p | yuv420p10le
):
    if not jobs._try_admit():
        return JSONResponse({"error": jobs._BUSY_MESSAGE}, status_code=409)
    job = jobs._register_job(jobs.Job("local", files=files))

    async def _convert_local_stream():
        refusal = runner._nvenc_missing_error(mode, True)
        if refusal:
            yield job.emit(events._ev_error(refusal))
            return

        _target_res = int(target_res) if target_res.lstrip("-").isdigit() else 0
        _sharpen = sharpen == "true"

        is_audio = mode == "audio"
        out_ext = encode.audio_output_ext(audio_preset) if is_audio else "mp4"
        out_suffix = "" if is_audio else "_h265"

        file_paths = [Path(f) for f in json.loads(files)]
        file_paths = [f for f in file_paths if f.exists()]
        total = len(file_paths)

        if not file_paths:
            yield job.emit(events._ev_error('No valid files found.'))
            return

        custom_dest = Path(output_dir) if output_dir else None
        if custom_dest:
            custom_dest.mkdir(parents=True, exist_ok=True)

        action_label = "audio extraction" if is_audio else "conversion"
        yield job.emit(events._ev_log(f'{total} file(s) queued for {action_label}.'))

        for idx, input_file in enumerate(file_paths, 1):
            if job.cancel.is_set():
                break   # cancel stops the whole queue
            yield job.emit(events._ev_video_start(idx, total))
            dest_dir = custom_dest or (input_file.parent / "converted")
            dest_dir.mkdir(parents=True, exist_ok=True)
            safe_stem = ytdlp.sanitize(input_file.stem)
            output_file = dest_dir / "{}{}.{}".format(safe_stem, out_suffix, out_ext)

            source_bytes = input_file.stat().st_size if input_file.exists() else 0
            if output_file.exists():
                out_size = output_file.stat().st_size
                min_output = runner._min_output_bytes(source_bytes, is_audio)
                if out_size < min_output:
                    yield job.emit(events._ev_log("Removing incomplete output ({:.0f} MB, expected >={:.0f} MB) \u2014 re-encoding.".format(
                        out_size / 1024**2, min_output / 1024**2)))
                    output_file.unlink()
                else:
                    yield job.emit(events._ev_log(f'Skipping — output already exists: {output_file.name} ({out_size / 1024 ** 2:.0f} MB)'))
                    continue

            phase_verb = "Extracting audio" if is_audio else "Converting"
            yield job.emit(events._ev_phase(f'{phase_verb} {idx}/{total}', input_file.name))
            _src_bytes = input_file.stat().st_size if input_file.exists() else 0
            yield job.emit(events._ev_source_size(_src_bytes))

            # Deep-probe source for smart encode parameters
            src = await ytdlp.probe_video(input_file)
            duration_secs = src.get("duration") or None
            if duration_secs:
                yield job.emit(events._ev_log(f'Duration: {duration_secs:.1f}s'))

            if is_audio:
                ffmpeg_args = encode.build_audio_ffmpeg(input_file, output_file, audio_preset)
                enc_log = "Audio extract: {} → {}".format(audio_preset.upper(), output_file.name)
                yield job.emit(events._ev_log(enc_log))
            else:
                params, ffmpeg_args, enc_logs = encode._encode_plan(
                    src, input_file, output_file,
                    {"tune_mode": tune_mode, "cq": cq, "maxrate": maxrate,
                     "preset": preset, "tune": tune, "pix_fmt": pix_fmt,
                     "target_res": _target_res, "sharpen": _sharpen},
                )
                yield job.emit(events._ev_encode_params(params))
                for _log in enc_logs:
                    yield job.emit(events._ev_log(_log))

            enc_res: dict = {}
            async for _s in encode.run_encode(ffmpeg_args, duration_secs=duration_secs,
                                              idx=idx, total=total, result=enc_res, job=job):
                yield _s

            if job.cancel.is_set():
                if output_file.exists():
                    output_file.unlink(missing_ok=True)
                break   # user cancelled mid-encode — not a failure
            if enc_res["returncode"] != 0:
                # Remove partial output so retries don't falsely skip
                if output_file.exists():
                    output_file.unlink(missing_ok=True)
                fail_verb = "Audio extract" if is_audio else "Conversion"
                yield job.emit(events._ev_error(f'{fail_verb} failed: {input_file.name}'))
                return

            source_bytes = input_file.stat().st_size
            output_bytes = output_file.stat().st_size
            yield job.emit(events._ev_log(f'Done: {output_file.name}'))
            if source_bytes > 0:
                yield job.emit(events._ev_size_info(source_bytes, output_bytes))

        if job.cancel.is_set():
            yield job.emit(events._ev_cancelled('Cancelled by user.'))
        else:
            yield job.emit(events._ev_done('All done!'))
            if shutdown == "true":
                # Client-driven: UI runs the visible countdown and calls /shutdown-now.
                yield job.emit(events._ev_shutdown('Shutting down in 60 seconds...'))

    return StreamingResponse(
        jobs._tracked(jobs._job_stream(job, _convert_local_stream()), job),
        media_type="text/event-stream")


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


@app.get("/capabilities")
async def get_capabilities():
    """The startup capability report (#47): which ffmpeg is in use, what version
    it reports, and the outcome of all three startup probes — plus a prebuilt
    `warning` (None when everything passed) for the UI to render.

    Served from the cache filled in `lifespan`; the lazy refresh only fires if
    the app was started without its lifespan, and runs off the event loop."""
    if not capabilities.get_capability_report():
        await asyncio.to_thread(capabilities._refresh_capability_report)
    return capabilities.get_capability_report()


@app.get("/heartbeat")
async def heartbeat():
    """Keep-alive ping from the open browser tab. When pings stop (tab closed),
    the watchdog self-exits the server after _HEARTBEAT_TIMEOUT seconds."""
    jobs._last_heartbeat = time.monotonic()
    return {"ok": True, "timeout": jobs._HEARTBEAT_TIMEOUT}


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
    config._poweroff()
    return {"ok": True}


def _candidate_ports() -> range:
    return range(DEFAULT_PORT, DEFAULT_PORT + PORT_ATTEMPTS)


def _bind_listen_socket(port: int):
    """Bind HOST:port up front, or return None if something already holds it.

    Binding before uvicorn starts means a busy port is known before the startup
    probes run and before the browser is pointed at whatever owns the port.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name == "nt":
        # Windows SO_REUSEADDR would let us bind over a live listener; this refuses it.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        # POSIX SO_REUSEADDR only skips TIME_WAIT from our own previous run.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((HOST, port))
    except OSError:
        sock.close()
        return None
    return sock


def _running_fetchforge_version(port: int) -> Optional[str]:
    """Version reported by a FetchForge already serving port, or None if the holder isn't one."""
    try:
        with urllib.request.urlopen("http://{}:{}/version".format(HOST, port), timeout=2) as r:
            data = json.loads(r.read())
    except (OSError, ValueError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    return version if isinstance(version, str) else None


def run_server(open_browser: bool = True) -> int:
    """Serve the app until the heartbeat watchdog or Ctrl+C stops it; returns an exit code.

    Walks _candidate_ports() in order: a port held by another FetchForge is reused
    (a tab opens on it), a port held by anything else is skipped. A second launch
    walks the same order, so it finds an instance that fell back to a later port.
    """
    config._ensure_runtime_dirs()   # create cache/downloads/logs before anything writes to them
    config._setup_logging()         # deferred here (not import time) so a bare import is side-effect-free
    ports = _candidate_ports()
    sock = None
    for port in ports:
        sock = _bind_listen_socket(port)
        if sock is not None:
            break
        running = _running_fetchforge_version(port)
        if running is not None:
            _set_port(port)
            config.logger.info("FetchForge %s is already running at %s — opening it", running, APP_URL)
            if open_browser:
                webbrowser.open(APP_URL)
            return 0
        config.logger.info("Port %d is in use by another program — trying the next one", port)
    if sock is None:
        config.logger.error(
            "Ports %d-%d are all in use by other programs, so FetchForge cannot start.",
            ports[0], ports[-1])
        return 1
    _set_port(port)
    config.logger.info("Starting FetchForge at %s", APP_URL)
    if open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(APP_URL)).start()
    uvicorn_config = uvicorn.Config(app, host=HOST, port=PORT)
    jobs._uvicorn_server = uvicorn.Server(uvicorn_config)
    try:
        jobs._uvicorn_server.run(sockets=[sock])
    except Exception:
        # On a headless (no-console) launch this is the only place the failure is
        # visible — make sure it lands in logs/server.log before we die.
        config.logger.exception("Server exited with an unhandled exception")
        raise
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    from fetchforge.cli import main
    sys.exit(main())
