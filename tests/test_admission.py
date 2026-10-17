"""Single-job admission (#89).

Only one /download or /convert-local job may run at a time. A second request
gets HTTP 409 before any stream work, and the rejected request must not clear
the shared cancel flag, replace the shared process handles, terminate anything,
or write output. Admission is released on completion, validation failure,
stream error, and disconnect — including a response whose stream is never
consumed.

These tests drive the real handlers (server.download / server.convert_local),
the way tests/test_wake_lock.py and tests/test_disconnect_subprocess.py do, with
a fake subprocess standing in for yt-dlp/ffmpeg.
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


async def _call(endpoint, **form):
    """Invoke a FastAPI endpoint with its Form defaults filled in, returning the
    response WITHOUT consuming its stream (so the job holds admission)."""
    kwargs = {name: form[name] if name in form else param.default.default
              for name, param in inspect.signature(endpoint).parameters.items()}
    return await endpoint(**kwargs)


async def _drain(response):
    """Consume an SSE stream to completion."""
    async for _chunk in response.body_iterator:
        pass


async def _consume_with_disconnect(response, disconnect_after):
    """Consume `disconnect_after` events, then drop the client by cancelling the
    consuming task from a sibling task — the same mechanism Starlette's
    StreamingResponse disconnect listener uses (see tests/test_wake_lock.py)."""
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
            tg.start_soon(disconnect)
    return events


class AdmissionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.out = self.tmp / "out"
        self.out.mkdir()
        self.logs = self.tmp / "logs"
        self.logs.mkdir()
        self.cache = self.tmp / "cache"
        self.cache.mkdir()
        self.procs = []

        self._resolve_output = (b'{"_type": "video", "title": "T", "duration": 10}', b"")

        async def fake_subprocess(*args, **kwargs):
            if "--flat-playlist" in args:
                proc = _FakeProc("resolve", communicate_output=self._resolve_output)
            else:
                proc = _FakeProc("download", stdout_lines=[b"[download] 1%\n"])
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
            mock.patch.object(server, "CONVERTED_DIR", self.out),
            mock.patch.object(server, "_keep_awake", lambda: None),
            mock.patch.object(server, "_allow_sleep", lambda: None),
            mock.patch.object(server, "_cancel_requested", asyncio.Event()),
            mock.patch.object(server, "_job_admitted", False),
            mock.patch.object(server, "_active_jobs", 0),
            mock.patch.object(server, "current_process", None),
            mock.patch.object(server, "current_dl_process", None),
        ]:
            p.start()
            self.addCleanup(p.stop)
        self.addAsyncCleanup(asyncio.get_running_loop().shutdown_default_executor)

    def _download_form(self, **overrides):
        form = {
            "url": "https://www.youtube.com/watch?v=abc",
            "audio_format": "251",
            "convert": "false",
            "output_dir": str(self.out),
        }
        form.update(overrides)
        return form

    def _local_form(self, **overrides):
        src = self.tmp / "source.mkv"
        src.write_bytes(b"\0" * 1024)
        form = {
            "files": json.dumps([str(src)]),
            "output_dir": str(self.out),
        }
        form.update(overrides)
        return form

    def assert_admitted(self, response):
        self.assertNotEqual(response.status_code, 409)

    # ── overlap rejection ───────────────────────────────────────────────────
    async def test_second_download_rejected_with_409(self):
        first = await _call(server.download, **self._download_form())
        self.assertTrue(server._job_admitted)

        second = await _call(server.download, **self._download_form())
        self.assertEqual(second.status_code, 409)
        self.assertEqual(json.loads(second.body), {"error": server._BUSY_MESSAGE})
        self.assertTrue(server._job_admitted, "the loser must not release the winner's admission")

        await first.body_iterator.aclose()
        self.assertFalse(server._job_admitted)

    async def test_second_convert_local_rejected_with_409(self):
        first = await _call(server.convert_local, **self._local_form())
        second = await _call(server.convert_local, **self._local_form())
        self.assertEqual(second.status_code, 409)
        await first.body_iterator.aclose()

    async def test_mixed_overlap_rejected_with_409(self):
        first_dl = await _call(server.download, **self._download_form())
        second_local = await _call(server.convert_local, **self._local_form())
        self.assertEqual(second_local.status_code, 409)
        await first_dl.body_iterator.aclose()

        first_local = await _call(server.convert_local, **self._local_form())
        second_dl = await _call(server.download, **self._download_form())
        self.assertEqual(second_dl.status_code, 409)
        await first_local.body_iterator.aclose()

    # ── the losing request has no effects ───────────────────────────────────
    async def test_rejected_job_has_no_effects(self):
        first = await _call(server.download, **self._download_form())

        # The accepted job's shared state, which the loser must not touch.
        server._cancel_requested.set()
        handle = _FakeProc("guard")
        server.current_process = handle
        server.current_dl_process = handle
        self.procs.clear()

        second = await _call(server.download, **self._download_form())
        self.assertEqual(second.status_code, 409)
        self.assertTrue(server._cancel_requested.is_set(), "loser cleared the sticky cancel flag")
        self.assertIs(server.current_process, handle, "loser replaced current_process")
        self.assertIs(server.current_dl_process, handle, "loser replaced current_dl_process")
        self.assertFalse(handle.terminated, "loser terminated a shared handle")
        self.assertEqual(self.procs, [], "loser spawned a subprocess")
        self.assertEqual(list(self.out.iterdir()), [], "loser wrote output")
        self.assertEqual(len(list(self.logs.iterdir())), 0, "loser wrote a job log")

        await first.body_iterator.aclose()

    # ── a new job is admitted after cleanup ─────────────────────────────────
    async def test_new_job_admitted_after_validation_failure(self):
        first = await _call(server.convert_local, files=json.dumps([str(self.tmp / "missing.mkv")]),
                            output_dir=str(self.out))
        await _drain(first)          # no valid files → error event → stream ends
        self.assertFalse(server._job_admitted)

        second = await _call(server.download, **self._download_form())
        self.assert_admitted(second)
        await second.body_iterator.aclose()

    async def test_new_job_admitted_after_disconnect(self):
        first = await _call(server.download, **self._download_form())
        await _consume_with_disconnect(first, disconnect_after=1)
        self.assertFalse(server._job_admitted)

        second = await _call(server.convert_local, **self._local_form())
        self.assert_admitted(second)
        await second.body_iterator.aclose()

    async def test_unconsumed_response_releases_admission(self):
        # A response whose stream is never consumed still releases admission when
        # the stream is closed (as Starlette does when the client goes away before
        # reading anything). The stream is closed without a single chunk consumed.
        first = await _call(server.download, **self._download_form())
        self.assertTrue(server._job_admitted)

        await first.body_iterator.aclose()
        self.assertFalse(server._job_admitted)

        second = await _call(server.download, **self._download_form())
        self.assert_admitted(second)
        await second.body_iterator.aclose()


if __name__ == "__main__":
    unittest.main()
