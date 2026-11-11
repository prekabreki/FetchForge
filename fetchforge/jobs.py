"""The Job object, single-job admission, heartbeat watchdog and wake lock (#105)."""
import asyncio
import contextlib
import datetime
import json
import logging
import shutil
import subprocess
import sys
import time
from typing import Optional

from . import config, events

IS_WINDOWS = sys.platform.startswith("win")
if IS_WINDOWS:
    import ctypes

logger = logging.getLogger("fetchforge")

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


@contextlib.contextmanager
def _wake_lock():
    """Hold the wake lock for the body's lifetime: released exactly once, on any exit."""
    _keep_awake()
    try:
        yield
    finally:
        _allow_sleep()


async def _write_job_log(path, text):
    """Write a job log off the event loop, logging (never raising) a failed write.

    On a client disconnect the job's task is cancelled, and anyio re-delivers
    that cancellation at every await until the task finishes, so the thread
    hop is cancelled too. Write synchronously then: the log matters more than
    a few ms of a cancelled stream's loop time.
    """
    try:
        await asyncio.to_thread(path.write_text, text, encoding="utf-8")
    except asyncio.CancelledError:
        try:
            path.write_text(text, encoding="utf-8")
        except Exception as exc:
            logger.warning("Log write failed: %s", exc)
        raise
    except Exception as exc:
        logger.warning("Log write failed: %s", exc)


def _terminate_if_running(proc) -> bool:
    """Synchronously SIGTERM a subprocess that hasn't exited yet.

    Synchronous on purpose: a finally running under a re-delivered anyio
    cancellation must not await before the terminate, or the terminate never
    runs (#87). Swallows the race where the process already died and was reaped
    (ProcessLookupError). Returns True if a terminate was actually sent."""
    if proc is None or proc.returncode is not None:
        return False
    try:
        proc.terminate()
    except Exception:
        pass
    return True


async def _reap_tasks(*tasks) -> None:
    """Await already-cancelled worker tasks to completion, immune to anyio
    re-delivering the disconnect cancellation at every await (#87)."""
    for _t in tasks:
        while not _t.done():
            try:
                await asyncio.shield(_t)
            except asyncio.CancelledError:
                pass


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
_job_admitted = False            # single-job admission (#89): True while a job owns the server
_current_job = None              # the admitted Job (#105); /cancel acts on its handles
_BUSY_MESSAGE = "Another download or conversion is already running. Wait for it to finish and try again."
_uvicorn_server = None           # set in run_server(); lets the watchdog stop the server


def __getattr__(name):
    """Read-only view of the current job's live subprocess handles.

    `current_process` / `current_dl_process` were mutable module globals until
    #105; a Job now owns both slots. This keeps callers that merely *read* the
    handles (diagnostics, tests) working without reintroducing shared state."""
    if name in ("current_process", "current_dl_process"):
        job = _current_job
        if job is None:
            return None
        return job.ffmpeg if name == "current_process" else job.downloader
    raise AttributeError("module {!r} has no attribute {!r}".format(__name__, name))


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


def _try_admit() -> bool:
    """Atomically claim the single job slot (#89). Synchronous and await-free, so
    the check-and-set cannot be interleaved with another request on the event
    loop. Returns False when a /download or /convert-local job already runs."""
    global _job_admitted
    if _job_admitted:
        return False
    _job_admitted = True
    return True


def _release_admission() -> None:
    """Release the single job slot. Idempotent and synchronous, so it runs even
    under a re-delivered anyio cancellation (no awaits)."""
    global _job_admitted
    _job_admitted = False


def _register_job(job):
    """Make `job` the one /cancel can reach after admission succeeds."""
    global _current_job
    _current_job = job
    return job


class _TrackedStream:
    """Cold-safe async iterable wrapping an SSE generator.

    Increments _active_jobs for the life of the stream, stamps _last_job_end, and
    closes the Job exactly once — on exhaustion, error, cancellation, or close,
    which releases the single-job admission. It is a plain object (not a bare
    async generator) so that aclose() releases admission even when the stream is
    closed before it is ever iterated: a never-started async generator's finally
    never runs, which would leak the admission held from the handler (#89)."""

    def __init__(self, agen, job):
        self._agen = agen
        self._job = job
        self._started = False
        self._finished = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        global _active_jobs
        if not self._started:
            self._started = True
            _active_jobs += 1
        try:
            return await self._agen.__anext__()
        except StopAsyncIteration:
            self._finish()
            raise
        except asyncio.CancelledError:
            self._finish()
            raise
        except BaseException:
            self._finish()
            raise

    async def aclose(self):
        self._finish()
        aclose = getattr(self._agen, "aclose", None)
        if aclose is not None:
            await aclose()

    def _finish(self):
        global _active_jobs, _last_job_end
        if self._finished:
            return
        self._finished = True
        if self._started:
            _active_jobs -= 1
        _last_job_end = time.monotonic()
        self._job.close()


def _tracked(agen, job):
    """Wrap an SSE generator so the heartbeat watchdog knows a job is in flight
    and the Job is closed (releasing admission) when the stream ends (whether it
    finished, errored, was cancelled, or was never consumed)."""
    return _TrackedStream(agen, job)


# ── Job (#105) ───────────────────────────────────────────────────────────────
# One admitted /download or /convert-local run. A Job owns everything that used
# to be module-global state: its sticky cancel event, the live subprocess
# handles (one slot per role), the wake lock, the job-log lines and the SSE wire
# format. Only one Job can exist at a time (#89), but a fresh one is created per
# request, so nothing leaks from a finished or cancelled run.
class Job:
    """The state and teardown of one admitted job.

    `emit(event)` is the single sink for job events: it appends the matching
    job-log line and returns the SSE string, so no code path has to parse an SSE
    string back into JSON to build the log.
    """

    def __init__(self, kind: str, *, url: str = "", files: str = ""):
        self.kind = kind                     # "download" | "local"
        self.url = url
        self.files = files
        self.started = datetime.datetime.now()
        self.cancel = asyncio.Event()
        self.ffmpeg = None                   # encoder subprocess (run_encode)
        self.downloader = None               # yt-dlp subprocess (fetch_item)
        self.lines = []                      # "[HH:MM:SS] [LEVEL] message"
        self._wake = None

    # ── wake lock ──────────────────────────────────────────────────────────
    def __enter__(self):
        self._wake = _wake_lock()
        self._wake.__enter__()
        return self

    def __exit__(self, *exc):
        return self._wake.__exit__(*exc)

    # ── event log + wire ───────────────────────────────────────────────────
    def emit(self, event: dict) -> str:
        """Log `event` and return its SSE wire form."""
        self.log_event(event)
        return events._sse(event)

    def log_event(self, event: dict) -> None:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        t = event.get("type", "")
        if t == "phase":
            level, msg = "PHASE", event.get("msg", "")
        elif t == "log":
            level, msg = "LOG", event.get("msg", "")
        elif t == "error":
            level, msg = "ERROR", event.get("msg", "")
        elif t == "done":
            level, msg = "DONE", event.get("msg", "")
        elif t == "video_start":
            level = "VIDEO"
            msg = "Video {current}/{total}".format(**event)
        elif t == "size_info":
            level = "SIZE"
            msg = "source={source_mb}MB  output={output_mb}MB  bloat={bloat_pct:+.1f}%".format(**event)
        else:
            return
        self.lines.append("[{}] [{}] {}".format(ts, level, msg))

    def status(self) -> str:
        for ln in reversed(self.lines):
            if "[ERROR]" in ln:
                return "FAILED"
            if "[DONE]" in ln:
                return "SUCCESS"
        return "CANCELLED"

    def log_text(self) -> str:
        if self.kind == "download":
            header = [
                "Type    : YouTube Download",
                "URL     : {}".format(self.url),
            ]
        else:
            try:
                file_list = json.loads(self.files)
            except Exception:
                file_list = [self.files]
            header = [
                "Type    : Local Convert",
                "Files   : {}".format(", ".join(str(f) for f in file_list)),
            ]
        elapsed = datetime.datetime.now() - self.started
        header_lines = [
            "=== FetchForge Job Log ===",
            "Started : {}".format(self.started.strftime("%Y-%m-%d %H:%M:%S")),
            *header,
            "Elapsed : {}".format(str(elapsed).split(".")[0]),
            "Status  : {}".format(self.status()),
            "",
            "--- Events ---",
            "",
        ]
        return "\n".join(header_lines + self.lines + [""])

    def log_path(self):
        ts_file = self.started.strftime("%Y-%m-%d_%H-%M-%S")
        suffix = "download" if self.kind == "download" else "local"
        return config.LOGS_DIR / "{}_{}.txt".format(ts_file, suffix)

    # ── cancel / teardown ──────────────────────────────────────────────────
    def cancel_now(self) -> bool:
        """Set the cancel flag and synchronously terminate live subprocesses.

        Synchronous on purpose: it must run under a re-delivered anyio
        cancellation (#87). Returns True if a handle was terminated."""
        self.cancel.set()
        terminated = False
        for attr in ("ffmpeg", "downloader"):
            proc = getattr(self, attr)
            if proc is not None:
                if _terminate_if_running(proc):
                    terminated = True
                setattr(self, attr, None)
        return terminated

    def close(self) -> None:
        """Release admission exactly once and stop being the current job."""
        global _current_job
        if _current_job is self:
            _current_job = None
        _release_admission()

    @property
    def exception_label(self) -> str:
        return ("Unexpected error in download job" if self.kind == "download"
                else "Unexpected error in local conversion job")


def _job_emit(job: Optional["Job"], event: dict) -> str:
    """Serialize an event through the job (so it is logged) when there is one."""
    return job.emit(event) if job is not None else events._sse(event)


async def _job_stream(job: Job, event_iter):
    """The one SSE wrapper shared by /download and /convert-local (#105).

    Holds the job's wake lock, forwards every event the runner emitted (the
    runner logs them through `job.emit`), turns an unexpected exception into a
    final error event (#96), and always writes the job log."""
    with job:
        try:
            async for chunk in event_iter:
                yield chunk
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A disconnect cancels the task, and cleanup can raise while that
            # cancellation is pending (#87). Never turn that into a job error:
            # let the cancellation continue so it propagates as before.
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise asyncio.CancelledError() from exc
            logger.exception(job.exception_label)
            yield job.emit(events._ev_error("Unexpected error: {}".format(exc)))
        finally:
            await _write_job_log(job.log_path(), job.log_text())
