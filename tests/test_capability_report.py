"""Issue #47 — the startup capability report and its ffmpeg warning.

Three startup probes (the uhq NVENC tune, scale_cuda, libplacebo) each silently
select a lesser path when they fail. These tests pin the report that makes that
visible, and in particular pin the two things the issue's design note and danger
zone care about:

  * The PROBES are the gate. The version string only composes the message — an
    ancient version with all probes passing must produce NO warning, and a missing
    version must not suppress a warning the probes earned.
  * The report runs in `lifespan`. A missing binary, an exploding version read or
    garbage ffmpeg output must never stop the server starting.

Pure functions throughout — no ffmpeg, no GPU, no subprocess.

Run: python -m unittest tests.test_capability_report -v
"""
import asyncio
import unittest
from unittest import mock

from fetchforge import server


# Real first line from the 2023-10-29 gyan build that triggered the issue.
GYAN_2023_VERSION_OUT = """\
ffmpeg version 2023-10-29-git-2532e832d2-full_build-www.gyan.dev Copyright (c) 2000-2023 the FFmpeg developers
built with gcc 12.2.0 (Rev10, Built by MSYS2 project)
configuration: --enable-gpl --enable-libnpp --enable-libplacebo
"""

FEDORA_VERSION_OUT = "ffmpeg version 6.1.1 Copyright (c) 2000-2023 the FFmpeg developers\n"

BTBN_VERSION_OUT = "ffmpeg version n7.1-11-g1b6d4b0b0d Copyright (c) 2000-2024 the FFmpeg developers\n"

WINDOWS_PATH = r"C:\Users\preka\Documents\ffmpeg\bin\ffmpeg.EXE"


def report(*, tune_cause=server._TUNE_SUPPORTED, version="7.1.1",
           path=WINDOWS_PATH, scale_cuda=True, libplacebo=True):
    """The pure builder with sane defaults; override one axis per test."""
    return server.build_capability_report(
        ffmpeg_path=path,
        ffmpeg_version=version,
        tune_cause=tune_cause,
        has_scale_cuda=scale_cuda,
        has_libplacebo=libplacebo,
    )


class TestVersionParsing(unittest.TestCase):
    """The version string is cosmetic, but a wrong one is worse than none."""

    def test_parses_the_gyan_git_build_string(self):
        self.assertEqual(server._parse_ffmpeg_version(GYAN_2023_VERSION_OUT),
                         "2023-10-29-git-2532e832d2-full_build-www.gyan.dev")

    def test_parses_a_plain_distro_version(self):
        self.assertEqual(server._parse_ffmpeg_version(FEDORA_VERSION_OUT), "6.1.1")

    def test_parses_a_btbn_git_describe_version(self):
        self.assertEqual(server._parse_ffmpeg_version(BTBN_VERSION_OUT),
                         "n7.1-11-g1b6d4b0b0d")

    def test_garbage_output_yields_empty_not_a_bogus_version(self):
        for junk in ("", "   ", "not ffmpeg at all\n", "\x00\x01binary noise"):
            self.assertEqual(server._parse_ffmpeg_version(junk), "")

    def test_none_is_tolerated(self):
        self.assertEqual(server._parse_ffmpeg_version(None), "")

    def test_read_returns_empty_when_ffmpeg_cannot_be_resolved(self):
        with mock.patch.object(server, "get_ffmpeg", side_effect=RuntimeError("no ffmpeg")):
            self.assertEqual(server._read_ffmpeg_version(), "")

    def test_read_returns_empty_when_the_subprocess_explodes(self):
        with mock.patch.object(server, "get_ffmpeg", return_value="ffmpeg"), \
             mock.patch("subprocess.run", side_effect=OSError("boom")):
            self.assertEqual(server._read_ffmpeg_version(), "")

    def test_read_returns_empty_on_unrecognised_output(self):
        done = mock.Mock(stdout="???", stderr="")
        with mock.patch.object(server, "get_ffmpeg", return_value="ffmpeg"), \
             mock.patch("subprocess.run", return_value=done):
            self.assertEqual(server._read_ffmpeg_version(), "")


class TestWarningComposition(unittest.TestCase):
    """Given a report, does the warning say the three things the user needs:
    which binary, what version, and what to do about it?"""

    def test_old_ffmpeg_names_path_version_and_the_ffmpeg_remedy(self):
        w = report(tune_cause=server._TUNE_FFMPEG_TOO_OLD,
                   version="2023-10-29-git-2532e832d2-full_build-www.gyan.dev")["warning"]
        self.assertIsNotNone(w)
        self.assertEqual(w["ffmpeg_path"], WINDOWS_PATH)
        self.assertEqual(w["ffmpeg_version"],
                         "2023-10-29-git-2532e832d2-full_build-www.gyan.dev")
        self.assertIn(WINDOWS_PATH, w["text"])
        self.assertIn("2023-10-29-git", w["text"])
        self.assertIn("Update ffmpeg", w["text"])
        self.assertIn("7.1", w["text"])
        self.assertEqual(len(w["items"]), 1)
        self.assertIn("uhq", w["items"][0]["name"])

    def test_driver_rejection_blames_the_driver_not_ffmpeg(self):
        """#44's whole point: same symptom, opposite remedy. Never conflate them."""
        w = report(tune_cause=server._TUNE_DRIVER_REJECTED)["warning"]
        self.assertIsNotNone(w)
        self.assertIn("NVIDIA driver", w["text"])
        self.assertNotIn("Update ffmpeg", w["text"])

    def test_unrecognised_probe_failure_still_warns(self):
        w = report(tune_cause=server._TUNE_PROBE_FAILED)["warning"]
        self.assertIsNotNone(w)
        self.assertIn("do not recognise", w["text"])

    def test_missing_scale_cuda_is_named_with_its_remedy(self):
        w = report(scale_cuda=False)["warning"]
        self.assertIsNotNone(w)
        self.assertEqual(len(w["items"]), 1)
        self.assertIn("scale_cuda", w["items"][0]["name"])
        self.assertIn("libnpp", w["items"][0]["remedy"])

    def test_missing_libplacebo_is_named_with_its_remedy(self):
        w = report(libplacebo=False)["warning"]
        self.assertIsNotNone(w)
        self.assertIn("libplacebo", w["items"][0]["name"])
        self.assertIn("--enable-libplacebo", w["items"][0]["remedy"])

    def test_several_failures_are_all_listed_and_counted(self):
        w = report(tune_cause=server._TUNE_FFMPEG_TOO_OLD,
                   scale_cuda=False, libplacebo=False)["warning"]
        self.assertEqual(len(w["items"]), 3)
        self.assertIn("ffmpeg is missing 3 features", w["headline"])
        self.assertIn("slower fallbacks", w["headline"])

    def test_single_failure_headline_is_singular(self):
        w = report(scale_cuda=False)["warning"]
        self.assertIn("ffmpeg is missing 1 feature", w["headline"])
        self.assertIn("a slower fallback", w["headline"])

    def test_warning_says_gpu_encoding_still_works(self):
        """All three capabilities are extras on top of NVENC, which the ffmpeg
        preflight already required. Without this line the banner reads as "the
        GPU is not encoding" -- the misreading it actually caused."""
        w = report(scale_cuda=False)["warning"]
        self.assertIn("GPU encoding itself is working", w["reassurance"])

    def test_unknown_path_is_labelled_not_blank(self):
        w = report(path="", scale_cuda=False)["warning"]
        self.assertEqual(w["ffmpeg_path"], server.FFMPEG_PATH_UNKNOWN)
        self.assertIn(server.FFMPEG_PATH_UNKNOWN, w["text"])


class TestNoWarningWhenHealthy(unittest.TestCase):
    """The false-positive side. This is the failure mode the design note exists
    to prevent: a version comparison sneaking in as the real gate."""

    def test_all_probes_passing_produces_no_warning(self):
        r = report()
        self.assertIsNone(r["warning"])
        self.assertTrue(all(c["ok"] for c in r["capabilities"]))

    def test_ancient_version_string_with_passing_probes_still_no_warning(self):
        """A distro/vendor/git build can report anything. Behaviour is the gate."""
        for v in ("6.1.1", "2019-01-01-git-deadbeef", "n4.2.7", "0", ""):
            with self.subTest(version=v):
                self.assertIsNone(report(version=v)["warning"])

    def test_unknown_version_with_passing_probes_still_no_warning(self):
        r = report(version="")
        self.assertIsNone(r["warning"])
        self.assertEqual(r["ffmpeg_version"], server.FFMPEG_VERSION_UNKNOWN)

    def test_report_always_lists_all_three_capabilities(self):
        keys = [c["key"] for c in report()["capabilities"]]
        self.assertEqual(sorted(keys), ["libplacebo", "nvenc_uhq_tune", "scale_cuda"])


class TestVersionFailureDoesNotSuppressWarning(unittest.TestCase):
    """Acceptance criterion: an unreadable version degrades to "unknown version"
    and must NOT swallow a warning the probes earned."""

    def test_empty_version_still_warns_and_says_unknown(self):
        w = report(tune_cause=server._TUNE_FFMPEG_TOO_OLD, version="")["warning"]
        self.assertIsNotNone(w)
        self.assertEqual(w["ffmpeg_version"], server.FFMPEG_VERSION_UNKNOWN)
        self.assertIn(server.FFMPEG_VERSION_UNKNOWN, w["text"])
        self.assertIn(WINDOWS_PATH, w["text"])
        self.assertIn("Update ffmpeg", w["text"])

    def test_refresh_survives_a_version_read_that_raises_and_still_warns(self):
        with mock.patch.object(server, "get_ffmpeg", return_value=WINDOWS_PATH), \
             mock.patch.object(server, "_read_ffmpeg_version", side_effect=RuntimeError("boom")), \
             mock.patch.object(server, "_nvenc_tune_cause", server._TUNE_FFMPEG_TOO_OLD), \
             mock.patch.object(server, "logger"):
            r = server._refresh_capability_report()
        self.assertEqual(r["ffmpeg_version"], server.FFMPEG_VERSION_UNKNOWN)
        self.assertIsNotNone(r["warning"])
        self.assertIn(WINDOWS_PATH, r["warning"]["text"])


class TestRefreshIsCrashProof(unittest.TestCase):
    """Danger zone: this runs in `lifespan`. Nothing here may raise."""

    def setUp(self):
        self._saved = server._capability_report

    def tearDown(self):
        server._capability_report = self._saved

    def test_missing_ffmpeg_binary_yields_a_report_not_an_exception(self):
        with mock.patch.object(server, "get_ffmpeg", side_effect=RuntimeError("not found")), \
             mock.patch.object(server, "_read_ffmpeg_version", return_value=""), \
             mock.patch.object(server, "_nvenc_tune_cause", server._TUNE_PROBE_FAILED), \
             mock.patch.object(server, "logger"):
            r = server._refresh_capability_report()
        self.assertEqual(r["ffmpeg_path"], server.FFMPEG_PATH_UNKNOWN)
        self.assertIsNotNone(r["warning"])   # probe failure still surfaces

    def test_a_broken_builder_degrades_to_an_error_report(self):
        with mock.patch.object(server, "build_capability_report",
                               side_effect=ValueError("kaboom")), \
             mock.patch.object(server, "logger"):
            r = server._refresh_capability_report()
        self.assertEqual(r["error"], "capability report unavailable")
        self.assertIsNone(r["warning"])

    def test_lifespan_starts_even_when_the_report_blows_up(self):
        """The end of the danger zone: a diagnostic cannot take the app down."""
        async def drive():
            with mock.patch.object(server, "_probe_nvenc_tune", new=mock.AsyncMock()), \
                 mock.patch.object(server, "_refresh_capability_report",
                                   side_effect=RuntimeError("total failure")), \
                 mock.patch.object(server, "_heartbeat_watchdog", new=mock.AsyncMock()), \
                 mock.patch.object(server, "logger"):
                async with server.lifespan(server.app):
                    return True

        self.assertTrue(asyncio.run(drive()))

    def test_lifespan_records_the_report_on_the_happy_path(self):
        server._capability_report = {}

        async def drive():
            with mock.patch.object(server, "_probe_nvenc_tune", new=mock.AsyncMock()), \
                 mock.patch.object(server, "get_ffmpeg", return_value=WINDOWS_PATH), \
                 mock.patch.object(server, "_read_ffmpeg_version", return_value="7.1.1"), \
                 mock.patch.object(server, "_nvenc_tune_cause", server._TUNE_SUPPORTED), \
                 mock.patch.object(server, "HAS_SCALE_CUDA", True), \
                 mock.patch.object(server, "HAS_LIBPLACEBO", True), \
                 mock.patch.object(server, "_heartbeat_watchdog", new=mock.AsyncMock()), \
                 mock.patch.object(server, "logger"):
                async with server.lifespan(server.app):
                    return server._capability_report

        r = asyncio.run(drive())
        self.assertEqual(r["ffmpeg_path"], WINDOWS_PATH)
        self.assertEqual(r["ffmpeg_version"], "7.1.1")
        self.assertIsNone(r["warning"])


class TestCapabilitiesEndpoint(unittest.TestCase):
    """GET /capabilities serves the cached report; it never re-probes."""

    def setUp(self):
        self._saved = server._capability_report

    def tearDown(self):
        server._capability_report = self._saved

    def test_serves_the_cached_report_without_rebuilding(self):
        server._capability_report = {"ffmpeg_path": WINDOWS_PATH, "warning": None}
        with mock.patch.object(server, "_refresh_capability_report") as refresh:
            got = asyncio.run(server.get_capabilities())
        refresh.assert_not_called()
        self.assertEqual(got["ffmpeg_path"], WINDOWS_PATH)

    def test_builds_lazily_when_the_cache_is_empty(self):
        """Only reachable when the app was started without its lifespan."""
        server._capability_report = {}

        def fill():
            server._capability_report = {"ffmpeg_path": "late", "warning": None}
            return server._capability_report

        with mock.patch.object(server, "_refresh_capability_report", side_effect=fill) as refresh:
            got = asyncio.run(server.get_capabilities())
        refresh.assert_called_once()
        self.assertEqual(got["ffmpeg_path"], "late")


if __name__ == "__main__":
    unittest.main()
