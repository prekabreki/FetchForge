import unittest
from unittest import mock
from fetchforge import cli, provision


class TestCli(unittest.TestCase):
    def test_main_runs_preflight_then_server(self):
        with mock.patch.object(provision, "ensure_ffmpeg", return_value="ffmpeg") as pre, \
             mock.patch("fetchforge.server.run_server") as run:
            rc = cli.main([])
        pre.assert_called_once()
        run.assert_called_once()
        self.assertEqual(rc, 0)

    def test_main_reports_provision_error_without_crashing_when_no_ffmpeg(self):
        with mock.patch.object(provision, "ensure_ffmpeg",
                               side_effect=provision.ProvisionError("install ffmpeg")), \
             mock.patch("shutil.which", return_value=None), \
             mock.patch("fetchforge.server.run_server") as run:
            rc = cli.main([])
        run.assert_not_called()
        self.assertEqual(rc, 1)

    def test_main_starts_without_nvenc_when_plain_ffmpeg_present(self):
        with mock.patch.object(provision, "ensure_ffmpeg",
                               side_effect=provision.ProvisionError("no NVENC ffmpeg found")), \
             mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
             mock.patch("fetchforge.server.run_server") as run:
            rc = cli.main([])
        run.assert_called_once()
        self.assertEqual(rc, 0)

    def test_main_runs_preflight_before_server(self):
        from unittest import mock
        manager = mock.Mock()
        with mock.patch.object(provision, "ensure_ffmpeg", return_value="ffmpeg") as pre, \
             mock.patch("fetchforge.server.run_server") as run:
            manager.attach_mock(pre, "pre")
            manager.attach_mock(run, "run")
            rc = cli.main([])
        self.assertEqual(rc, 0)
        names = [c[0] for c in manager.mock_calls]
        self.assertLess(names.index("pre"), names.index("run"))  # ensure_ffmpeg before run_server
