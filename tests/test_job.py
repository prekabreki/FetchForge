"""The Job object and its job log (#105).

`Job.emit` is the one sink that both appends a job-log line and returns the SSE
string, so the shared stream wrapper never parses an SSE string. These tests
drive the real `/download` and `/convert-local` handlers and read the log each
one writes, pinning the file names, header fields and status detection that the
pre-#105 copies of the wrapper produced.
"""
import asyncio
import inspect
import json
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from fetchforge import server, config, events, ytdlp, cookies, jobs


_FAKE_YTDLP = textwrap.dedent('''
    import json, sys
    from pathlib import Path
    argv = sys.argv[1:]
    if "--flat-playlist" in argv:
        print(json.dumps({"title": "T", "duration": 10}), flush=True)
        sys.exit(0)
    paths = Path(argv[argv.index("--paths") + 1])
    name = argv[argv.index("-o") + 1].replace("%(title)s", "T").replace("%(ext)s", "mkv")
    out = paths / name
    out.write_bytes(b"x" * 4096)
    print('[Merger] Merging formats into "' + str(out) + '"', flush=True)
    sys.exit(0)
''')


async def _call(endpoint, **form):
    kwargs = {name: form[name] if name in form else param.default.default
              for name, param in inspect.signature(endpoint).parameters.items()}
    return await endpoint(**kwargs)


async def _drain(response):
    async for _chunk in response.body_iterator:
        pass


class JobLogTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.out = self.tmp / "out"
        self.out.mkdir()
        self.logs = self.tmp / "logs"
        self.logs.mkdir()
        self.cache = self.tmp / "cache"
        self.cache.mkdir()

        for p in [
            mock.patch.object(ytdlp, "get_ytdlp_argv",
                              lambda: [sys.executable, "-c", _FAKE_YTDLP]),
            mock.patch.object(ytdlp, "NODE_ARGS", []),
            mock.patch.object(cookies, "cookie_args", lambda: []),
            mock.patch.object(config, "LOGS_DIR", self.logs),
            mock.patch.object(config, "CACHE_DIR", self.cache),
            mock.patch.object(config, "CONVERTED_DIR", self.out),
            mock.patch.object(jobs, "_keep_awake", lambda: None),
            mock.patch.object(jobs, "_allow_sleep", lambda: None),
        ]:
            p.start()
            self.addCleanup(p.stop)
        self.addAsyncCleanup(asyncio.get_running_loop().shutdown_default_executor)

    def _only_log(self, suffix):
        logs = list(self.logs.glob("*_{}.txt".format(suffix)))
        self.assertEqual(len(logs), 1, list(self.logs.iterdir()))
        return logs[0], logs[0].read_text(encoding="utf-8")

    def assert_common_header(self, text):
        self.assertIn("=== FetchForge Job Log ===", text)
        self.assertIn("Started : ", text)
        self.assertIn("Elapsed : ", text)
        self.assertIn("--- Events ---", text)

    async def test_download_success_log_name_header_and_status(self):
        response = await _call(
            server.download,
            url="https://www.youtube.com/watch?v=abc",
            audio_format="251",
            convert="false",
            delete_cache="false",
            output_dir=str(self.out),
        )
        await _drain(response)

        path, text = self._only_log("download")
        self.assert_common_header(text)
        self.assertIn("Type    : YouTube Download", text)
        self.assertIn("URL     : https://www.youtube.com/watch?v=abc", text)
        self.assertIn("Status  : SUCCESS", text)
        self.assertIn("[VIDEO] Video 1/1", text)
        self.assertIn("[DONE] All done!", text)
        self.assertIn("[PHASE]", text)
        self.assertIn("[LOG]", text)

    async def test_convert_local_success_log_name_header_and_status(self):
        src = self.tmp / "source.mkv"
        src.write_bytes(b"\0" * 1024)
        # Pre-create a complete output so the job skips the encode and reaches
        # its `done` event without needing ffmpeg.
        existing = self.out / "source_h265.mp4"
        existing.write_bytes(b"\0" * (2 * 1024 * 1024))

        response = await _call(
            server.convert_local,
            files=json.dumps([str(src)]),
            output_dir=str(self.out),
        )
        await _drain(response)

        path, text = self._only_log("local")
        self.assert_common_header(text)
        self.assertIn("Type    : Local Convert", text)
        self.assertIn("Files   : {}".format(src), text)
        self.assertIn("Status  : SUCCESS", text)
        self.assertIn("[VIDEO] Video 1/1", text)
        self.assertIn("[DONE] All done!", text)

    async def test_job_log_is_cancelled_without_a_terminal_event(self):
        response = await _call(
            server.download,
            url="https://www.youtube.com/watch?v=abc",
            audio_format="251",
            convert="false",
            output_dir=str(self.out),
        )
        await response.body_iterator.aclose()   # never consumed → no events

        # An unconsumed stream never starts, so no log is written; admission is
        # still released (covered by test_admission). A started-then-abandoned
        # job logs CANCELLED, which test_wake_lock pins.
        self.assertEqual(list(self.logs.iterdir()), [])
        self.assertFalse(jobs._job_admitted)


class JobStatusTest(unittest.TestCase):
    def test_emit_logs_and_returns_wire_form(self):
        job = jobs.Job("download", url="u")
        wire = job.emit(events._ev_log("hi"))
        self.assertTrue(wire.startswith("data: ") and wire.endswith("\n\n"))
        self.assertEqual(json.loads(wire[6:].strip()),
                         {"type": "log", "msg": "hi"})
        self.assertEqual(len(job.lines), 1)
        self.assertTrue(job.lines[0].endswith("[LOG] hi"))

    def test_status_detection_and_default(self):
        job = jobs.Job("download")
        self.assertEqual(job.status(), "CANCELLED")
        job.emit(events._ev_done("All done!"))
        self.assertEqual(job.status(), "SUCCESS")
        job.emit(events._ev_error("boom"))
        self.assertEqual(job.status(), "FAILED")

    def test_log_file_names(self):
        with mock.patch.object(config, "LOGS_DIR", Path("/logs")):
            download = jobs.Job("download")
            local = jobs.Job("local")
            self.assertTrue(download.log_path().name.endswith("_download.txt"))
            self.assertTrue(local.log_path().name.endswith("_local.txt"))


if __name__ == "__main__":
    unittest.main()
