"""run_encode + _download_is_complete against a REAL (CPU) ffmpeg — no NVENC/GPU.
Generates a short clip with ffmpeg's testsrc. Skipped if ffmpeg is unavailable."""
import asyncio
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from fetchforge import server

_HAS_FFMPEG = shutil.which("ffmpeg") is not None


@unittest.skipUnless(_HAS_FFMPEG, "ffmpeg not on PATH")
class TestRunEncode(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="dlpr_test_"))
        cls.clip = cls.tmp / "clip.mkv"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=d=5:s=320x240",
             "-c:v", "libx264", str(cls.clip)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        server._cancel_requested.clear()

    def tearDown(self):
        server._cancel_requested.clear()

    def _cpu_args(self, out):
        return ["ffmpeg", "-y", "-loglevel", "info", "-i", str(self.clip),
                "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                "-progress", "pipe:1", "-nostats", str(out)]

    async def test_success_emits_progress_and_log(self):
        out = self.tmp / "out.mp4"
        res, got_progress, got_log = {}, False, False
        async for s in server.run_encode(self._cpu_args(out), duration_secs=5.0,
                                         idx=1, total=1, result=res):
            if '"type": "progress"' in s:
                got_progress = True
            if '"type": "log"' in s:
                got_log = True
        self.assertEqual(res["returncode"], 0)
        self.assertTrue(out.exists() and out.stat().st_size > 0)
        self.assertTrue(got_progress, "no progress events")
        self.assertTrue(got_log, "no stderr/log events")
        self.assertIsNone(server.current_process)

    async def test_cancel_terminates_with_nonzero_rc(self):
        out = self.tmp / "out_cancel.mp4"
        args = ["ffmpeg", "-y", "-loglevel", "info", "-f", "lavfi",
                "-i", "testsrc=d=30:s=640x480", "-c:v", "libx264", "-preset", "veryslow",
                "-progress", "pipe:1", "-nostats", str(out)]
        res = {}

        async def canceller():
            await asyncio.sleep(0.5)
            server._cancel_requested.set()

        asyncio.create_task(canceller())
        async for _ in server.run_encode(args, duration_secs=30.0, idx=1, total=1, result=res):
            pass
        self.assertNotEqual(res["returncode"], 0)
        self.assertIsNone(server.current_process)


@unittest.skipUnless(_HAS_FFMPEG, "ffmpeg not on PATH")
class TestDownloadIsComplete(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="dlpr_dic_"))
        cls.clip = cls.tmp / "clip.mkv"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=d=5:s=320x240",
             "-c:v", "libx264", str(cls.clip)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    async def test_known_size(self):
        sz = self.clip.stat().st_size
        self.assertTrue(await server._download_is_complete(self.clip, int(sz * 0.9)))
        self.assertFalse(await server._download_is_complete(self.clip, sz * 2))

    async def test_duration_when_size_unknown(self):
        self.assertTrue(await server._download_is_complete(self.clip, 0, 5.0))    # matches ~5s
        self.assertFalse(await server._download_is_complete(self.clip, 0, 60.0))  # truncated
        self.assertFalse(await server._download_is_complete(self.clip, 0, 0))     # unverifiable

    async def test_missing_file(self):
        self.assertFalse(await server._download_is_complete(self.tmp / "nope.mkv", 0, 5.0))


if __name__ == "__main__":
    unittest.main()
