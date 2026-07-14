import unittest
from unittest import mock

from fetchforge import server


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
