"""Pure-function + builder unit tests. No subprocess/ffmpeg needed.
Run: .venv/bin/python -m unittest discover -s tests -v
(Use the venv python — server.py resolves yt-dlp/ffmpeg at import time.)"""
import json
import unittest
from pathlib import Path

from fetchforge import server
from yt_dlp.utils import sanitize_filename


class TestUrlValidation(unittest.TestCase):
    def test_accepts_http_https(self):
        self.assertTrue(server.is_http_url("https://youtube.com/watch?v=x"))
        self.assertTrue(server.is_http_url("http://localhost/x"))

    def test_rejects_injection_and_other_schemes(self):
        for bad in ("--exec=calc", "file:///etc/passwd", "ftp://x", "", "   ", None):
            self.assertFalse(server.is_http_url(bad), bad)


class TestOutputStem(unittest.TestCase):
    """_predict_output_stem must match what the post-download path derives from
    yt-dlp's actual --restrict-filenames output (H-8 / #17)."""

    def test_matches_ytdlp_sanitizer(self):
        for title in ["café", "🎮 Game", "Episode 1.", "Foo  —  Bar",
                      "Naïve résumé", "a/b\\c", "plain title 123"]:
            expected = server.sanitize(sanitize_filename(title, restricted=True))
            self.assertEqual(server._predict_output_stem(title), expected, title)


class TestEncodeParams(unittest.TestCase):
    def _p(self, **kw):
        base = dict(height=1080, fps=30.0, video_bitrate_kbps=8000, codec="vp9",
                    pix_fmt="yuv420p", color_transfer="")
        base.update(kw)
        return server.calc_encode_params(**base)

    def test_cq_by_resolution_uhq(self):
        self.assertEqual(self._p(height=2160, nvenc_tune="uhq")["cq"], 22)
        self.assertEqual(self._p(height=1440, nvenc_tune="uhq")["cq"], 24)
        self.assertEqual(self._p(height=1080, nvenc_tune="uhq")["cq"], 26)
        self.assertEqual(self._p(height=720, nvenc_tune="uhq")["cq"], 28)

    def test_hq_cq_boost(self):
        self.assertEqual(self._p(height=1080, nvenc_tune="hq")["cq"], 24)  # 26 - 2

    def test_cq_override_wins(self):
        self.assertEqual(self._p(height=1080, cq_override=19)["cq"], 19)

    def test_4k_maxrate_hard_capped_at_20(self):
        # High source bitrate must not exceed the 4K 20M ceiling (no 60fps bump at 4K).
        p = self._p(height=2160, fps=60.0, video_bitrate_kbps=100000, nvenc_tune="uhq")
        self.assertEqual(p["maxrate"], "20M")

    def test_sub4k_60fps_ceiling_bump(self):
        # 1080p ceiling 7M × 1.40 = 10M at 60fps.
        p = self._p(height=1080, fps=60.0, video_bitrate_kbps=100000, nvenc_tune="uhq")
        self.assertEqual(p["maxrate"], "10M")

    def test_preset_selection(self):
        self.assertEqual(self._p(height=2160, fps=60.0, nvenc_tune="uhq")["preset"], "p3")
        self.assertEqual(self._p(height=1080, fps=60.0, nvenc_tune="uhq")["preset"], "p2")
        self.assertEqual(self._p(height=1080, fps=30.0, nvenc_tune="uhq")["preset"], "p4")
        self.assertEqual(self._p(height=2160, fps=60.0, nvenc_tune="hq")["preset"], "p2")

    def test_hdr_10bit(self):
        p = self._p(color_transfer="smpte2084")
        self.assertEqual(p["pix_fmt"], "yuv420p10le")
        self.assertEqual(p["profile"], "main10")
        self.assertTrue(p["hdr"] and p["ten_bit"])


class TestFfmpegArgs(unittest.TestCase):
    PARAMS = {"cq": 24, "preset": "p2", "maxrate": "7M", "bufsize": "14M",
              "pix_fmt": "yuv420p", "profile": "main", "bf": 3, "b_ref_mode": "middle"}

    def test_hq_has_bf_and_aq(self):
        a = server.build_video_ffmpeg_args(Path("in.mkv"), Path("out.mp4"), self.PARAMS, "hq", "vp9")
        self.assertIn("-bf", a)
        self.assertIn("-spatial_aq", a)
        self.assertIn("-temporal_aq", a)
        self.assertEqual(a[a.index("-preset") + 1], "p2")   # hq keeps p2
        self.assertEqual(a[-1], "out.mp4")
        self.assertIn("hevc_nvenc", a)

    def test_uhq_omits_bf_and_aq_and_bumps_p2(self):
        a = server.build_video_ffmpeg_args(Path("in.mkv"), Path("out.mp4"), self.PARAMS, "uhq", "vp9")
        self.assertNotIn("-bf", a)
        self.assertNotIn("-spatial_aq", a)
        self.assertEqual(a[a.index("-preset") + 1], "p4")   # uhq rejects p2 → p4


class TestSseBuilders(unittest.TestCase):
    """Builders must be byte-identical to the old hand-rolled emits (#9)."""

    def _wire(self, obj):
        return "data: " + json.dumps(obj) + "\n\n"

    def test_msg_events(self):
        self.assertEqual(server.sse_log("hi"), self._wire({"type": "log", "msg": "hi"}))
        self.assertEqual(server.sse_error("e"), self._wire({"type": "error", "msg": "e"}))
        self.assertEqual(server.sse_cancelled(), self._wire({"type": "cancelled", "msg": "Cancelled by user."}))
        self.assertEqual(server.sse_done(), self._wire({"type": "done", "msg": "All done!"}))

    def test_phase_contract(self):
        self.assertEqual(server.sse_phase("Converting", "a.mkv"),
                         self._wire({"type": "phase", "msg": "Converting: a.mkv"}))
        self.assertEqual(server.sse_phase("Resolving..."),
                         self._wire({"type": "phase", "msg": "Resolving..."}))

    def test_structured(self):
        self.assertEqual(server.sse_video_start(2, 5),
                         self._wire({"type": "video_start", "current": 2, "total": 5}))
        self.assertEqual(server.sse_source_size(1234),
                         self._wire({"type": "source_size", "bytes": 1234}))
        self.assertEqual(server.sse_dl_progress(50.0, "1GB", "2MiB/s", "01:00", 1, 3),
                         self._wire({"type": "dl_progress", "pct": 50.0, "size": "1GB",
                                     "speed": "2MiB/s", "eta": "01:00", "idx": 1, "total": 3}))
        self.assertEqual(server.sse_progress(10.0, 5.0, "1x", "30 fps", "5 MB", 999),
                         self._wire({"type": "progress", "pct": 10.0, "overall": 5.0,
                                     "speed": "1x", "eta": "30 fps", "size": "5 MB", "output_bytes": 999}))

    def test_item_outcomes(self):
        self.assertEqual(server.sse_item_done(2, 3),
                         self._wire({"type": "item_done", "idx": 2, "total": 3}))
        self.assertEqual(server.sse_item_failed(2, 3, "x"),
                         self._wire({"type": "item_failed", "idx": 2, "total": 3, "msg": "x"}))

    def test_size_info_bloat(self):
        sb, ob = 1000 * 1024**2, 1200 * 1024**2
        bloat = (ob - sb) / sb * 100
        self.assertEqual(server.sse_size_info(sb, ob),
                         self._wire({"type": "size_info",
                                     "source_mb": round(sb / 1024**2, 1),
                                     "output_mb": round(ob / 1024**2, 1),
                                     "bloat_pct": round(bloat, 1)}))


if __name__ == "__main__":
    unittest.main()
