"""Time-range capture (#76): pure helpers, argv wiring, and one real /download
stream that cuts an audio section end-to-end with CPU ffmpeg."""
import asyncio
import inspect
import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from fetchforge import server

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


class ParseSectionTest(unittest.TestCase):
    def test_blank_and_whole_video_mean_no_range(self):
        self.assertIsNone(server.parse_section("", ""))
        self.assertIsNone(server.parse_section("0", ""))
        self.assertIsNone(server.parse_section(" ", " "))

    def test_parses_whole_seconds(self):
        self.assertEqual(server.parse_section("750", "1080"), (750, 1080))
        self.assertEqual(server.parse_section("", "90"), (0, 90))
        self.assertEqual(server.parse_section("90", ""), (90, None))

    def test_rejects_bad_input(self):
        for start, end in [("10", "10"), ("20", "10"), ("-5", ""), ("1.5", ""),
                           ("12:30", ""), ("", "--exec=rm"), ("nan", ""), ("10000000", "")]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                server.parse_section(start, end)


class SectionNamingTest(unittest.TestCase):
    def test_suffix(self):
        self.assertEqual(server.section_suffix(None), "")
        self.assertEqual(server.section_suffix((750, 1080)), "_12m30s-18m00s")
        self.assertEqual(server.section_suffix((0, 65)), "_0m00s-1m05s")
        self.assertEqual(server.section_suffix((3720, None)), "_1h02m00s-end")

    def test_output_stem_unchanged_without_range(self):
        self.assertEqual(server._output_stem("Some_Video"), server.sanitize("Some_Video"))
        self.assertEqual(server._predict_output_stem("Some Video"),
                         server.sanitize(server._restrict_filename("Some Video")))

    def test_prediction_matches_the_downloaded_name(self):
        # yt-dlp writes <restricted title><suffix>.mkv; the encoder derives the
        # output from that stem, and the skip check must predict the same name.
        suffix = server.section_suffix((750, 1080))
        for title in ["Plain title", "Trailing_ ", "Ünïcödé — émoji 🎬", "x" * 400]:
            with self.subTest(title=title[:20]):
                downloaded = server._restrict_filename(title) + suffix
                self.assertEqual(server._output_stem(downloaded, suffix),
                                 server._predict_output_stem(title, suffix))

    def test_long_title_keeps_its_range_suffix(self):
        suffix = server.section_suffix((750, 1080))
        stem = server._predict_output_stem("x" * 400, suffix)
        self.assertTrue(stem.endswith(suffix))
        self.assertLessEqual(len(stem), 200)
        self.assertNotEqual(stem, server._predict_output_stem("x" * 400))


class SectionArgvTest(unittest.TestCase):
    def test_ytdlp_args(self):
        self.assertEqual(server.section_ytdlp_args(None), [])
        self.assertEqual(server.section_ytdlp_args((65, 95)),
                         ["--download-sections", "*65-95", "--downloader-args", "ffmpeg_o:-copyts"])
        self.assertEqual(server.section_ytdlp_args((65, None))[1], "*65-inf")

    def test_input_seek_is_relative_to_the_file_start(self):
        self.assertEqual(server.section_input_seek(None, 60.0), [])
        self.assertEqual(server.section_input_seek((65, 95), 60.021), ["-ss", "4.979", "-t", "30"])
        self.assertEqual(server.section_input_seek((65, None), 60.0), ["-ss", "5.000"])
        # A file that (unexpectedly) starts after the requested start never seeks negative.
        self.assertEqual(server.section_input_seek((65, 95), 70.0)[1], "0.000")

    def test_clip_duration(self):
        self.assertEqual(server._clip_duration((65, 95), 600), 30.0)
        self.assertEqual(server._clip_duration((65, None), 100), 35.0)
        self.assertIsNone(server._clip_duration((65, None), 0))   # open-ended, length unknown

    def _video_args(self, **kw):
        params = {"preset": "p4", "profile": "main", "cq": 26, "maxrate": "7M", "bufsize": "14M",
                  "pix_fmt": "yuv420p", "bf": 3, "b_ref_mode": "middle"}
        return server.build_video_ffmpeg_args(Path("in.mkv"), Path("out.mp4"), params, "hq", "vp9", **kw)

    def test_video_builder_puts_seek_before_input(self):
        plain = self._video_args()
        cut = self._video_args(input_opts=["-ss", "4.979", "-t", "30"])
        i = cut.index("-i")
        self.assertEqual(cut[i - 4:i], ["-ss", "4.979", "-t", "30"])
        self.assertEqual([a for a in cut if a not in ("-ss", "4.979", "-t", "30")], plain)

    def test_audio_builder_puts_seek_before_input(self):
        plain = server.build_audio_ffmpeg(Path("in.webm"), Path("out.mp3"), "mp3")
        cut = server.build_audio_ffmpeg(Path("in.webm"), Path("out.mp3"), "mp3", input_opts=["-ss", "2.000"])
        i = cut.index("-i")
        self.assertEqual(cut[i - 2:i], ["-ss", "2.000"])
        self.assertEqual(len(cut), len(plain) + 2)


class ClipDurationTest(unittest.TestCase):
    def test_resolve_section_closed_range(self):
        self.assertEqual(server._resolve_section((65, 95), 600.0), 95)

    def test_resolve_section_open_ended_uses_full_duration(self):
        self.assertEqual(server._resolve_section((65, None), 600.0), 600.0)

    def test_resolve_section_open_ended_unknown_zero_duration(self):
        self.assertIsNone(server._resolve_section((65, None), 0))

    def test_resolve_section_open_ended_unknown_none_duration(self):
        self.assertIsNone(server._resolve_section((65, None), None))

    def test_clip_duration_closed_range(self):
        result = server._clip_duration((65, 95), 600.0)
        self.assertEqual(result, 30.0)
        self.assertIsInstance(result, float)

    def test_clip_duration_open_ended_uses_full_duration(self):
        self.assertEqual(server._clip_duration((65, None), 600.0), 535.0)

    def test_clip_duration_open_ended_unknown_duration(self):
        self.assertIsNone(server._clip_duration((65, None), 0))

    def test_clip_duration_zero_length_range(self):
        self.assertIsNone(server._clip_duration((95, 95), 600.0))

    def test_clip_duration_end_before_start(self):
        self.assertIsNone(server._clip_duration((100, 95), 600.0))


class BatchSectionTest(unittest.TestCase):
    def _items(self, **extra):
        return json.dumps([{"url": "https://youtu.be/AAA", "video_format": "137", "audio_format": "140",
                            "title": "First", "duration": 600, **extra}])

    def test_batch_item_carries_its_section(self):
        *_, per_item = server._resolve_batch_items(self._items(section_start=65, section_end=95))
        self.assertEqual(per_item[0]["section"], (65, 95))

    def test_batch_item_without_range(self):
        for extra in ({}, {"section_start": None, "section_end": None}):
            *_, per_item = server._resolve_batch_items(self._items(**extra))
            self.assertIsNone(per_item[0]["section"])

    def test_batch_rejects_a_bad_range(self):
        with self.assertRaises(ValueError):
            server._resolve_batch_items(self._items(section_start=95, section_end=65))


def _make_media(path: Path, *, seconds: int, offset: int):
    """A real opus clip whose own timeline starts at *offset*, the way a
    yt-dlp `--download-sections` + `-copyts` download does."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=d={}".format(seconds),
                    "-output_ts_offset", str(offset), "-c:a", "libopus", str(path)], check=True)


def _probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=start_time,duration",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    return {k: float(v) for k, v in json.loads(out)["format"].items()}


@unittest.skipUnless(HAS_FFMPEG, "needs ffmpeg/ffprobe on PATH")
class SectionCompletenessTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    async def test_covering_file_is_complete(self):
        f = self.tmp / "c.webm"
        _make_media(f, seconds=35, offset=60)          # covers 60..95
        self.assertTrue(await server._section_download_is_complete(f, (65, 95), 600))

    async def test_truncated_file_is_incomplete(self):
        f = self.tmp / "t.webm"
        _make_media(f, seconds=15, offset=60)          # stops at 75, range wants 95
        self.assertFalse(await server._section_download_is_complete(f, (65, 95), 600))

    async def test_span_comes_from_packets_not_format_duration(self):
        f = self.tmp / "s.webm"
        _make_media(f, seconds=12, offset=8)
        self.assertAlmostEqual(_probe(f)["duration"], 20.0, delta=0.1)   # the trap: end time, not length
        t0, t1 = await server._media_time_span(f)
        self.assertAlmostEqual(t0, 8.0, delta=0.1)
        self.assertAlmostEqual(t1, 20.0, delta=0.1)

    async def test_open_ended_range_needs_the_video_end(self):
        f = self.tmp / "o.webm"
        _make_media(f, seconds=35, offset=60)          # ends at 95
        self.assertTrue(await server._section_download_is_complete(f, (65, None), 95))
        self.assertFalse(await server._section_download_is_complete(f, (65, None), 600))
        self.assertFalse(await server._section_download_is_complete(f, (65, None), 0))


# A stand-in for yt-dlp: answers the -J resolve, and for a download records its
# argv and writes a real offset clip where the -o template says, printing the
# same "[download] Destination:" line a section download prints.
_FAKE_YTDLP = textwrap.dedent('''
    import json, subprocess, sys
    from pathlib import Path
    argv = sys.argv[1:]
    Path(sys.argv[0] + ".calls").open("a").write(json.dumps(argv) + "\\n")
    if "-J" in argv:
        print(json.dumps({"title": "Clip Test", "duration": 60}))
        sys.exit(0)
    paths = Path(argv[argv.index("--paths") + 1])
    name = argv[argv.index("-o") + 1].replace("%(title)s", "Clip_Test").replace("%(ext)s", "webm")
    out = paths / name
    start, end = argv[argv.index("--download-sections") + 1][1:].split("-")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=d=12",
                    "-output_ts_offset", "8", "-c:a", "libopus", str(out)], check=True)
    print("[download] Destination: " + str(out), flush=True)
''')


@unittest.skipUnless(HAS_FFMPEG, "needs ffmpeg/ffprobe on PATH")
class SectionDownloadStreamTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fake = self.tmp / "fake_ytdlp.py"
        self.fake.write_text(_FAKE_YTDLP)
        (self.tmp / "cache").mkdir()
        (self.tmp / "out").mkdir()
        (self.tmp / "logs").mkdir()
        for p in [
            mock.patch.object(server, "get_ytdlp_argv", lambda: [sys.executable, str(self.fake)]),
            mock.patch.object(server, "CACHE_DIR", self.tmp / "cache"),
            mock.patch.object(server, "LOGS_DIR", self.tmp / "logs"),
            mock.patch.object(server, "NODE_ARGS", []),
            mock.patch.object(server, "_keep_awake", lambda: None),
            mock.patch.object(server, "_allow_sleep", lambda: None),
        ]:
            p.start()
            self.addCleanup(p.stop)

    async def _run(self, **form):
        kw = {}
        for name, prm in inspect.signature(server.download).parameters.items():
            kw[name] = getattr(prm.default, "default", prm.default)
        kw.update(url="https://www.youtube.com/watch?v=abc", audio_format="251", mode="audio",
                  audio_preset="mp3", output_dir=str(self.tmp / "out"), **form)
        resp = await server.download(**kw)
        events = []
        async for chunk in resp.body_iterator:
            chunk = chunk.decode() if isinstance(chunk, bytes) else chunk
            if chunk.startswith("data: "):
                events.append(json.loads(chunk[6:]))
        return events

    def _calls(self):
        return [json.loads(l) for l in (self.tmp / "fake_ytdlp.py.calls").read_text().splitlines()]

    async def test_range_downloads_only_the_section_and_cuts_it_exactly(self):
        events = await self._run(section_start="10", section_end="15")
        errors = [e["msg"] for e in events if e["type"] == "error"]
        self.assertEqual(errors, [])
        self.assertEqual(events[-1]["type"], "done")

        dl = [c for c in self._calls() if "-J" not in c][0]
        self.assertIn("*10-15", dl)
        self.assertEqual(dl[dl.index("-o") + 1], "%(title)s_0m10s-0m15s.%(ext)s")
        self.assertEqual(dl[-2:], ["--", "https://www.youtube.com/watch?v=abc"])

        out = self.tmp / "out" / "Clip_Test_0m10s-0m15s.mp3"
        self.assertTrue(out.exists(), list((self.tmp / "out").iterdir()))
        # The clip file spans 8..20; the encode must keep exactly 10..15.
        self.assertAlmostEqual(_probe(out)["duration"], 5.0, delta=0.1)

    async def test_bad_range_is_rejected_before_anything_runs(self):
        events = await self._run(section_start="15", section_end="10")
        self.assertEqual(events[0]["type"], "error")
        self.assertIn("time range", events[0]["msg"].lower())
        self.assertFalse((self.tmp / "fake_ytdlp.py.calls").exists())


if __name__ == "__main__":
    unittest.main()
