"""Unexpected errors inside a job stream (#96).

A bug in a job's SSE generator used to abort the response without any `error`
event, so the client never learned why the job stopped. These tests drive the
real handlers (server.download / server.convert_local) and assert that an
unexpected exception is reported as a final `error` event, recorded as FAILED
in the job log, and does not leak the single-job admission.
"""
import asyncio
import inspect
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fetchforge import server, config, ytdlp, encode, cookies, jobs

_FAKE_YTDLP = "import sys\nsys.exit(1)\n"


async def _call(endpoint, **form):
    """Invoke a FastAPI endpoint with its Form defaults filled in."""
    kwargs = {name: form[name] if name in form else param.default.default
              for name, param in inspect.signature(endpoint).parameters.items()}
    return await endpoint(**kwargs)


async def _events(response):
    """Consume an SSE stream to completion and decode each event."""
    events = []
    async for chunk in response.body_iterator:
        chunk = chunk.decode() if isinstance(chunk, bytes) else chunk
        if chunk.startswith("data: "):
            events.append(json.loads(chunk[6:]))
    return events


class StreamErrorTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.out = self.tmp / "out"
        self.out.mkdir()
        self.logs = self.tmp / "logs"
        self.logs.mkdir()
        self.cache = self.tmp / "cache"
        self.cache.mkdir()

        # A regular file used as an ancestor: a path beneath it can never be made.
        self.blocker = self.tmp / "blocker"
        self.blocker.write_bytes(b"not a directory")
        self.uncreatable = self.blocker / "child"

        self.fake = self.tmp / "fake_ytdlp.py"
        self.fake.write_text(_FAKE_YTDLP)

        for p in [
            mock.patch.object(ytdlp, "get_ytdlp_argv", lambda: [sys.executable, str(self.fake)]),
            mock.patch.object(ytdlp, "NODE_ARGS", []),
            mock.patch.object(cookies, "cookie_args", lambda: []),
            mock.patch.object(config, "LOGS_DIR", self.logs),
            mock.patch.object(config, "CACHE_DIR", self.cache),
            mock.patch.object(config, "CONVERTED_DIR", self.out),
            mock.patch.object(jobs, "_keep_awake", lambda: None),
            mock.patch.object(jobs, "_allow_sleep", lambda: None),
            mock.patch.object(encode, "_cancel_requested", asyncio.Event()),
            mock.patch.object(jobs, "_job_admitted", False),
            mock.patch.object(jobs, "_active_jobs", 0),
            mock.patch.object(jobs, "current_process", None),
            mock.patch.object(jobs, "current_dl_process", None),
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

    async def _assert_recovers(self):
        """Admission was released, so the next job is admitted."""
        self.assertFalse(jobs._job_admitted)
        following = await _call(server.convert_local, **self._local_form())
        self.assertNotEqual(following.status_code, 409)
        await following.body_iterator.aclose()
        self.assertFalse(jobs._job_admitted)

    def _job_logs(self):
        return sorted(self.logs.glob("*.txt"))

    def assert_failed_log(self, marker):
        logs = self._job_logs()
        self.assertEqual(len(logs), 1, logs)
        text = logs[0].read_text(encoding="utf-8")
        self.assertIn("Status  : FAILED", text)
        self.assertIn("Unexpected error", text)
        self.assertIn(marker, text)

    def assert_unexpected_error(self, events):
        self.assertTrue(events, "no events were emitted")
        self.assertEqual(events[-1]["type"], "error")
        self.assertTrue(events[-1]["msg"].startswith("Unexpected error: "), events[-1])

    async def test_download_uncreatable_output_dir_emits_error(self):
        response = await _call(server.download, **self._download_form(output_dir=str(self.uncreatable)))
        events = await _events(response)
        self.assert_unexpected_error(events)
        self.assert_failed_log("YouTube Download")
        await self._assert_recovers()

    async def test_convert_local_uncreatable_output_dir_emits_error(self):
        response = await _call(server.convert_local, **self._local_form(output_dir=str(self.uncreatable)))
        events = await _events(response)
        self.assert_unexpected_error(events)
        self.assert_failed_log("Local Convert")
        await self._assert_recovers()

    async def test_convert_local_malformed_files_emits_error(self):
        response = await _call(server.convert_local, files="{not json", output_dir=str(self.out))
        events = await _events(response)
        self.assert_unexpected_error(events)
        self.assert_failed_log("Local Convert")
        await self._assert_recovers()


if __name__ == "__main__":
    unittest.main()
