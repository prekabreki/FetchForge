"""One-item download/encode runner and the pipeline coordinator (#103).

`fetch_item` owns the yt-dlp argv, progress parsing, cache snapshot/output
detection and download-completeness checks; `process_item` owns encode, audio
extract, download-only and the single delete_cache site. `run_pipelined` and
`_iter_sequential` drive them for the two execution modes.
"""
import asyncio
import os
import re
from pathlib import Path
from typing import Optional

from . import capabilities, config, cookies, encode, events, jobs, sections, ytdlp

_NO_NVENC_VIDEO_MESSAGE = (
    "Video conversion needs NVENC, but this ffmpeg has no hevc_nvenc encoder. "
    "Download-only and audio extraction still work."
)

_MAX_CONSECUTIVE_ENCODE_FAILURES = 3  # pipeline mode: bail out if the encoder is fundamentally broken


def _nvenc_missing_error(mode: str, converting: bool) -> Optional[str]:
    """Why a video job must be refused before any download, or None.

    Audio extraction and download-only never touch hevc_nvenc, so they keep
    working on an ffmpeg without NVENC (#99)."""
    if converting and mode != "audio" and not capabilities.HAS_NVENC:
        return _NO_NVENC_VIDEO_MESSAGE
    return None


def _min_output_bytes(source_bytes: int, is_audio: bool) -> int:
    """The single "is this output complete?" rule for every path: an audio
    output only needs to exist and be at least 1 MB; a video output must be at
    least 1 MB and 30% of its source (H.265 is 40-100% of source)."""
    if is_audio:
        return 1024 * 1024
    return max(1024 * 1024, int(source_bytes * 0.30))


def _item_is_audio(item) -> bool:
    return item.get("mode", "video") == "audio"


def _item_output_ext(item) -> str:
    return encode.audio_output_ext(item.get("audio_preset", "mp3")) if _item_is_audio(item) else "mp4"


def _item_output_suffix(item) -> str:
    return "" if _item_is_audio(item) else "_h265"


def _item_dest_dir(item) -> Path:
    raw = item.get("output_dir") or ""
    return Path(raw) if raw else config.CONVERTED_DIR


def _item_output_path(item, stem: str) -> Path:
    return _item_dest_dir(item) / "{}{}.{}".format(
        stem, _item_output_suffix(item), _item_output_ext(item))


def _make_download_item(idx, data, *, mode, audio_preset, convert, delete_cache,
                        output_dir, encode_opts) -> dict:
    """One fully self-describing item. Every per-item value lives here, so both
    the sequential loop and the pipeline read settings the same way."""
    item = {
        "idx": idx,
        "url": data["url"],
        "title": data.get("title") or "",
        "duration": data.get("duration") or 0,
        "video_format": data.get("video_format") or "",
        "audio_format": data.get("audio_format") or "",
        "expected_size": int(data.get("expected_size") or 0),
        "output_dir": data.get("output_dir") or output_dir or "",
        "tune_mode": data.get("tune_mode") or "uhq",
        "section": data.get("section"),
        "mode": data.get("mode") or mode,
        "audio_preset": data.get("audio_preset") or audio_preset,
        "convert": convert,
        "delete_cache": delete_cache,
    }
    item.update(encode_opts)
    return item


async def fetch_item(item, emit, stopping, *, total, pipelined=False, job=None):
    """Download one item and return its Path, or None when it was skipped,
    failed or cancelled.

    Owns the yt-dlp argv, progress parsing, cache snapshot/output detection
    (`_newest_new_file`) and the download-completeness checks for BOTH the
    sequential loop and the pipeline coordinator. A failed item emits
    `item_failed` and returns None; the caller keeps going. The live yt-dlp
    handle lives in the job's downloader slot so /cancel can reach it.
    """
    idx = item["idx"]
    url = item["url"]
    title = item.get("title") or ""
    section = item.get("section")
    is_audio = _item_is_audio(item)
    convert = bool(item.get("convert", True))
    label = "[{}/{}] ".format(idx, total) if total > 1 else ""
    dl_label = "[DL {}/{}] ".format(idx, total) if total > 1 else ""

    dest_dir = _item_dest_dir(item)
    dest_dir.mkdir(parents=True, exist_ok=True)

    # `video_start` marks the start of processing THIS item for the UI. In
    # sequential mode the download is the start (encode follows immediately);
    # in pipelined mode the encoder is a separate worker that may be a whole
    # video behind, so process_item emits it at encode time instead.
    if not pipelined:
        await emit(events._ev_video_start(idx, total))

    # Skip only when the output this job produces is already complete. A
    # skipped item never deletes a cache file it did not download.
    if title and (convert or is_audio):
        predicted = _item_output_path(
            item, ytdlp._predict_output_stem(title, sections.section_suffix(section)))
        # A clip's size is unrelated to the full video's reported size.
        skip_source_bytes = 0 if section else int(item.get("expected_size") or 0)
        min_output = _min_output_bytes(skip_source_bytes, is_audio)
        if predicted.exists() and predicted.stat().st_size >= min_output:
            await emit(events._ev_log("{}Skipping \u2014 already encoded: {}".format(
                label, predicted.name)))
            await emit(events._ev_item_done(idx, total))
            return None

    # In pipeline mode the main progress bar tracks the concurrent ENCODE;
    # download progress goes to the separate dl-status row via dl_progress.
    # A `phase` event here would flip the main bar to indeterminate.
    if not pipelined:
        await emit(events._ev_phase("{}Downloading".format(label), title))
    if title:
        await emit(events._ev_log("{}Downloading: {}".format(dl_label, title)))
    else:
        await emit(events._ev_log("{}Downloading...".format(dl_label)))

    if is_audio:
        fmt_args = ["-f", ytdlp._audio_format_selector(item.get("audio_format", ""))]
        merge_args = []
    else:
        fmt_args = ["-f", ytdlp._video_format_selector(
            item.get("video_format", ""), item.get("audio_format", ""),
            prefer_picked=True)]
        merge_args = ["--merge-output-format", "mkv"]

    dl_args = [
        *ytdlp.get_ytdlp_argv(),
        *fmt_args,
        *ytdlp.NODE_ARGS,
        "--no-playlist",
        "--no-overwrites",
        *merge_args,
        "--concurrent-fragments", "2",
        "--retries", "25",
        "--fragment-retries", "25",
        "--newline",
        "--restrict-filenames",
        "--paths", str(config.CACHE_DIR),
        "-o", "%(title)s{}.%(ext)s".format(sections.section_suffix(section)),
        *sections.section_ytdlp_args(section),
    ]
    dl_args += cookies.cookie_args()
    dl_args += ["--", url]

    cache_patterns = ytdlp._cache_snapshot_patterns(is_audio)
    pre_existing = {p for pat in cache_patterns for p in config.CACHE_DIR.glob(pat)}
    expected_dur = item.get("duration") or 0
    exp_bytes = 0 if section else int(item.get("expected_size") or 0)

    proc = await asyncio.create_subprocess_exec(
        *dl_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    if job is not None:
        job.downloader = proc
    output_paths = set()

    try:
        async for raw in proc.stdout:
            if stopping():
                proc.terminate()
                break
            line = raw.decode(errors="replace").rstrip()
            _cw = cookies._maybe_flag_stale_cookies(line)
            if _cw:
                await emit(events._ev_cookie_warning(_cw))
            m = re.search(
                r"\[download\]\s+([\d.]+)%\s+of\s+(\S+)\s+at\s+(.+?)\s+ETA\s+(\S+)",
                line
            )
            if m:
                pct, size, speed, eta = m.groups()
                if pipelined:
                    await emit(events._ev_dl_progress(float(pct), size, speed, eta, idx, total))
                else:
                    await emit({"type": "progress", "pct": float(pct),
                                "size": size, "speed": speed, "eta": eta})
            else:
                merger = re.search(r'\[Merger\] Merging formats into "(.+?)"', line)
                # Audio mode and section downloads have no merger — capture the
                # destination line instead.
                dest = re.search(r'\[download\] Destination: (.+)', line) if (is_audio or section) else None
                already = re.search(r'\[download\] (.+?) has already been downloaded', line)
                if merger:
                    output_paths.add(merger.group(1))
                elif dest:
                    output_paths.add(dest.group(1).strip())
                elif already:
                    output_paths.add(already.group(1))
                await emit(events._ev_log(line))
        await proc.wait()
    finally:
        # Synchronous terminate first: a finally under a re-delivered anyio
        # cancellation must not await before terminating (#87).
        jobs._terminate_if_running(proc)
        if job is not None and job.downloader is proc:
            job.downloader = None
        await proc.wait()

    if (job.cancel.is_set() if job is not None else encode._cancel_requested.is_set()) or stopping():
        return None   # user cancelled / pipeline aborted — not a download failure

    allowed_suffixes = ytdlp._AUDIO_EXTS if is_audio else {".mkv"}
    files = []
    if output_paths:
        files = sorted(
            [Path(p) for p in output_paths
             if Path(p).exists() and Path(p).suffix.lower() in allowed_suffixes],
            key=os.path.getmtime,
        )
        if not files and not is_audio:
            for p in output_paths:
                candidate = Path(p) if Path(p).suffix else Path(p).with_suffix(".mkv")
                if candidate.exists():
                    files = [candidate]
                    break
    if not files:
        # Only a file that appeared during THIS download — never a sibling
        # video's file already sitting in the shared cache (#8/#92).
        found = ytdlp._newest_new_file(config.CACHE_DIR, pre_existing, cache_patterns)
        if found:
            files = [found]

    if proc.returncode != 0:
        if files and await (
                sections._section_download_is_complete(files[0], section, expected_dur) if section
                else ytdlp._download_is_complete(files[0], exp_bytes, expected_dur)):
            await emit(events._ev_log("{}yt-dlp exited with errors but output file verified complete \u2014 continuing.".format(dl_label)))
        else:
            size_note = ""
            if files and exp_bytes > 0:
                actual = files[0].stat().st_size
                size_note = " (got {:.0f}MB of expected {:.0f}MB)".format(
                    actual / 1024**2, exp_bytes / 1024**2)
            await emit(events._ev_item_failed(idx, total, "download failed"))
            if total == 1:
                await emit(events._ev_error("{}Download failed{}.".format(label, size_note)))
            await emit(events._ev_log("{}Download failed{} \u2014 skipping, continuing with the rest.".format(dl_label, size_note)))
            return None

    if not files:
        await emit(events._ev_item_failed(idx, total, "no output"))
        if total == 1:
            await emit(events._ev_error("{}No .mkv found after download.".format(label)))
        await emit(events._ev_log("{}No .mkv found after download \u2014 skipping, continuing with the rest.".format(dl_label)))
        return None

    await emit(events._ev_log("{}Queued for encoding.".format(dl_label)))
    return files[0]


async def process_item(item, path, emit, *, total, pipelined=False, job=None) -> bool:
    """Encode, audio-extract or leave-as-downloaded one fetched item.

    Returns True on success (including a skipped/already-complete output) and
    False on an encode failure. Owns the output-completeness rule and the
    single `delete_cache` deletion site. The live ffmpeg handle lives in the
    job's encoder slot so /cancel can reach it.
    """
    idx = item["idx"]
    is_audio = _item_is_audio(item)
    convert = bool(item.get("convert", True))
    delete_cache = bool(item.get("delete_cache", True))
    section = item.get("section")
    label = "[{}/{}] ".format(idx, total) if total > 1 else ""
    enc_label = "[ENC {}/{}] ".format(idx, total) if total > 1 else ""

    # In pipelined mode the download worker runs a video ahead, so the UI's
    # "now processing THIS video" signal must come from the encoder (encode
    # start), not the downloader — otherwise video_start for item N+1 would
    # fire while item N is still encoding and reset the encode timer early.
    if pipelined:
        await emit(events._ev_video_start(idx, total))

    if not convert and not is_audio:
        await emit(events._ev_log("{}Saved (no conversion): {}".format(label, path)))
        await emit(events._ev_item_done(idx, total))
        return True

    dest_dir = _item_dest_dir(item)
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_stem = ytdlp._output_stem(path.stem, sections.section_suffix(section))
    output_file = _item_output_path(item, safe_stem)

    source_size = path.stat().st_size if path.exists() else 0
    min_output = _min_output_bytes(source_size, is_audio)

    if output_file.exists():
        out_size = output_file.stat().st_size
        if out_size < min_output:
            await emit(events._ev_log("{}Removing incomplete output ({:.0f} MB, expected >={:.0f} MB) \u2014 re-encoding.".format(
                enc_label, out_size / 1024**2, min_output / 1024**2)))
            output_file.unlink()
        else:
            await emit(events._ev_log("{}Skipping \u2014 output already exists: {} ({:.0f} MB)".format(
                enc_label, output_file.name, out_size / 1024**2)))
            if delete_cache:
                path.unlink(missing_ok=True)
            await emit(events._ev_item_done(idx, total))
            return True

    phase_verb = "Extracting audio" if is_audio else "Converting"
    await emit(events._ev_phase(phase_verb, path.name))
    await emit(events._ev_source_size(source_size))

    src = await ytdlp.probe_video(path)
    duration_secs = src.get("duration") or None
    seek = sections.section_input_seek(section, src.get("start_time", 0.0))
    if section:
        duration_secs = sections._clip_duration(section, item.get("duration") or 0)
    if duration_secs:
        await emit(events._ev_log("Duration: {:.1f}s".format(duration_secs)))
    else:
        await emit(events._ev_log("Could not read duration \u2014 progress bar will be indeterminate"))

    if is_audio:
        ffmpeg_args = encode.build_audio_ffmpeg(
            path, output_file, item.get("audio_preset", "mp3"), input_opts=seek)
        await emit(events._ev_log("Audio extract: {} \u2192 {}".format(
            item.get("audio_preset", "mp3").upper(), output_file.name)))
    else:
        params, ffmpeg_args, enc_logs = encode._encode_plan(
            src, path, output_file,
            {"tune_mode": item.get("tune_mode", "uhq"),
             "cq": item.get("cq", "0"), "maxrate": item.get("maxrate", ""),
             "preset": item.get("preset", ""), "tune": item.get("tune", ""),
             "pix_fmt": item.get("pix_fmt", ""),
             "target_res": item.get("target_res", 0),
             "sharpen": item.get("sharpen", False)},
            input_opts=seek,
        )
        await emit(events._ev_encode_params(params))
        for _log in enc_logs:
            await emit(events._ev_log(_log))

    enc_res: dict = {}
    async for _s in encode.run_encode(ffmpeg_args, duration_secs=duration_secs,
                                      idx=idx, total=total, result=enc_res, job=job):
        await emit(_s)

    if (job.cancel.is_set() if job is not None else encode._cancel_requested.is_set()):
        # User cancelled mid-encode — discard the partial output, don't treat
        # it as a failure or retry.
        if output_file.exists():
            output_file.unlink(missing_ok=True)
        return True

    if enc_res["returncode"] != 0:
        if output_file.exists():
            output_file.unlink(missing_ok=True)
        fail_verb = "Audio extract" if is_audio else "Conversion"
        await emit(events._ev_error("{}{} failed: {}".format(label, fail_verb, path.name)))
        await emit(events._ev_item_failed(idx, total, "encode failed"))
        return False

    source_bytes = path.stat().st_size if path.exists() else 0
    if delete_cache:
        path.unlink(missing_ok=True)
    output_bytes = output_file.stat().st_size
    size_mb = output_bytes / 1024 ** 2
    cache_note = "cache cleared" if delete_cache else "cache kept"
    await emit(events._ev_log("Done: {} \u2014 {:.0f} MB ({})".format(output_file.name, size_mb, cache_note)))
    if source_bytes > 0:
        await emit(events._ev_size_info(source_bytes, output_bytes))
    await emit(events._ev_item_done(idx, total))
    return True


async def run_pipelined(items, fetch, process, cancel, *, throttle=None,
                        finalize=None, shutdown=False, job=None):
    """Coordinate the pipeline downloader and encoder (one item model).

    `items` is an iterable of opaque item identifiers. For each one,
    `fetch(item, emit, stopping)` is awaited and returns a payload to encode, or
    None to skip that item. `process(payload, emit)` is awaited once per queued
    payload and returns True on success, False on an encode failure; the
    coordinator owns the consecutive-failure cap. Both bodies publish SSE
    strings through the async `emit` sink — this async generator yields them in
    order.

    `throttle(item, emit, stopping)` runs after a payload is queued (the real
    inter-download sleep), and `finalize(emit)` runs after the download loop.

    This function owns `file_q` (maxsize=1), `abort`, `enc_gone`, cancel
    polling, the two None sentinels, the encoder's drain-without-delete, and the
    worker-task cancellation/reaping on generator close (#87). With a `job`, the
    bodies' event dicts are logged and serialized by that job.
    """
    file_q: asyncio.Queue = asyncio.Queue(maxsize=1)
    msg_q: asyncio.Queue = asyncio.Queue()
    abort = asyncio.Event()        # set on hard failure (stops the pipeline)
    enc_gone = asyncio.Event()     # set when the encoder exits (unblocks the fetcher)

    async def emit(msg) -> None:
        # Event dicts are logged + rendered by the job; an already rendered
        # wire string (from run_encode) is forwarded untouched.
        if job is not None and isinstance(msg, dict):
            msg = job.emit(msg)
        await msg_q.put(msg)

    def stopping() -> bool:
        return abort.is_set() or cancel.is_set() or enc_gone.is_set()

    async def _put_file(payload) -> bool:
        """Hand a downloaded file to the encoder over the maxsize=1 queue, but
        never block forever if the encoder has stopped consuming (abort, cancel,
        or encoder exited). Returns False if we gave up without enqueuing.
        Fixes the dl_worker↔enc_worker deadlock (H-7)."""
        while not stopping():
            try:
                file_q.put_nowait(payload)
                return True
            except asyncio.QueueFull:
                await asyncio.sleep(0.1)
        return False

    async def _send_sentinel() -> None:
        """Deliver the None stop-sentinel to the encoder so it can break out of
        file_q.get(). Keep trying while the encoder is alive (even on
        cancel/abort — it still needs the sentinel to exit), but give up the
        moment it has already exited so we never block on a full queue."""
        while not enc_gone.is_set():
            try:
                file_q.put_nowait(None)
                return
            except asyncio.QueueFull:
                await asyncio.sleep(0.1)

    async def dl_worker():
        try:
            for item in items:
                if stopping():
                    break
                payload = await fetch(item, emit, stopping)
                if payload is None:
                    continue
                if not await _put_file(payload):
                    break   # encoder stopped consuming (abort/cancel) — don't hang
                if throttle is not None:
                    await throttle(item, emit, stopping)
            if finalize is not None:
                await finalize(emit)
        finally:
            # Always make sure the encoder receives its stop-sentinel (even on
            # cancel/abort it may be idle on file_q.get()), but without blocking
            # if it has already exited. Its own finally sends the msg_q sentinel
            # the main loop counts.
            await _send_sentinel()
            await msg_q.put(None)

    async def enc_worker():
        consecutive_failures = 0
        try:
            while True:
                item = await file_q.get()
                if item is None:
                    break
                if cancel.is_set():
                    break
                ok = await process(item, emit)
                if cancel.is_set():
                    break
                if not ok:
                    consecutive_failures += 1
                    # H-6: a single encode failure must NOT abort the pipeline —
                    # that would kill the fetcher's in-flight download. Surface
                    # the error and move on; only give up once the encoder looks
                    # fundamentally broken (N in a row).
                    if consecutive_failures >= _MAX_CONSECUTIVE_ENCODE_FAILURES:
                        abort.set()
                        await emit(events._ev_error(
                            "Aborting pipeline: {} consecutive encode failures.".format(consecutive_failures)))
                        break
                    continue
                consecutive_failures = 0
        finally:
            # Unblock any pending _put_file so the fetcher can't hang on the
            # maxsize=1 queue now that we're gone (H-7). Distinct from `abort`
            # so a clean finish still reports success.
            enc_gone.set()
            # Drain queue without deleting — queued files may still be valid.
            while True:
                try:
                    file_q.get_nowait()
                except asyncio.QueueEmpty:
                    break
            await msg_q.put(None)

    dl_task = asyncio.create_task(dl_worker())
    enc_task = asyncio.create_task(enc_worker())

    try:
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
                yield jobs._job_emit(job, events._ev_error("Pipeline worker error: {}".format(_r)))

        if cancel.is_set():
            yield jobs._job_emit(job, events._ev_cancelled('Cancelled by user.'))
        elif not abort.is_set():
            yield jobs._job_emit(job, events._ev_done('All done!'))
            if shutdown:
                # Shutdown is client-driven: emit the event, the UI runs the
                # visible countdown and calls POST /shutdown-now itself.
                yield jobs._job_emit(job, events._ev_shutdown('Shutting down in 60 seconds...'))
    finally:
        # A client disconnect cancels this generator while the workers keep
        # running as detached tasks. Cancel them so their own finallys terminate
        # any in-flight yt-dlp/ffmpeg, then reap them (#87).
        for _t in (dl_task, enc_task):
            if not _t.done():
                _t.cancel()
        await jobs._reap_tasks(dl_task, enc_task)


async def _iter_sequential(items, *, total, shutdown, job):
    """Sequential execution: a loop over `fetch_item` / `process_item`. Events
    are pumped through a queue so the two coroutines can be the exact same ones
    the pipeline coordinator drives."""
    q: asyncio.Queue = asyncio.Queue()
    done_ids, failed_ids = set(), set()

    async def emit(msg):
        # Our bodies hand in event dicts; `run_encode` hands in an already
        # rendered (and already logged) wire string, forwarded untouched.
        if isinstance(msg, dict):
            if msg.get("type") == "item_done":
                done_ids.add(msg.get("idx"))
            elif msg.get("type") == "item_failed":
                failed_ids.add(msg.get("idx"))
            msg = job.emit(msg)
        await q.put(msg)

    async def driver():
        try:
            for item in items:
                if job.cancel.is_set():
                    break
                path = await fetch_item(
                    item, emit, job.cancel.is_set,
                    total=total, pipelined=False, job=job)
                if path is None:
                    if job.cancel.is_set():
                        break
                    continue
                await process_item(item, path, emit, total=total, job=job)

            if job.cancel.is_set():
                await emit(events._ev_cancelled('Cancelled by user.'))
            elif failed_ids and not done_ids:
                # Every item failed. A single-item job already emitted its own
                # `error`; a multi-item one gets one terminal error here.
                if total > 1:
                    await emit(events._ev_error("All items failed."))
            else:
                await emit(events._ev_done('All done!'))
                if shutdown:
                    await emit(events._ev_shutdown('Shutting down in 60 seconds...'))
        finally:
            await q.put(None)

    task = asyncio.create_task(driver())
    try:
        while True:
            msg = await q.get()
            if msg is None:
                break
            yield msg
        await task
    finally:
        if not task.done():
            task.cancel()
        await jobs._reap_tasks(task)
