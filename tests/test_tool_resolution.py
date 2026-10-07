import asyncio
import site
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
        self.assertTrue(server._ytdlp_in_this_env([p]))

    def test_module_invocation_is_updatable(self):
        # `python -m yt_dlp` runs out of this interpreter by construction, so
        # /update-ytdlp must pick the pip path, not the "can't update it" message.
        self.assertTrue(server._ytdlp_in_this_env([sys.executable, "-m", "yt_dlp"]))

    def test_system_ytdlp_not_in_env(self):
        # /usr/bin is outside a venv sys.prefix (the case that must fall through
        # to the "use your package manager" message).
        for root in (sys.prefix, site.getuserbase()):
            if Path("/usr/bin").resolve().is_relative_to(Path(root).resolve()):
                self.skipTest("running under a system interpreter (prefix == /usr)")
        self.assertFalse(server._ytdlp_in_this_env(["/usr/bin/yt-dlp"]))


class TestYtdlpArgvResolution(unittest.TestCase):
    """Regression: under `pip install --user` / Windows Store Python the yt-dlp
    console script lands in a dir that is on neither PATH nor sys.prefix, and
    every yt-dlp call site 500'd even though the module was importable."""

    def setUp(self):
        server.get_ytdlp_argv.cache_clear()
        self.addCleanup(server.get_ytdlp_argv.cache_clear)

    def test_falls_back_to_module_when_no_console_script(self):
        with mock.patch("shutil.which", return_value=None), \
             mock.patch.object(server, "IS_WINDOWS", False), \
             mock.patch.object(server, "sys") as fake_sys:
            fake_sys.prefix = "/nonexistent-prefix"
            fake_sys.executable = "/usr/bin/python3"
            self.assertEqual(server.get_ytdlp_argv(),
                             ["/usr/bin/python3", "-m", "yt_dlp"])

    def test_console_script_on_path_wins(self):
        with mock.patch("shutil.which", return_value="/venv/bin/yt-dlp"), \
             mock.patch.object(server, "IS_WINDOWS", False):
            self.assertEqual(server.get_ytdlp_argv(), ["/venv/bin/yt-dlp"])

    def test_raises_only_when_module_is_absent_too(self):
        with mock.patch("shutil.which", return_value=None), \
             mock.patch.object(server, "IS_WINDOWS", False), \
             mock.patch("importlib.util.find_spec", return_value=None), \
             mock.patch.object(server, "sys") as fake_sys:
            fake_sys.prefix = "/nonexistent-prefix"
            with self.assertRaises(RuntimeError):
                server.get_ytdlp_argv()


class TestUpdateYtdlpBranches(unittest.TestCase):
    """Issue #79: a venv made by `uv venv`/`uv sync` has no pip, so /update-ytdlp
    must fall back to `uv pip install --python sys.executable`, and say so plainly
    when neither tool exists. Subprocess is mocked throughout (no network)."""

    @staticmethod
    def _fake_proc(returncode=0, output=b"ok"):
        proc = mock.Mock()
        proc.returncode = returncode
        proc.communicate = mock.AsyncMock(return_value=(output, None))
        return proc

    def _run(self, proc, find_spec_return, which_return=None):
        spawn = mock.AsyncMock(return_value=proc)
        with mock.patch.object(server, "get_ytdlp_argv", return_value=["/venv/bin/yt-dlp"]), \
             mock.patch.object(server, "_ytdlp_in_this_env", return_value=True), \
             mock.patch.object(server, "IS_WINDOWS", False), \
             mock.patch("importlib.util.find_spec", return_value=find_spec_return), \
             mock.patch("shutil.which", return_value=which_return), \
             mock.patch("asyncio.create_subprocess_exec", new=spawn):
            return asyncio.run(server.update_ytdlp()), spawn

    def test_pip_path_unchanged_when_pip_available(self):
        out, spawn = self._run(self._fake_proc(), find_spec_return=object())
        self.assertEqual(
            spawn.await_args.args,
            (sys.executable, "-m", "pip", "install", "-U", "yt-dlp[default]"))
        self.assertEqual(out["output"], "ok")

    def test_uv_path_when_pip_absent(self):
        out, spawn = self._run(self._fake_proc(), find_spec_return=None,
                               which_return="/usr/local/bin/uv")
        self.assertEqual(
            spawn.await_args.args,
            ("/usr/local/bin/uv", "pip", "install", "--python", sys.executable,
             "-U", "yt-dlp[default]"))
        self.assertEqual(out["output"], "ok")

    def test_neither_pip_nor_uv_names_both_commands(self):
        out, spawn = self._run(self._fake_proc(), find_spec_return=None,
                               which_return=None)
        spawn.assert_not_awaited()
        self.assertNotIn("No module named", out["output"])
        self.assertIn(f"{sys.executable} -m pip install -U", out["output"])
        self.assertIn(f"uv pip install --python {sys.executable}", out["output"])

    def test_successful_upgrade_clears_argv_cache(self):
        ytdlp = mock.MagicMock(return_value=["/venv/bin/yt-dlp"])
        with mock.patch.object(server, "get_ytdlp_argv", ytdlp), \
             mock.patch.object(server, "_ytdlp_in_this_env", return_value=True), \
             mock.patch.object(server, "IS_WINDOWS", False), \
             mock.patch("importlib.util.find_spec", return_value=object()), \
             mock.patch("asyncio.create_subprocess_exec",
                        new=mock.AsyncMock(return_value=self._fake_proc())):
            asyncio.run(server.update_ytdlp())
        ytdlp.cache_clear.assert_called_once()

    def test_failed_upgrade_keeps_argv_cache(self):
        ytdlp = mock.MagicMock(return_value=["/venv/bin/yt-dlp"])
        with mock.patch.object(server, "get_ytdlp_argv", ytdlp), \
             mock.patch.object(server, "_ytdlp_in_this_env", return_value=True), \
             mock.patch.object(server, "IS_WINDOWS", False), \
             mock.patch("importlib.util.find_spec", return_value=object()), \
             mock.patch("asyncio.create_subprocess_exec",
                        new=mock.AsyncMock(return_value=self._fake_proc(returncode=1))):
            asyncio.run(server.update_ytdlp())
        ytdlp.cache_clear.assert_not_called()


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
