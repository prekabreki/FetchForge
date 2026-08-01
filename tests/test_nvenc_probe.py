"""Issue #44 — the -tune uhq startup probe must diagnose *why* uhq is unavailable.

Two unrelated stderr signatures used to be lumped together and always blamed the GPU
driver. These tests pin the classification (pure, no ffmpeg / no GPU needed) and the
resulting module state, including the danger-zone case: an unrecognised failure must
degrade to "hq" and must never leave _nvenc_tune == "uhq".

Run: .venv/bin/python -m unittest tests.test_nvenc_probe -v
"""
import asyncio
import unittest
from unittest import mock

from fetchforge import server


# Real stderr captured on Windows 2026-08-01, RTX 4080 / driver 610.62, against
# ffmpeg 2023-10-29-git-2532e832d2-full_build-www.gyan.dev (predates ffmpeg 7.1).
FFMPEG_TOO_OLD_STDERR = """\
Stream mapping:
  Stream #0:0 -> #0:0 (wrapped_avframe (native) -> hevc (hevc_nvenc))
Press [q] to stop, [?] for help
[hevc_nvenc @ 000002942ee3bfc0] [Eval @ 000000ed123fed70] Undefined constant or missing '(' in 'uhq'
[hevc_nvenc @ 000002942ee3bfc0] Unable to parse option value "uhq"
[hevc_nvenc @ 000002942ee3bfc0] Error setting option tune to value uhq.
[vost#0:0/hevc_nvenc @ 000002942ee3bd00] Error while opening encoder - maybe incorrect parameters such as bit_rate, rate, width or height.
Error while filtering: Invalid argument
[out#0/null @ 000002942ee3b480] Nothing was written into output file, because at least one of its streams received no packets.
frame=    0 fps=0.0 q=0.0 Lsize=       0kB time=N/A bitrate=N/A speed=N/A
Conversion failed!
"""

# ffmpeg understands -tune uhq; the driver/GPU refuses the configuration.
DRIVER_REJECTED_STDERR = """\
Stream mapping:
  Stream #0:0 -> #0:0 (wrapped_avframe (native) -> hevc (hevc_nvenc))
[hevc_nvenc @ 000001d4a0d0b400] InitializeEncoder failed: invalid param (8)
[vost#0:0/hevc_nvenc @ 000001d4a0d0b180] Error while opening encoder - maybe incorrect parameters such as bit_rate, rate, width or height.
Conversion failed!
"""

# A failure with none of the known markers — e.g. the input/filter blew up before the
# encoder was ever reached. Unknown remedy, but still a failure.
UNRECOGNISED_FAILURE_STDERR = """\
[lavfi @ 0000021f0c0b1a80] No such filter: 'nullsrc'
Error opening input file lavfi.
Error opening input files: Invalid argument
Conversion failed!
"""

SUCCESS_STDERR = """\
Stream mapping:
  Stream #0:0 -> #0:0 (wrapped_avframe (native) -> hevc (hevc_nvenc))
Press [q] to stop, [?] for help
Output #0, null, to 'pipe:':
  Stream #0:0: Video: hevc, yuv420p(tv, progressive), 1920x1080, q=2-31, 2000 kb/s, 25 fps
frame=    3 fps=0.0 q=25.0 Lsize=       0kB time=00:00:00.08 bitrate=N/A speed=1.72x
"""


class _FakeProc:
    """Stand-in for asyncio.subprocess.Process with only what the probe uses."""

    def __init__(self, stderr: str, returncode: int):
        self._stderr = stderr.encode()
        self.returncode = returncode

    async def communicate(self):
        return b"", self._stderr


class TestClassifyTuneProbe(unittest.TestCase):
    """The pure classifier — the whole point of the extraction."""

    def test_old_ffmpeg_stderr_blames_the_ffmpeg_build(self):
        self.assertEqual(server._classify_tune_probe(FFMPEG_TOO_OLD_STDERR),
                         server._TUNE_FFMPEG_TOO_OLD)

    def test_driver_rejection_stderr_blames_the_driver(self):
        self.assertEqual(server._classify_tune_probe(DRIVER_REJECTED_STDERR),
                         server._TUNE_DRIVER_REJECTED)

    def test_unrecognised_failure_is_not_reported_as_supported(self):
        self.assertEqual(server._classify_tune_probe(UNRECOGNISED_FAILURE_STDERR),
                         server._TUNE_PROBE_FAILED)

    def test_clean_run_is_supported(self):
        self.assertEqual(server._classify_tune_probe(SUCCESS_STDERR),
                         server._TUNE_SUPPORTED)

    def test_error_setting_option_tune_alone_is_enough(self):
        """Some builds emit the "Error setting option" line without the parse line."""
        self.assertEqual(
            server._classify_tune_probe("[hevc_nvenc @ x] Error setting option tune to value uhq."),
            server._TUNE_FFMPEG_TOO_OLD)

    def test_invalid_param_alone_is_a_driver_rejection(self):
        self.assertEqual(server._classify_tune_probe("[hevc_nvenc @ x] invalid param (8)"),
                         server._TUNE_DRIVER_REJECTED)

    def test_parse_failure_wins_over_driver_markers(self):
        """ffmpeg rejecting the option name happens first — the driver never saw it,
        so a stderr carrying both signatures is an ffmpeg-too-old diagnosis."""
        both = FFMPEG_TOO_OLD_STDERR + "\n[hevc_nvenc @ x] InitializeEncoder failed: invalid param (8)"
        self.assertEqual(server._classify_tune_probe(both), server._TUNE_FFMPEG_TOO_OLD)

    def test_empty_stderr_is_supported(self):
        self.assertEqual(server._classify_tune_probe(""), server._TUNE_SUPPORTED)


class TestProbeNvencTune(unittest.TestCase):
    """End-to-end probe with a faked ffmpeg: module state + the logged remedy."""

    def setUp(self):
        self._saved_tune = server._nvenc_tune
        self._saved_cause = server._nvenc_tune_cause

    def tearDown(self):
        server._nvenc_tune = self._saved_tune
        server._nvenc_tune_cause = self._saved_cause

    def _run_probe(self, stderr: str, returncode: int):
        """Run _probe_nvenc_tune against a fake ffmpeg; return the captured log text."""
        async def fake_exec(*args, **kwargs):
            return _FakeProc(stderr, returncode)

        # Poison the globals first so we can prove the probe assigns them.
        server._nvenc_tune = "poisoned"
        server._nvenc_tune_cause = "poisoned"
        with mock.patch.object(server, "get_ffmpeg", return_value="ffmpeg"), \
             mock.patch("asyncio.create_subprocess_exec", side_effect=fake_exec), \
             mock.patch.object(server, "logger") as log:
            asyncio.run(server._probe_nvenc_tune())
        return " ".join(str(c.args[0]) for c in log.info.call_args_list)

    def test_old_ffmpeg_falls_back_to_hq_and_names_ffmpeg_71(self):
        msg = self._run_probe(FFMPEG_TOO_OLD_STDERR, 1)
        self.assertEqual(server._nvenc_tune, "hq")
        self.assertEqual(server._nvenc_tune_cause, server._TUNE_FFMPEG_TOO_OLD)
        self.assertIn("ffmpeg 7.1", msg)
        self.assertNotIn("not supported on this driver", msg)

    def test_driver_rejection_keeps_the_driver_wording(self):
        msg = self._run_probe(DRIVER_REJECTED_STDERR, 1)
        self.assertEqual(server._nvenc_tune, "hq")
        self.assertEqual(server._nvenc_tune_cause, server._TUNE_DRIVER_REJECTED)
        self.assertIn("not supported on this driver", msg)
        self.assertNotIn("ffmpeg 7.1", msg)

    def test_unrecognised_failure_degrades_to_hq(self):
        """Danger zone: an unknown failure must never leave the tune at uhq."""
        msg = self._run_probe(UNRECOGNISED_FAILURE_STDERR, 1)
        self.assertEqual(server._nvenc_tune, "hq")
        self.assertNotEqual(server._nvenc_tune, "uhq")
        self.assertEqual(server._nvenc_tune_cause, server._TUNE_PROBE_FAILED)
        self.assertIn("unrecognised", msg)

    def test_silent_nonzero_exit_still_degrades_to_hq(self):
        """No known marker anywhere in stderr, but ffmpeg exited non-zero — the
        returncode backstop must stop this being read as a working uhq."""
        self._run_probe("something went sideways\n", 1)
        self.assertEqual(server._nvenc_tune, "hq")
        self.assertEqual(server._nvenc_tune_cause, server._TUNE_PROBE_FAILED)

    def test_success_path_still_sets_uhq(self):
        msg = self._run_probe(SUCCESS_STDERR, 0)
        self.assertEqual(server._nvenc_tune, "uhq")
        self.assertEqual(server._nvenc_tune_cause, server._TUNE_SUPPORTED)
        self.assertIn("'uhq' supported", msg)


if __name__ == "__main__":
    unittest.main()
