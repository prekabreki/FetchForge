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


class TestEncodePlan(unittest.TestCase):
    """_encode_plan is the single source of truth for the params / argv / log
    lines the three call sites used to build by hand (H-2)."""

    SRC = {
        "height": 2160, "fps": 60.0, "video_bitrate_kbps": 100000,
        "codec": "vp9", "pix_fmt": "yuv420p", "color_transfer": "",
    }
    OPTS = {
        "tune_mode": "hq", "cq": 0, "maxrate": "25M", "preset": "",
        "tune": "", "pix_fmt": "", "target_res": 0, "sharpen": False,
    }

    def test_matches_pre_refactor_construction_and_logs(self):
        input_file, output_file = Path("in.mkv"), Path("out.mp4")
        seek = ["-ss", "5.000", "-t", "25"]

        params, argv, logs = server._encode_plan(
            self.SRC, input_file, output_file, self.OPTS, input_opts=seek)

        expected_params = server.calc_encode_params(
            height=2160, fps=60.0, video_bitrate_kbps=100000, codec="vp9",
            pix_fmt="yuv420p", color_transfer="",
            cq_override=0, maxrate_override="25M", nvenc_tune="hq",
            target_res=0, sharpen=False, preset_override="", tune_override="",
            pix_fmt_override="", probe_tune=server._nvenc_tune,
        )
        self.assertEqual(params, expected_params)

        expected_argv = server.build_video_ffmpeg_args(
            input_file, output_file, expected_params, expected_params["tune"],
            "vp9", target_height=server._effective_target_height(2160, 0),
            sharpen=False, input_opts=seek,
        )
        self.assertEqual(argv, expected_argv)
        self.assertEqual(params["maxrate"], "20M")   # 25M clamped on 4K
        self.assertIn("-ss", argv[:argv.index("-i")])

        self.assertEqual(logs, [
            "Encode params: CQ=20 maxrate=20M preset=p2 tune=hq pix=yuv420p",
            "Override maxrate=25M clamped → 20M",
        ])

    def test_local_conversion_passes_no_input_opts(self):
        argv = server._encode_plan(
            self.SRC, Path("in.mkv"), Path("out.mp4"), self.OPTS)[1]
        self.assertNotIn("-ss", argv)
        self.assertNotIn("-t", argv)


if __name__ == "__main__":
    unittest.main()
