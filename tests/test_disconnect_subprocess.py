"""Subprocess termination on client disconnect (#87).

When the browser drops a /download or /convert-local SSE stream mid-job, the
job's generator is cancelled, but the yt-dlp / ffmpeg subprocess it started used
to keep running, detached from any job. Only POST /cancel terminated them. These
tests drive each endpoint's stream the way tests/test_wake_lock.py does (an
anyio cancel scope cancelled from a sibling task — the same mechanism
Starlette's StreamingResponse uses), with a fake subprocess whose terminate()
is recorded, and assert it was terminated.

anyio re-delivers the cancellation at every await until the task finishes, so a
cleanup that awaits before terminating never runs the terminate (#80 showed
exactly this). The terminate must be synchronous and first; these tests fail on
the pre-fix code.
"""
import asyncio
import inspect
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import anyio

from fetchforge import server


class _StreamReader:
    """An async stdout/stderr: yields each line once, then hangs (or EOFs)."""

    def __init__(self, lines, hang=True):
        self._lines = iter(lines)
        self._hang = hang

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._lines)
        except StopIteration:
            if self._hang:
                await asyncio.Event().wait()
            raise StopAsyncIteration


class _FakeProc:
    """Stands in for a yt-dlp / ffmpeg subprocess. terminate() is recorded."""

    returncode = None

    def __init__(self, kind, stdout_lines=(), stderr_lines=(), communicate_output=None,
                 returncode=None, hang=True):
        self.kind = kind
        self.terminated = False
        self.returncode = returncode
        self._stdout = _StreamReader(stdout_lines, hang=hang)
        self._stderr = _StreamReader(stderr_lines, hang=hang)
        self._communicate_output = communicate_output

    @property
    def stdout(self):
        return self._stdout

    @property
    def stderr(self):
        return self._stderr

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    async def wait(self):
        return self.returncode

    async def communicate(self):
        if self._communicate_output is None:
            await asyncio.Event().wait()
        out, err = self._communicate_output
        self.returncode = 0
        return out, err


DOWNLOAD_LINES = [
    "[download] {}% of 100MiB at 1MiB/s ETA 00:0{}\n".format(n, n).encode()
    for n in range(1, 11)
]


async def _run(endpoint, disconnect_after, **form):
    """Call a FastAPI endpoint with its Form defaults filled in and consume the
    SSE stream. disconnect_after events arrive before the client drops."""
    kwargs = {name: form[name] if name in form else param.default.default
              for name, param in inspect.signature(endpoint).parameters.items()}
    response = await endpoint(**kwargs)
    events = []
    enough = anyio.Event()

    async def consume():
        async for chunk in response.body_iterator:
            events.append(chunk)
            if len(events) == disconnect_after:
                enough.set()

    async def disconnect():
        await enough.wait()
        tg.cancel_scope.cancel()

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            tg.start_soon(consume)
            if disconnect_after:
                tg.start_soon(disconnect)
    return events


class DisconnectSubprocessTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.logs = self.tmp / "logs"
        self.logs.mkdir()
        self.cache = self.tmp / "cache"
        self.cache.mkdir()
        self.out = self.tmp / "out"
        self.out.mkdir()
        self.procs = []
        # resolve JSON returned by the flat-playlist probe; the download fake
        # normally hangs mid-stream, but a test can flip _download_completes to
        # make it finish (so the job reaches the encode).
        self._resolve_output = (b'{"_type": "video", "title": "T"}', b"")
        self._download_completes = False

        async def fake_subprocess(*args, **kwargs):
            if "--flat-playlist" in args:
                proc = _FakeProc("resolve", communicate_output=self._resolve_output)
            elif args and str(args[0]) == "/usr/bin/ffmpeg":
                proc = _FakeProc(
                    "encode",
                    stdout_lines=[b"progress=continue\n"] * 5,
                    stderr_lines=["frame={}\n".format(i).encode() for i in range(1, 6)],
                )
            elif self._download_completes:
                mkv = self.cache / "X.mkv"
                mkv.write_bytes(b"\0" * 2048)
                proc = _FakeProc(
                    "download",
                    stdout_lines=[b'[Merger] Merging formats into "' + str(mkv).encode() + b'"\n'],
                    returncode=0,
                    hang=False,
                )
            else:
                proc = _FakeProc("download", stdout_lines=DOWNLOAD_LINES)
            self.procs.append(proc)
            return proc

        async def fake_probe(path):
            return {
                "duration": 10.0, "start_time": 0.0, "height": 1080, "fps": 30.0,
                "codec": "h264", "pix_fmt": "yuv420p", "color_transfer": "",
                "video_bitrate_kbps": 5000,
            }

        for p in [
            mock.patch.object(server.asyncio, "create_subprocess_exec", fake_subprocess),
            mock.patch.object(server, "probe_video", fake_probe),
            mock.patch.object(server, "get_ffmpeg", lambda: "/usr/bin/ffmpeg"),
            mock.patch.object(server, "get_ffprobe", lambda: "/usr/bin/ffprobe"),
            mock.patch.object(server, "NODE_ARGS", []),
            mock.patch.object(server, "cookie_args", lambda: []),
            mock.patch.object(server, "LOGS_DIR", self.logs),
            mock.patch.object(server, "CACHE_DIR", self.cache),
            mock.patch.object(server, "_keep_awake", lambda: None),
            mock.patch.object(server, "_allow_sleep", lambda: None),
            mock.patch.object(server, "_cancel_requested", asyncio.Event()),
        ]:
            p.start()
            self.addCleanup(p.stop)
        self.addAsyncCleanup(asyncio.get_running_loop().shutdown_default_executor)

    def _proc(self, kind):
        matches = [p for p in self.procs if p.kind == kind]
        self.assertEqual(len(matches), 1, [p.kind for p in self.procs])
        return matches[0]

    # ── sequential download ────────────────────────────────────────────────
    async def test_sequential_download_disconnect_terminates_ytdlp(self):
        await _run(server.download, disconnect_after=6,
                   url="https://www.youtube.com/watch?v=abc",
                   audio_format="251",
                   convert="true",
                   output_dir=str(self.out))
        self.assertTrue(self._proc("download").terminated)

    # ── pipeline download ──────────────────────────────────────────────────
    async def test_pipeline_download_disconnect_terminates_ytdlp(self):
        self._resolve_output = (json.dumps({
            "_type": "playlist",
            "entries": [
                {"webpage_url": "https://www.youtube.com/watch?v=a", "title": "A", "duration": 10},
                {"webpage_url": "https://www.youtube.com/watch?v=b", "title": "B", "duration": 10},
            ],
        }).encode(), b"")
        await _run(server.download, disconnect_after=7,
                   url="https://www.youtube.com/playlist?list=PLx",
                   audio_format="251",
                   convert="true",
                   pipeline="true",
                   output_dir=str(self.out))
        self.assertTrue(self._proc("download").terminated)

    # ── encode (run_encode) via /convert-local ─────────────────────────────
    async def test_convert_local_disconnect_terminates_ffmpeg(self):
        src = self.tmp / "source.mkv"
        src.write_bytes(b"\0" * 1024)
        await _run(server.convert_local, disconnect_after=9,
                   files=json.dumps([str(src)]),
                   output_dir=str(self.out))
        self.assertTrue(self._proc("encode").terminated)

    # ── encode (run_encode) via /download ──────────────────────────────────
    async def test_download_disconnect_terminates_ffmpeg(self):
        self._download_completes = True
        await _run(server.download, disconnect_after=12,
                   url="https://www.youtube.com/watch?v=abc",
                   audio_format="251",
                   convert="true",
                   output_dir=str(self.out))
        self.assertTrue(self._proc("encode").terminated)


if __name__ == "__main__":
    unittest.main()
