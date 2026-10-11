"""Sleep-inhibitor lifetime across client disconnects (#80).

Each test drives an endpoint's stream() the way Starlette does and drops the
client mid-job by cancelling an anyio cancel scope from a sibling task, which is
what StreamingResponse's disconnect listener and the http middleware's task
group do. anyio re-delivers that cancellation at every await until the task
finishes, so the `await asyncio.to_thread(...)` that used to write the job log
in stream()'s finally was itself cancelled, and _allow_sleep() never ran.
A single task.cancel() or aclose() delivers it once and hides the bug.

Job-log writes are slowed down, as on a busy disk: a write that finishes in
the same loop iteration as the re-delivery can slip through it, so without the
delay the race would only be lost some of the time.
"""
import asyncio
import inspect
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import anyio

from fetchforge import server


class _FakeInhibitor:
    """Stands in for the systemd-inhibit Popen handle."""

    def __init__(self):
        self.terminated = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True


class _HangingProc:
    """A yt-dlp resolve that never finishes, so the disconnect lands mid-job."""

    returncode = None

    async def communicate(self):
        await asyncio.Event().wait()


async def _hanging_subprocess(*args, **kwargs):
    return _HangingProc()


async def _hanging_probe(path):
    await asyncio.Event().wait()


async def _run(endpoint, disconnect_after, **form):
    """Call a FastAPI endpoint with its Form defaults filled in and consume
    the SSE stream. With disconnect_after=N the client goes away once N events
    have arrived; with 0 the stream runs to completion."""
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


class WakeLockDisconnectTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.logs = self.tmp / "logs"
        self.logs.mkdir()
        self.out = self.tmp / "out"
        self.out.mkdir()
        self.inhibitors = []

        real_popen, real_which = server.subprocess.Popen, server.shutil.which

        def spawn(args, *rest, **kwargs):
            # Patching Popen patches the shared subprocess module, which asyncio's
            # own subprocess support also goes through: fake only the inhibitor.
            if args and str(args[0]).endswith("systemd-inhibit"):
                inhibitor = _FakeInhibitor()
                self.inhibitors.append(inhibitor)
                return inhibitor
            return real_popen(args, *rest, **kwargs)

        real_write_text = Path.write_text

        def slow_write_text(path, *args, **kwargs):
            if path.parent == self.logs:
                time.sleep(0.1)
            return real_write_text(path, *args, **kwargs)

        for p in [
            mock.patch.object(Path, "write_text", slow_write_text),
            mock.patch.object(server, "IS_WINDOWS", False),
            mock.patch.object(server.shutil, "which",
                              lambda name: "/usr/bin/systemd-inhibit" if name == "systemd-inhibit"
                              else real_which(name)),
            mock.patch.object(server.subprocess, "Popen", spawn),
            mock.patch.object(server, "_awake_refcount", 0),
            mock.patch.object(server, "_awake_inhibitor", None),
            mock.patch.object(server, "LOGS_DIR", self.logs),
            mock.patch.object(server, "NODE_ARGS", []),
            mock.patch.object(server, "cookie_args", lambda: []),
            mock.patch.object(server.asyncio, "create_subprocess_exec", _hanging_subprocess),
            mock.patch.object(server, "probe_video", _hanging_probe),
        ]:
            p.start()
            self.addCleanup(p.stop)
        # A cancelled to_thread() leaves its worker thread running: let it finish
        # before the patches and the temp dir go away.
        self.addAsyncCleanup(asyncio.get_running_loop().shutdown_default_executor)

    async def _download(self, disconnect_after):
        return await _run(server.download, disconnect_after,
                          url="https://www.youtube.com/watch?v=abc",
                          audio_format="251",
                          convert="false",
                          output_dir=str(self.out))

    async def _convert_local(self, files, disconnect_after):
        return await _run(server.convert_local, disconnect_after,
                          files=json.dumps([str(f) for f in files]),
                          output_dir=str(self.out))

    def _source_file(self):
        src = self.tmp / "source.mkv"
        src.write_bytes(b"\0" * 1024)
        return src

    def assert_released(self):
        self.assertEqual(server._awake_refcount, 0)
        self.assertTrue(self.inhibitors, "the job never took the wake lock")
        self.assertTrue(all(i.terminated for i in self.inhibitors),
                        "an inhibitor was left running")

    def assert_one_log(self, status):
        logs = list(self.logs.iterdir())
        self.assertEqual(len(logs), 1, logs)
        self.assertIn("Status  : {}".format(status), logs[0].read_text(encoding="utf-8"))

    async def test_download_disconnect_releases_inhibitor_and_writes_log(self):
        self.assertTrue(await self._download(disconnect_after=1))
        self.assert_released()
        self.assert_one_log("CANCELLED")

    async def test_convert_local_disconnect_releases_inhibitor_and_writes_log(self):
        self.assertTrue(await self._convert_local([self._source_file()], disconnect_after=1))
        self.assert_released()
        self.assert_one_log("CANCELLED")

    async def test_unwritable_log_on_disconnect_still_releases_and_warns(self):
        shutil.rmtree(self.logs)
        with self.assertLogs("fetchforge", level="WARNING") as captured:
            await self._download(disconnect_after=1)
        self.assert_released()
        self.assertTrue(any("Log write failed" in line for line in captured.output),
                        captured.output)

    async def test_job_after_a_disconnect_still_releases_its_inhibitor(self):
        await self._download(disconnect_after=1)
        # A normal job that runs to completion: no valid files, one error event.
        await self._convert_local([self.tmp / "missing.mkv"], disconnect_after=0)
        self.assertEqual(len(self.inhibitors), 2)
        self.assert_released()

    async def test_completed_job_takes_and_releases_the_lock_once(self):
        await self._convert_local([self.tmp / "missing.mkv"], disconnect_after=0)
        self.assertEqual(len(self.inhibitors), 1)
        self.assert_released()
        self.assert_one_log("FAILED")


if __name__ == "__main__":
    unittest.main()
