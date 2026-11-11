"""Sequential download fallback (#92): when yt-dlp prints no output line, only
a file that appeared during THIS download may be used — never an unrelated file
already sitting in the shared cache."""
import inspect
import json
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from fetchforge import server, config, ytdlp, jobs


# A stand-in for yt-dlp. It answers the -J resolve, then for a download reads a
# small config: optionally write a bare file into --paths, optionally print an
# error and exit 1. It deliberately prints no Destination/Merger line, which is
# exactly the case the sequential fallback has to handle.
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
        (paths / cfg["write"]).write_bytes(b"x" * int(cfg.get("size", 4096)))
    if cfg.get("error"):
        print(cfg["error"], flush=True)
        sys.exit(1)
    sys.exit(0)
''')


class CacheSnapshotPatternsTest(unittest.TestCase):
    def test_audio_patterns_cover_every_audio_suffix(self):
        self.assertEqual({p[1:] for p in ytdlp._cache_snapshot_patterns(True)},
                         ytdlp._AUDIO_EXTS)

    def test_video_pattern_is_mkv_only(self):
        self.assertEqual(ytdlp._cache_snapshot_patterns(False), ["*.mkv"])


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
                  expected_size=str(self.older.stat().st_size), **form)
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


if __name__ == "__main__":
    unittest.main()
