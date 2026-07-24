import sys
import unittest
from pathlib import Path
from unittest import mock

from fetchforge import server


class TestUpdateTargetDetection(unittest.TestCase):
    """Issue #6: /update-ytdlp must pip-upgrade yt-dlp that lives in this
    interpreter's env, and NOT try to update a system/package-managed binary."""

    def test_venv_ytdlp_is_updatable(self):
        p = str(Path(sys.prefix) / "bin" / "yt-dlp")
        self.assertTrue(server._ytdlp_in_this_env(p))

    def test_system_ytdlp_not_in_env(self):
        # /usr/bin is outside a venv sys.prefix (the case that must fall through
        # to the "use your package manager" message).
        if Path("/usr/bin").resolve().is_relative_to(Path(sys.prefix).resolve()):
            self.skipTest("running under a system interpreter (sys.prefix == /usr)")
        self.assertFalse(server._ytdlp_in_this_env("/usr/bin/yt-dlp"))


class TestLazyToolResolution(unittest.TestCase):
    def test_resolution_is_lazy_not_at_import(self):
        # Module already imported at top without ffmpeg guaranteed present -> no
        # import crash. With nothing resolvable, the getter (not import) is what
        # raises — proving resolution is deferred to call time.
        self.assertTrue(callable(server.get_ffmpeg))
        server.get_ffmpeg.cache_clear()
        with mock.patch("shutil.which", return_value=None), \
             mock.patch.object(server, "IS_WINDOWS", False):
            with self.assertRaises(RuntimeError):
                server.get_ffmpeg()
        server.get_ffmpeg.cache_clear()

    def test_resolution_is_cached(self):
        server.get_ffprobe.cache_clear()
        with mock.patch.object(server, "_resolve_tool", return_value="/x/ffprobe") as m:
            first = server.get_ffprobe()
            second = server.get_ffprobe()
        self.assertEqual(first, second)
        self.assertEqual(m.call_count, 1)  # cached: _resolve_tool invoked once, not twice
        server.get_ffprobe.cache_clear()  # don't leak the patched value to other tests
