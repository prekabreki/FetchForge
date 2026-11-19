"""Sequential download fallback (#92): when yt-dlp prints no output line, only
a file that appeared during THIS download may be used — never an unrelated file
already sitting in the shared cache."""
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

from fetchforge import server, config, ytdlp, jobs

_HAS_FFMPEG = shutil.which("ffmpeg") is not None


# A stand-in for yt-dlp. It answers the -J resolve, then for a download reads a
# small config: optionally write a file into --paths (bare bytes, or a copy of a
# real media file), optionally print an error and exit 1. By default it prints
# no Destination/Merger line, which is exactly the case the sequential fallback
# has to handle; `dest` makes it print yt-dlp's Destination line as well.
_FAKE_YTDLP = textwrap.dedent('''
    import json, sys
    from pathlib import Path
    argv = sys.argv[1:]
    here = Path(sys.argv[0])
    Path(str(here) + ".calls").open("a").write(json.dumps(argv) + "\\n")
    cfg = json.loads(Path(str(here) + ".cfg").read_text())
    if "-J" in argv:
        print(json.dumps({"title": "New Title", "duration": 60}), flush=True)
        sys.exit(0)
    paths = Path(argv[argv.index("--paths") + 1])
    if cfg.get("write"):
        target = paths / cfg["write"]
        if cfg.get("copy_from"):
            target.write_bytes(Path(cfg["copy_from"]).read_bytes())
        else:
            target.write_bytes(b"x" * int(cfg.get("size", 4096)))
        if cfg.get("dest"):
            print("[download] Destination: " + str(target), flush=True)
    if cfg.get("error"):
        print(cfg["error"], flush=True)
        sys.exit(1)
    sys.exit(0)
''')


class CacheSnapshotPatternsTest(unittest.TestCase):
    def test_audio_patterns_cover_every_audio_mode_suffix(self):
        self.assertEqual({p[1:] for p in ytdlp._cache_snapshot_patterns(True)},
                         ytdlp._AUDIO_MODE_EXTS)

    def test_audio_mode_accepts_audio_only_and_combined_files(self):
        # `ba/b` falls back to a combined file when no audio-only format exists (#141).
        self.assertLessEqual(ytdlp._AUDIO_EXTS, ytdlp._AUDIO_MODE_EXTS)
        self.assertLessEqual({".mp4", ".mkv", ".webm"}, ytdlp._AUDIO_MODE_EXTS)

    def test_video_pattern_is_mkv_only(self):
        self.assertEqual(ytdlp._cache_snapshot_patterns(False), ["*.mkv"])


class AudioModeCacheLookupTest(unittest.TestCase):
    """The file lookup the audio path uses, against a real temporary cache
    directory and no network (#141)."""

    def setUp(self):
        self.cache = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.cache, True)
        self.patterns = ytdlp._cache_snapshot_patterns(True)

    def _snapshot(self):
        return {p for pat in self.patterns for p in self.cache.glob(pat)}

    def test_finds_a_combined_file(self):
        for ext in (".mp4", ".mkv", ".webm"):
            with self.subTest(ext=ext):
                pre = self._snapshot()
                new = self.cache / ("Combined_Fallback" + ext)
                new.write_bytes(b"x" * 4096)
                self.assertEqual(ytdlp._newest_new_file(self.cache, pre, self.patterns), new)
                new.unlink()

    def test_ignores_part_files_and_older_unrelated_videos(self):
        older_mp4 = self.cache / "Another_Jobs_Video.mp4"
        older_mkv = self.cache / "Another_Jobs_Video.mkv"
        older_mp4.write_bytes(b"o" * 4096)
        older_mkv.write_bytes(b"o" * 4096)
        pre = self._snapshot()
        self.assertEqual(pre, {older_mp4, older_mkv})
        for leftover in ("Half_Done.mp4.part", "Half_Done.f18.mp4.part",
                         "Half_Done.webm.part", "Half_Done.mp4.ytdl"):
            (self.cache / leftover).write_bytes(b"p" * 4096)
        self.assertIsNone(ytdlp._newest_new_file(self.cache, pre, self.patterns))
        # Once this job's real file lands, it is the one picked.
        new = self.cache / "This_Jobs_Download.mp4"
        new.write_bytes(b"n" * 4096)
        self.assertEqual(ytdlp._newest_new_file(self.cache, pre, self.patterns), new)


class SequentialDownloadFallbackTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fake = self.tmp / "fake_ytdlp.py"
        self.fake.write_text(_FAKE_YTDLP)
        self.cache = self.tmp / "cache"
        self.cache.mkdir()
        (self.tmp / "out").mkdir()
        (self.tmp / "logs").mkdir()

        self.older = self.cache / "Some_Older_Unrelated_Video.mkv"
        self.older.write_bytes(b"o" * (3 * 1024 * 1024))

        for p in [
            mock.patch.object(ytdlp, "get_ytdlp_argv", lambda: [sys.executable, str(self.fake)]),
            mock.patch.object(config, "CACHE_DIR", self.cache),
            mock.patch.object(config, "LOGS_DIR", self.tmp / "logs"),
            mock.patch.object(ytdlp, "NODE_ARGS", []),
            mock.patch.object(jobs, "_keep_awake", lambda: None),
            mock.patch.object(jobs, "_allow_sleep", lambda: None),
        ]:
            p.start()
            self.addCleanup(p.stop)

    def _configure(self, **cfg):
        Path(str(self.fake) + ".cfg").write_text(json.dumps(cfg))

    async def _run(self, **form):
        kw = {}
        for name, prm in inspect.signature(server.download).parameters.items():
            kw[name] = getattr(prm.default, "default", prm.default)
        kw.update(url="https://www.youtube.com/watch?v=abc", audio_format="140",
                  output_dir=str(self.tmp / "out"),
                  expected_size=str(self.older.stat().st_size))
        kw.update(form)
        resp = await server.download(**kw)
        events = []
        async for chunk in resp.body_iterator:
            chunk = chunk.decode() if isinstance(chunk, bytes) else chunk
            if chunk.startswith("data: "):
                events.append(json.loads(chunk[6:]))
        return events

    async def test_failed_download_with_unrelated_cache_file_errors_and_keeps_it(self):
        for convert in ("false", "true"):
            with self.subTest(convert=convert):
                self._configure(error="ERROR: [youtube] abc: HTTP Error 403: Forbidden")
                events = await self._run(convert=convert, delete_cache="true")
                types = [e["type"] for e in events]
                self.assertIn("error", types)
                self.assertNotIn("done", types)
                errors = [e["msg"] for e in events if e["type"] == "error"]
                self.assertTrue(any("Download failed" in m for m in errors), errors)
                # Danger zone: the unrelated file must survive delete_cache=true.
                self.assertTrue(self.older.exists())
                self.assertEqual(self.older.stat().st_size, 3 * 1024 * 1024)
                self.assertEqual(sorted(p.name for p in self.tmp.joinpath("out").iterdir()), [])

    async def test_success_without_parsed_output_line_finds_only_the_new_file(self):
        self._configure(write="Newly_Downloaded_Video.mkv")
        events = await self._run(convert="false")
        types = [e["type"] for e in events]
        self.assertNotIn("error", types)
        self.assertEqual(events[-1]["type"], "done")
        saved = [e["msg"] for e in events
                 if e["type"] == "log" and "Saved (no conversion)" in e["msg"]]
        self.assertTrue(saved, [e["msg"] for e in events if e["type"] == "log"])
        self.assertIn("Newly_Downloaded_Video.mkv", saved[-1])
        self.assertNotIn("Some_Older_Unrelated_Video.mkv", saved[-1])
        self.assertTrue((self.cache / "Newly_Downloaded_Video.mkv").exists())
        self.assertTrue(self.older.exists())


    async def test_audio_mode_nothing_downloaded_names_what_was_looked_for(self):
        # Only a leftover .part lands; the older unrelated .mkv must not be used.
        self._configure(write="Half_Done.mp4.part")
        events = await self._run(mode="audio", audio_preset="mp3")
        errors = [e["msg"] for e in events if e["type"] == "error"]
        self.assertTrue(errors, [e["type"] for e in events])
        self.assertNotIn(".mkv found", errors[-1])
        for ext in (".mp4", ".m4a", ".webm"):
            self.assertIn(ext, errors[-1])
        self.assertTrue(self.older.exists())
        self.assertEqual(sorted(p.name for p in self.tmp.joinpath("out").iterdir()), [])

    async def test_video_mode_still_rejects_a_combined_mp4(self):
        self._configure(write="Newly_Downloaded_Video.mp4", dest=True)
        events = await self._run(convert="false")
        errors = [e["msg"] for e in events if e["type"] == "error"]
        self.assertTrue(any("No .mkv found" in m for m in errors), errors)

    def _make_combined(self, ext):
        """A real 2 s video+audio file, like yt-dlp's `b` fallback produces."""
        codecs = (["-c:v", "libvpx", "-c:a", "libopus"] if ext == ".webm"
                  else ["-c:v", "mpeg4", "-c:a", "aac"])
        src = self.tmp / ("source" + ext)
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc=d=2:s=160x120",
             "-f", "lavfi", "-i", "sine=f=440:d=2", "-shortest", *codecs, str(src)],
            check=True)
        return src

    @unittest.skipUnless(_HAS_FFMPEG, "ffmpeg not on PATH")
    async def test_audio_mode_extracts_from_a_combined_file(self):
        # #141: no audio-only format, so `ba/b` downloads a combined file.
        for ext in (".mp4", ".mkv", ".webm"):
            src = self._make_combined(ext)
            for preset in ("wav", "mp3"):
                for dest in (True, False):
                    with self.subTest(ext=ext, preset=preset, dest=dest):
                        name = "Combined_{}_{}".format(ext[1:], int(dest))
                        self._configure(write=name + ext, copy_from=str(src), dest=dest)
                        events = await self._run(mode="audio", audio_preset=preset,
                                                 expected_size="0", delete_cache="true")
                        types = [e["type"] for e in events]
                        self.assertNotIn("error", types,
                                         [e["msg"] for e in events if e["type"] == "error"])
                        self.assertEqual(events[-1]["type"], "done")
                        out = self.tmp / "out" / "{}.{}".format(name, preset)
                        self.assertTrue(out.exists(), list(self.tmp.joinpath("out").iterdir()))
                        probe = subprocess.run(
                            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name",
                             "-of", "json", str(out)], capture_output=True, text=True, check=True)
                        streams = json.loads(probe.stdout)["streams"]
                        self.assertEqual([st["codec_type"] for st in streams], ["audio"])
                        self.assertEqual(streams[0]["codec_name"],
                                         "pcm_s16le" if preset == "wav" else "mp3")
                        # The older unrelated video is never touched.
                        self.assertTrue(self.older.exists())
                        self.assertFalse((self.cache / (name + ext)).exists())


if __name__ == "__main__":
    unittest.main()
