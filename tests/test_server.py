"""Pure-function + builder unit tests. No subprocess/ffmpeg needed.
Run: .venv/bin/python -m unittest discover -s tests -v
(Use the venv python — server.py resolves yt-dlp/ffmpeg at import time.)"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fetchforge import server
from yt_dlp.utils import sanitize_filename


class TestStaleCookieDetection(unittest.TestCase):
    """Issue #9: yt-dlp's rotated-cookie warning must flip cookies off for the run."""

    def setUp(self):
        server._cookies_disabled = False

    def tearDown(self):
        server._cookies_disabled = False

    def test_flags_and_drops_cookies_on_rotation_warning(self):
        with tempfile.TemporaryDirectory() as d:
            ck = Path(d) / "cookies.txt"
            ck.write_text("x")
            with mock.patch.object(server, "COOKIES_PATH", ck):
                self.assertEqual(server.cookie_args(), ["--cookies", str(ck)])
                line = ("WARNING: [youtube] The provided YouTube account cookies are "
                        "no longer valid. They have likely been rotated in the browser.")
                msg = server._maybe_flag_stale_cookies(line)
                self.assertIsNotNone(msg)
                self.assertTrue(server._cookies_disabled)
                self.assertEqual(server.cookie_args(), [])            # dropped for the run
                self.assertIsNone(server._maybe_flag_stale_cookies(line))  # one-shot

    def test_benign_line_does_not_flag(self):
        with tempfile.TemporaryDirectory() as d:
            ck = Path(d) / "cookies.txt"
            ck.write_text("x")
            with mock.patch.object(server, "COOKIES_PATH", ck):
                self.assertIsNone(server._maybe_flag_stale_cookies("[download]  50% of 10MiB"))
                self.assertFalse(server._cookies_disabled)

    def test_no_flag_when_no_cookies_loaded(self):
        with tempfile.TemporaryDirectory() as d:
            ck = Path(d) / "cookies.txt"   # never created
            with mock.patch.object(server, "COOKIES_PATH", ck):
                self.assertIsNone(server._maybe_flag_stale_cookies("cookies are no longer valid"))
                self.assertFalse(server._cookies_disabled)

    def test_sse_cookie_warning_shape(self):
        s = server.sse_cookie_warning("hi")
        self.assertTrue(s.startswith("data: ") and s.endswith("\n\n"))
        self.assertEqual(json.loads(s[6:].strip()), {"type": "cookie_warning", "msg": "hi"})


class TestVideoFormatSelector(unittest.TestCase):
    """Issue #13: playlists (mixed 720p/1080p) take highest available; a single
    video / batch item honors the pick but never hard-fails."""

    def test_playlist_takes_highest(self):
        self.assertEqual(server._video_format_selector("298", "140", prefer_picked=False),
                         "bv*+ba/b")

    def test_single_honors_pick_with_fallback(self):
        self.assertEqual(server._video_format_selector("298", "140", prefer_picked=True),
                         "298+140/bv*+ba/b")

    def test_falls_back_to_highest_when_pick_missing(self):
        self.assertEqual(server._video_format_selector("", "", prefer_picked=True),
                         "bv*+ba/b")


class TestCookieFetch(unittest.TestCase):
    """Issue #12: browser cookie scanning — profile enumeration + counting."""

    def test_chromium_profiles_from_local_state(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "Local State").write_text(json.dumps({
                "profile": {"info_cache": {
                    "Default": {"name": "Pétur Heima"},
                    "Profile 1": {"name": "Pétur Vinna"},
                }}
            }), encoding="utf-8")
            profs = dict(server._chromium_profiles(root))
            self.assertEqual(profs["Default"], "Pétur Heima")
            self.assertEqual(profs["Profile 1"], "Pétur Vinna")

    def test_chromium_profiles_fallback_scans_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "Default").mkdir()
            (root / "Profile 2").mkdir()
            (root / "Cache").mkdir()          # not a profile dir
            profs = dict(server._chromium_profiles(root))
            self.assertIn("Default", profs)
            self.assertIn("Profile 2", profs)
            self.assertNotIn("Cache", profs)

    def test_chromium_profiles_default_when_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(server._chromium_profiles(Path(d)), [("Default", "Default")])

    def test_browser_roots_has_expected_browsers(self):
        roots = server._browser_roots()
        for b in ("brave", "chrome", "chromium", "edge", "vivaldi", "opera"):
            self.assertIn(b, roots)

    def test_count_youtube_cookies(self):
        class _C:
            def __init__(self, domain): self.domain = domain
        jar = [_C(".youtube.com"), _C(".google.com"), _C(".example.com"), _C("")]
        total, yt = server._count_youtube_cookies(jar)
        self.assertEqual(total, 4)
        self.assertEqual(yt, 2)


class TestNewestNewFile(unittest.TestCase):
    """Issue #8: the pipeline fallback must pick only a file produced by THIS
    download, never a sibling video's MKV already sitting in the shared cache."""

    def test_ignores_pre_existing_sibling(self):
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d)
            (cache / "part1.mkv").write_text("x")     # previous video, still in cache
            pre = set(cache.glob("*.mkv"))
            # A failed download produced nothing new -> must NOT grab part1.
            self.assertIsNone(server._newest_new_file(cache, pre))
            # A successful download drops part2 -> that's the one we want.
            current = cache / "part2.mkv"
            current.write_text("y")
            self.assertEqual(server._newest_new_file(cache, pre), current)

    def test_returns_newest_of_multiple_new(self):
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d)
            a, b = cache / "a.mkv", cache / "b.mkv"
            a.write_text("a"); b.write_text("b")
            os.utime(a, (1000, 1000)); os.utime(b, (2000, 2000))
            self.assertEqual(server._newest_new_file(cache, set()), b)

    def test_none_when_nothing_new(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(server._newest_new_file(Path(d), set()))


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

    def test_output_height_drives_bitrate_math(self):
        p = self._p(height=1080, fps=30.0, video_bitrate_kbps=100000, codec="h264")
        self.assertEqual(p["cq"], 26)
        self.assertEqual(p["maxrate"], "7M")


class TestDecodeFilterArgs(unittest.TestCase):
    """_decode_filter_args filter-chain unit tests (issue #18)."""

    def setUp(self):
        self._lib = server.HAS_LIBPLACEBO
        self._cuda = server.HAS_SCALE_CUDA

    def tearDown(self):
        server.HAS_LIBPLACEBO = self._lib
        server.HAS_SCALE_CUDA = self._cuda

    @staticmethod
    def _vf(args):
        return args[args.index("-vf") + 1]

    # -- passthrough (target_height=0, byte-identical) --

    def test_passthrough_gpu(self):
        server.HAS_LIBPLACEBO = False
        server.HAS_SCALE_CUDA = True
        a = server._decode_filter_args(Path("in.mkv"), "yuv420p", "h264_cuvid")
        vf = self._vf(a)
        self.assertEqual(vf, "scale_cuda=format=yuv420p")
        self.assertIn("-hwaccel_output_format", a)

    def test_passthrough_cpu(self):
        server.HAS_LIBPLACEBO = False
        server.HAS_SCALE_CUDA = False
        a = server._decode_filter_args(Path("in.mkv"), "yuv420p", None)
        vf = self._vf(a)
        self.assertEqual(vf, "format=yuv420p")
        self.assertNotIn("-hwaccel_output_format", a)

    def test_passthrough_hdr_10bit_gpu(self):
        server.HAS_LIBPLACEBO = False
        server.HAS_SCALE_CUDA = True
        a = server._decode_filter_args(Path("in.mkv"), "yuv420p10le", "hevc_cuvid")
        self.assertIn("yuv420p10le", self._vf(a))

    # -- scaling: libplacebo path --

    def test_downscale_libplacebo(self):
        server.HAS_LIBPLACEBO = True
        server.HAS_SCALE_CUDA = False
        a = server._decode_filter_args(Path("in.mkv"), "yuv420p", None, target_height=720)
        vf = self._vf(a)
        self.assertIn("libplacebo=", vf)
        self.assertIn("w=-2:h=720", vf)
        self.assertIn("upscaler=ewa_lanczos", vf)
        self.assertIn("downscaler=ewa_lanczos", vf)
        self.assertIn("tonemapping=none", vf)

    def test_upscale_libplacebo(self):
        server.HAS_LIBPLACEBO = True
        server.HAS_SCALE_CUDA = False
        a = server._decode_filter_args(Path("in.mkv"), "yuv420p", "h264_cuvid", target_height=2160)
        vf = self._vf(a)
        self.assertIn("h=2160", vf)

    def test_hdr_10bit_preserved_libplacebo(self):
        server.HAS_LIBPLACEBO = True
        server.HAS_SCALE_CUDA = False
        a = server._decode_filter_args(Path("in.mkv"), "yuv420p10le", None, target_height=1080)
        vf = self._vf(a)
        self.assertIn("yuv420p10le", vf)
        self.assertIn("tonemapping=none", vf)

    def test_libplacebo_preferred_over_scale_cuda(self):
        server.HAS_LIBPLACEBO = True
        server.HAS_SCALE_CUDA = True
        a = server._decode_filter_args(Path("in.mkv"), "yuv420p", "h264_cuvid", target_height=1080)
        self.assertIn("libplacebo=", self._vf(a))
        self.assertNotIn("scale_cuda", self._vf(a))

    # -- scaling: scale_cuda path --

    def test_downscale_cuda(self):
        server.HAS_LIBPLACEBO = False
        server.HAS_SCALE_CUDA = True
        a = server._decode_filter_args(Path("in.mkv"), "yuv420p", "h264_cuvid", target_height=720)
        vf = self._vf(a)
        self.assertEqual(vf, "scale_cuda=format=yuv420p:w=-2:h=720")
        self.assertIn("-hwaccel_output_format", a)

    def test_upscale_cuda(self):
        server.HAS_LIBPLACEBO = False
        server.HAS_SCALE_CUDA = True
        a = server._decode_filter_args(Path("in.mkv"), "yuv420p10le", None, target_height=2160)
        self.assertIn("h=2160", self._vf(a))
        self.assertIn("yuv420p10le", self._vf(a))

    # -- scaling: CPU fallback --

    def test_downscale_cpu(self):
        server.HAS_LIBPLACEBO = False
        server.HAS_SCALE_CUDA = False
        a = server._decode_filter_args(Path("in.mkv"), "yuv420p", None, target_height=720)
        vf = self._vf(a)
        self.assertEqual(vf, "scale=w=-2:h=720:flags=lanczos,format=yuv420p")

    def test_upscale_cpu(self):
        server.HAS_LIBPLACEBO = False
        server.HAS_SCALE_CUDA = False
        a = server._decode_filter_args(Path("in.mkv"), "yuv420p10le", None, target_height=2160)
        vf = self._vf(a)
        self.assertIn("h=2160", vf)
        self.assertIn("yuv420p10le", vf)


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

    def test_passthrough_target_height_zero_omits_scale_dims(self):
        a = server.build_video_ffmpeg_args(Path("in.mkv"), Path("out.mp4"), self.PARAMS, "hq", "vp9")
        vf = a[a.index("-vf") + 1]
        self.assertNotIn("h=", vf)
        self.assertNotIn("w=", vf)


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
