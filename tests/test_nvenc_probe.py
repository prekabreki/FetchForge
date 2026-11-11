"""Issue #44 — the -tune uhq startup probe must diagnose *why* uhq is unavailable.

Two unrelated stderr signatures used to be lumped together and always blamed the GPU
driver. These tests pin the classification (pure, no ffmpeg / no GPU needed) and the
resulting module state, including the danger-zone case: an unrecognised failure must
degrade to "hq" and must never leave _nvenc_tune == "uhq".

Run: .venv/bin/python -m unittest tests.test_nvenc_probe -v
"""
import asyncio
import importlib.util
import inspect
import json
import shutil
import tempfile
import unittest
from pathlib import Path
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
        self._saved_nvenc = server.HAS_NVENC

    def tearDown(self):
        server._nvenc_tune = self._saved_tune
        server._nvenc_tune_cause = self._saved_cause
        server.HAS_NVENC = self._saved_nvenc

    def _run_probe(self, stderr: str, returncode: int):
        """Run _probe_nvenc_tune against a fake ffmpeg; return the captured log text."""
        async def fake_exec(*args, **kwargs):
            return _FakeProc(stderr, returncode)

        # Poison the globals first so we can prove the probe assigns them.
        server._nvenc_tune = "poisoned"
        server._nvenc_tune_cause = "poisoned"
        with mock.patch.object(server, "HAS_NVENC", True), \
             mock.patch.object(server, "get_ffmpeg", return_value="ffmpeg"), \
             mock.patch("asyncio.create_subprocess_exec", side_effect=fake_exec), \
             mock.patch.object(server, "logger") as log:
            asyncio.run(server._probe_nvenc_tune())
        return " ".join(str(c.args[0]) for c in log.info.call_args_list)

    def test_no_nvenc_skips_the_probe_and_names_the_real_blocker(self):
        """No encoder means the uhq run cannot start; do not spawn a doomed
        ffmpeg or report an unrecognised probe failure."""
        async def boom(*args, **kwargs):
            raise AssertionError("probe must not spawn ffmpeg without NVENC")

        server._nvenc_tune = "poisoned"
        server._nvenc_tune_cause = "poisoned"
        with mock.patch.object(server, "HAS_NVENC", False), \
             mock.patch("asyncio.create_subprocess_exec", side_effect=boom), \
             mock.patch.object(server, "logger") as log:
            asyncio.run(server._probe_nvenc_tune())
        self.assertEqual(server._nvenc_tune, "hq")
        self.assertEqual(server._nvenc_tune_cause, server._TUNE_NVENC_MISSING)
        msg = " ".join(str(c.args[0]) for c in log.info.call_args_list)
        self.assertIn("not available", msg)
        self.assertIn("video conversion", msg)

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


class TestEncoderProbe(unittest.TestCase):
    """`hevc_nvenc` is read from `ffmpeg -encoders`, not guessed."""

    def _encoders(self, stdout):
        done = mock.Mock(stdout=stdout, stderr="")
        with mock.patch.object(server, "get_ffmpeg", return_value="ffmpeg"), \
             mock.patch("subprocess.run", return_value=done):
            return server._ffmpeg_has_encoder("hevc_nvenc")

    def test_lists_hevc_nvenc(self):
        self.assertTrue(self._encoders(
            " V....D hevc_nvenc            NVIDIA NVENC hevc encoder (codec hevc)\n"))

    def test_absent_encoder_is_false(self):
        self.assertFalse(self._encoders(
            " V....D libx264              H.264 / AVC / MPEG-4 AVC\n"))

    def test_probe_failure_is_false_not_an_exception(self):
        with mock.patch.object(server, "get_ffmpeg", return_value="ffmpeg"), \
             mock.patch("subprocess.run", side_effect=OSError("boom")):
            self.assertFalse(server._ffmpeg_has_encoder("hevc_nvenc"))


class TestStartupCapabilityProbe(unittest.IsolatedAsyncioTestCase):
    """The one probe `lifespan` runs records all four outcomes."""

    async def test_records_nvenc_filters_and_the_tune_outcome(self):
        saved = (server.HAS_NVENC, server.HAS_SCALE_CUDA, server.HAS_LIBPLACEBO)

        def restore():
            server.HAS_NVENC, server.HAS_SCALE_CUDA, server.HAS_LIBPLACEBO = saved

        self.addCleanup(restore)
        with mock.patch.object(server, "_ffmpeg_has_encoder", return_value=True) as enc, \
             mock.patch.object(server, "_ffmpeg_has_filter",
                               side_effect=lambda name: name == "scale_cuda") as filt, \
             mock.patch.object(server, "_probe_nvenc_tune", new=mock.AsyncMock()) as tune:
            await server._probe_ffmpeg_capabilities()
        enc.assert_called_once_with("hevc_nvenc")
        self.assertEqual([c.args[0] for c in filt.call_args_list],
                         ["scale_cuda", "libplacebo"])
        tune.assert_awaited_once()
        self.assertTrue(server.HAS_NVENC)
        self.assertTrue(server.HAS_SCALE_CUDA)
        self.assertFalse(server.HAS_LIBPLACEBO)

    async def test_missing_nvenc_still_records_the_other_probes(self):
        saved = (server.HAS_NVENC, server.HAS_SCALE_CUDA, server.HAS_LIBPLACEBO)

        def restore():
            server.HAS_NVENC, server.HAS_SCALE_CUDA, server.HAS_LIBPLACEBO = saved

        self.addCleanup(restore)
        with mock.patch.object(server, "_ffmpeg_has_encoder", return_value=False), \
             mock.patch.object(server, "_ffmpeg_has_filter", return_value=True), \
             mock.patch.object(server, "_probe_nvenc_tune", new=mock.AsyncMock()) as tune:
            await server._probe_ffmpeg_capabilities()
        tune.assert_awaited_once()
        self.assertFalse(server.HAS_NVENC)


class TestImportSpawnsNoSubprocess(unittest.TestCase):
    """Danger zone from #99: importing the module runs no probe."""

    def test_a_fresh_import_spawns_no_subprocess(self):
        import fetchforge

        src = Path(fetchforge.__file__).parent / "server.py"
        spec = importlib.util.spec_from_file_location(
            "fetchforge_server_fresh_import", src)
        module = importlib.util.module_from_spec(spec)
        with mock.patch("subprocess.run") as run, \
             mock.patch("asyncio.create_subprocess_exec") as exec_:
            spec.loader.exec_module(module)
        run.assert_not_called()
        exec_.assert_not_called()


class TestNoNvencRefusal(unittest.TestCase):
    """The pure gate: only video conversion needs NVENC."""

    def test_video_conversion_is_refused(self):
        with mock.patch.object(server, "HAS_NVENC", False):
            msg = server._nvenc_missing_error("video", True)
        self.assertIsNotNone(msg)
        self.assertIn("NVENC", msg)
        self.assertIn("Download-only", msg)
        self.assertIn("audio extraction still work", msg)

    def test_download_only_still_works(self):
        with mock.patch.object(server, "HAS_NVENC", False):
            self.assertIsNone(server._nvenc_missing_error("video", False))

    def test_audio_mode_still_works(self):
        with mock.patch.object(server, "HAS_NVENC", False):
            self.assertIsNone(server._nvenc_missing_error("audio", True))

    def test_video_conversion_allowed_when_nvenc_present(self):
        with mock.patch.object(server, "HAS_NVENC", True):
            self.assertIsNone(server._nvenc_missing_error("video", True))


async def _call(endpoint, **form):
    """Invoke a FastAPI endpoint with its Form defaults filled in."""
    kwargs = {name: form[name] if name in form else param.default.default
              for name, param in inspect.signature(endpoint).parameters.items()}
    return await endpoint(**kwargs)


async def _events(response):
    events = []
    async for chunk in response.body_iterator:
        chunk = chunk.decode() if isinstance(chunk, bytes) else chunk
        if chunk.startswith("data: "):
            events.append(json.loads(chunk[6:]))
    return events


class TestNoNvencRefusalEndpoints(unittest.IsolatedAsyncioTestCase):
    """Both video jobs stop with an `error` before any download is started."""

    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.logs = self.tmp / "logs"
        self.logs.mkdir()
        for patch in [
            mock.patch.object(server, "HAS_NVENC", False),
            mock.patch.object(server, "_try_admit", lambda: True),
            mock.patch.object(server, "_job_admitted", False),
            mock.patch.object(server, "_active_jobs", 0),
            mock.patch.object(server, "_keep_awake", lambda: None),
            mock.patch.object(server, "_allow_sleep", lambda: None),
            mock.patch.object(server, "_cancel_requested", asyncio.Event()),
            mock.patch.object(server, "LOGS_DIR", self.logs),
            mock.patch.object(server, "get_ytdlp_argv",
                              side_effect=AssertionError("yt-dlp must not run")),
            mock.patch.object(server, "current_process", None),
            mock.patch.object(server, "current_dl_process", None),
        ]:
            patch.start()
            self.addCleanup(patch.stop)
        self.addAsyncCleanup(asyncio.get_running_loop().shutdown_default_executor)

    async def test_download_video_is_refused_before_ytdlp(self):
        response = await _call(
            server.download,
            url="https://www.youtube.com/watch?v=abc",
            audio_format="251",
            convert="true",
            output_dir=str(self.tmp / "out"),
        )
        events = await _events(response)
        self.assertTrue(events, "no events were emitted")
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("NVENC", events[-1]["msg"])
        self.assertIn("Download-only", events[-1]["msg"])

    async def test_convert_local_video_is_refused_before_any_ffmpeg(self):
        response = await _call(
            server.convert_local,
            files=json.dumps([str(self.tmp / "source.mkv")]),
            output_dir=str(self.tmp / "out"),
        )
        events = await _events(response)
        self.assertTrue(events, "no events were emitted")
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("NVENC", events[-1]["msg"])


if __name__ == "__main__":
    unittest.main()
