import os
import unittest
from pathlib import Path
from unittest import mock
from fetchforge import provision


class TestUsePreservesPath(unittest.TestCase):
    """Issue #7: the ffmpeg preflight must not reorder PATH when ffmpeg is already
    discoverable there — prepending /usr/bin would shadow the venv's newer yt-dlp."""

    def test_leaves_path_untouched_when_already_on_path(self):
        with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             mock.patch.dict(os.environ, {"PATH": "/venv/bin:/usr/bin"}, clear=True):
            provision._use("/usr/bin/ffmpeg")
            self.assertEqual(os.environ["PATH"], "/venv/bin:/usr/bin")

    def test_prepends_when_off_path(self):
        # e.g. the Windows auto-provisioned build under %LOCALAPPDATA%, not on PATH.
        with mock.patch("shutil.which", return_value=None), \
             mock.patch.dict(os.environ, {"PATH": "/venv/bin:/usr/bin"}, clear=True):
            provision._use("/opt/ff/ffmpeg")
            # _use() resolves the path before splitting off the parent dir, so the
            # expected prefix must go through the same resolve() (adds a drive
            # letter on Windows) rather than a hardcoded POSIX literal.
            expected_dir = str(Path("/opt/ff/ffmpeg").resolve().parent)
            self.assertTrue(os.environ["PATH"].startswith(expected_dir + os.pathsep))


class TestNvencDetection(unittest.TestCase):
    def test_true_when_hevc_nvenc_listed(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(stdout="V..... hevc_nvenc NVIDIA NVENC hevc encoder", returncode=0)
            self.assertTrue(provision.ffmpeg_has_nvenc("ffmpeg"))

    def test_false_when_absent(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(stdout="V..... libx265 x265", returncode=0)
            self.assertFalse(provision.ffmpeg_has_nvenc("ffmpeg"))

    def test_false_when_ffmpeg_missing(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertFalse(provision.ffmpeg_has_nvenc("ffmpeg"))

    def test_provision_windows_extracts_binaries(self):
        import io, zipfile, tempfile
        from pathlib import Path as P

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("ffmpeg-x/bin/ffmpeg.exe", b"MZ-fake")
            zf.writestr("ffmpeg-x/bin/ffprobe.exe", b"MZ-fake")
        data = buf.getvalue()

        with tempfile.TemporaryDirectory() as tmp:
            cache = P(tmp) / "ffmpeg"
            def fake_dl(url, dest):
                P(dest).parent.mkdir(parents=True, exist_ok=True)
                P(dest).write_bytes(data)
            # provision_windows now re-verifies NVENC on the downloaded build; the
            # fake .exe can't be executed, so stub the check as present.
            with mock.patch.object(provision, "FFMPEG_CACHE", cache), \
                 mock.patch.object(provision, "ffmpeg_has_nvenc", return_value=True):
                out = provision.provision_windows(download=fake_dl)
            self.assertTrue(out.endswith("ffmpeg.exe"))
            self.assertTrue((cache / "ffprobe.exe").exists())

    def test_provision_windows_without_nvenc_raises(self):
        # A downloaded build that extracts fine but lacks hevc_nvenc must be
        # rejected, not silently used (guards against a wrong/changed asset).
        import io, zipfile, tempfile
        from pathlib import Path as P

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("ffmpeg-x/bin/ffmpeg.exe", b"MZ-fake")
            zf.writestr("ffmpeg-x/bin/ffprobe.exe", b"MZ-fake")
        data = buf.getvalue()

        with tempfile.TemporaryDirectory() as tmp:
            cache = P(tmp) / "ffmpeg"
            def fake_dl(url, dest):
                P(dest).parent.mkdir(parents=True, exist_ok=True)
                P(dest).write_bytes(data)
            with mock.patch.object(provision, "FFMPEG_CACHE", cache), \
                 mock.patch.object(provision, "ffmpeg_has_nvenc", return_value=False):
                with self.assertRaises(provision.ProvisionError):
                    provision.provision_windows(download=fake_dl)

    def test_provision_windows_missing_ffmpeg_exe_raises(self):
        import io, zipfile, tempfile
        from pathlib import Path as P

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("ffmpeg-x/readme.txt", b"no ffmpeg here")
        data = buf.getvalue()

        with tempfile.TemporaryDirectory() as tmp:
            cache = P(tmp) / "ffmpeg"
            def fake_dl(url, dest):
                P(dest).parent.mkdir(parents=True, exist_ok=True)
                P(dest).write_bytes(data)
            with mock.patch.object(provision, "FFMPEG_CACHE", cache):
                with self.assertRaises(provision.ProvisionError):
                    provision.provision_windows(download=fake_dl)


class TestFindNvencFfmpeg(unittest.TestCase):
    def test_returns_cached_path_when_it_passes_nvenc(self):
        with mock.patch.object(provision, "_cached_ffmpeg", return_value="/cache/ffmpeg"), \
             mock.patch.object(provision, "ffmpeg_has_nvenc", return_value=True), \
             mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            self.assertEqual(provision.find_nvenc_ffmpeg(), "/cache/ffmpeg")

    def test_falls_back_to_path_ffmpeg_when_no_cache(self):
        with mock.patch.object(provision, "_cached_ffmpeg", return_value=None), \
             mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             mock.patch.object(provision, "ffmpeg_has_nvenc", return_value=True):
            self.assertEqual(provision.find_nvenc_ffmpeg(), "/usr/bin/ffmpeg")

    def test_returns_none_when_nothing_resolvable(self):
        with mock.patch.object(provision, "_cached_ffmpeg", return_value=None), \
             mock.patch("shutil.which", return_value=None):
            self.assertIsNone(provision.find_nvenc_ffmpeg())

    def test_rejects_candidate_without_nvenc_and_falls_through(self):
        # cached ffmpeg exists but lacks NVENC -> skipped; PATH ffmpeg has NVENC -> returned
        with mock.patch.object(provision, "_cached_ffmpeg", return_value="/cache/ffmpeg"), \
             mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             mock.patch.object(provision, "ffmpeg_has_nvenc",
                               side_effect=lambda p: p == "/usr/bin/ffmpeg"):
            self.assertEqual(provision.find_nvenc_ffmpeg(), "/usr/bin/ffmpeg")

    def test_returns_none_when_only_candidate_lacks_nvenc(self):
        # a present ffmpeg without NVENC must NOT be returned
        with mock.patch.object(provision, "_cached_ffmpeg", return_value=None), \
             mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             mock.patch.object(provision, "ffmpeg_has_nvenc", return_value=False):
            self.assertIsNone(provision.find_nvenc_ffmpeg())


class TestEnsureFfmpeg(unittest.TestCase):
    def test_returns_existing_when_found(self):
        with mock.patch.object(provision, "find_nvenc_ffmpeg", return_value="/x/ffmpeg"):
            self.assertEqual(provision.ensure_ffmpeg(), "/x/ffmpeg")

    def test_windows_provisions_when_none_found(self):
        with mock.patch.object(provision, "find_nvenc_ffmpeg", return_value=None), \
             mock.patch.object(provision, "IS_WINDOWS", True), \
             mock.patch.object(provision, "IS_MACOS", False), \
             mock.patch.object(provision, "provision_windows", return_value="/w/ffmpeg.exe") as pw:
            self.assertEqual(provision.ensure_ffmpeg(), "/w/ffmpeg.exe")
            pw.assert_called_once()

    def test_macos_raises_provision_error_when_none_found(self):
        with mock.patch.object(provision, "find_nvenc_ffmpeg", return_value=None), \
             mock.patch.object(provision, "IS_WINDOWS", False), \
             mock.patch.object(provision, "IS_MACOS", True):
            with self.assertRaises(provision.ProvisionError):
                provision.ensure_ffmpeg()

    def test_linux_raises_provision_error_when_none_found(self):
        with mock.patch.object(provision, "find_nvenc_ffmpeg", return_value=None), \
             mock.patch.object(provision, "IS_WINDOWS", False), \
             mock.patch.object(provision, "IS_MACOS", False):
            with self.assertRaises(provision.ProvisionError):
                provision.ensure_ffmpeg()

    def test_use_prepends_ffmpeg_dir_to_path(self):
        import os
        orig = os.environ.get("PATH", "")
        try:
            os.environ["PATH"] = "/orig/bin"
            result = provision._use("/opt/ff/ffmpeg")
            self.assertEqual(result, "/opt/ff/ffmpeg")           # returns path unchanged
            # Derived (not hardcoded) expected prefix: _use() resolves the path
            # before taking .parent, which adds a drive letter on Windows.
            expected_dir = str(Path("/opt/ff/ffmpeg").resolve().parent)
            self.assertTrue(os.environ["PATH"].startswith(expected_dir + os.pathsep))  # dir prepended
            self.assertIn("/orig/bin", os.environ["PATH"])       # original PATH preserved
        finally:
            os.environ["PATH"] = orig                            # never leak PATH mutation
