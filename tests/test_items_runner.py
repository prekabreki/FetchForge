"""Items-only /download runner (#103): one fetch_item + one process_item, and
per-item settings honoured in both sequential and pipelined modes."""
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


# A stand-in for yt-dlp driven by a per-test config. Non-`-J` invocations are
# served in order from cfg["downloads"]; each spec names the file it writes so
# the server's Destination/Merger parsing sees a realistic output path.
_FAKE_YTDLP = textwrap.dedent('''
    import json, subprocess, sys
    from pathlib import Path
    argv = sys.argv[1:]
    here = Path(sys.argv[0])
    Path(str(here) + ".calls").open("a").write(json.dumps(argv) + "\\n")
    cfg = json.loads(Path(str(here) + ".cfg").read_text())
    if "-J" in argv:
        print(json.dumps(cfg.get("resolve", {"title": "Batch", "duration": 600})), flush=True)
        sys.exit(0)
    counter = Path(str(here) + ".counter")
    n = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(n + 1))
    spec = cfg["downloads"][n]
    if spec.get("error"):
        print(spec["error"], flush=True)
        sys.exit(1)
    paths = Path(argv[argv.index("--paths") + 1])
    name = argv[argv.index("-o") + 1].replace("%(title)s", spec["title"]).replace("%(ext)s", spec.get("ext", "mkv"))
    out = paths / name
    if spec.get("media") == "audio":
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
                        "-i", "sine=d={}".format(spec.get("seconds", 30)),
                        "-output_ts_offset", str(spec.get("offset", 8)),
                        "-c:a", "libopus", str(out)], check=True)
        print("[download] Destination: " + str(out), flush=True)
    else:
        out.write_bytes(b"x" * int(spec.get("size", 4096)))
        print('[Merger] Merging formats into "' + str(out) + '"', flush=True)
    sys.exit(0)
''')


def _probe_duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    return float(json.loads(out)["format"]["duration"])


class ItemsRunnerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fake = self.tmp / "fake_ytdlp.py"
        self.fake.write_text(_FAKE_YTDLP)
        self.cache = self.tmp / "cache"
        self.cache.mkdir()
        self.logs = self.tmp / "logs"
        self.logs.mkdir()
        self.out_a = self.tmp / "out_a"
        self.out_b = self.tmp / "out_b"
        self.out_a.mkdir()
        self.out_b.mkdir()

        for p in [
            mock.patch.object(server, "get_ytdlp_argv", lambda: [sys.executable, str(self.fake)]),
            mock.patch.object(server, "CACHE_DIR", self.cache),
            mock.patch.object(server, "LOGS_DIR", self.logs),
            mock.patch.object(server, "NODE_ARGS", []),
            mock.patch.object(server, "cookie_args", lambda: []),
            mock.patch.object(server, "_keep_awake", lambda: None),
            mock.patch.object(server, "_allow_sleep", lambda: None),
        ]:
            p.start()
            self.addCleanup(p.stop)

    def _configure(self, downloads, resolve=None):
        cfg = {"downloads": downloads}
        if resolve is not None:
            cfg["resolve"] = resolve
        Path(str(self.fake) + ".cfg").write_text(json.dumps(cfg))

    async def _run(self, **form):
        kw = {}
        for name, prm in inspect.signature(server.download).parameters.items():
            kw[name] = getattr(prm.default, "default", prm.default)
        kw.update({"url": "https://www.youtube.com/watch?v=abc", "audio_format": "251"})
        kw.update(form)
        resp = await server.download(**kw)
        events = []
        async for chunk in resp.body_iterator:
            chunk = chunk.decode() if isinstance(chunk, bytes) else chunk
            if chunk.startswith("data: "):
                events.append(json.loads(chunk[6:]))
        return events

    def _calls(self):
        path = Path(str(self.fake) + ".calls")
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    async def test_two_item_audio_batch_honours_sections_and_output_dirs(self):
        if not HAS_FFMPEG:
            self.skipTest("needs ffmpeg/ffprobe")
        self._configure([
            {"title": "Clip_One", "ext": "webm", "media": "audio"},
            {"title": "Clip_Two", "ext": "webm", "media": "audio"},
        ])
        items = [
            {"url": "https://youtu.be/a", "title": "Clip One", "duration": 600,
             "audio_format": "251", "section_start": 10, "section_end": 13,
             "output_dir": str(self.out_a)},
            {"url": "https://youtu.be/b", "title": "Clip Two", "duration": 600,
             "audio_format": "251", "section_start": 20, "section_end": 22,
             "output_dir": str(self.out_b)},
        ]
        events = await self._run(items=json.dumps(items), mode="audio",
                                 audio_preset="wav")
        self.assertEqual([e["msg"] for e in events if e["type"] == "error"], [])
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(sorted(e["idx"] for e in events if e["type"] == "item_done"), [1, 2])

        first = self.out_a / "Clip_One_0m10s-0m13s.wav"
        second = self.out_b / "Clip_Two_0m20s-0m22s.wav"
        self.assertTrue(first.exists(), list(self.out_a.iterdir()))
        self.assertTrue(second.exists(), list(self.out_b.iterdir()))
        self.assertAlmostEqual(_probe_duration(first), 3.0, delta=0.2)
        self.assertAlmostEqual(_probe_duration(second), 2.0, delta=0.2)

        calls = self._calls()
        self.assertEqual(len(calls), 2)
        self.assertIn("*10-13", calls[0])
        self.assertIn("*20-22", calls[1])
        self.assertEqual(calls[0][-2:], ["--", "https://youtu.be/a"])
        self.assertEqual(calls[1][-2:], ["--", "https://youtu.be/b"])

    async def test_two_item_download_only_batch_uses_per_item_formats(self):
        self._configure([
            {"title": "Video_A"},
            {"title": "Video_B"},
        ])
        items = [
            {"url": "https://youtu.be/a", "title": "Video A", "duration": 10,
             "video_format": "137", "audio_format": "140", "output_dir": str(self.out_a)},
            {"url": "https://youtu.be/b", "title": "Video B", "duration": 10,
             "video_format": "248", "audio_format": "140", "output_dir": str(self.out_b)},
        ]
        events = await self._run(items=json.dumps(items), convert="false")
        self.assertEqual([e["msg"] for e in events if e["type"] == "error"], [])
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(sorted(e["idx"] for e in events if e["type"] == "item_done"), [1, 2])

        calls = self._calls()
        self.assertEqual([c[c.index("-f") + 1] for c in calls],
                         ["137+140/bv*+ba/b", "248+140/bv*+ba/b"])
        self.assertTrue((self.cache / "Video_A.mkv").exists())
        self.assertTrue((self.cache / "Video_B.mkv").exists())

    async def test_failed_item_does_not_stop_remaining_items(self):
        self._configure([
            {"title": "Nope", "error": "ERROR: [youtube] a: HTTP Error 403: Forbidden"},
            {"title": "Video_B"},
        ])
        items = [
            {"url": "https://youtu.be/a", "title": "Video A", "audio_format": "140"},
            {"url": "https://youtu.be/b", "title": "Video B", "audio_format": "140"},
        ]
        events = await self._run(items=json.dumps(items), convert="false")
        self.assertEqual([e["idx"] for e in events if e["type"] == "item_failed"], [1])
        self.assertEqual([e["idx"] for e in events if e["type"] == "item_done"], [2])
        self.assertEqual(events[-1]["type"], "done")
        self.assertTrue((self.cache / "Video_B.mkv").exists())

    async def test_playlist_url_is_rejected_without_expanding(self):
        self._configure([], resolve={"_type": "playlist", "entries": [
            {"webpage_url": "https://youtu.be/a", "title": "A", "duration": 10},
            {"webpage_url": "https://youtu.be/b", "title": "B", "duration": 10},
        ]})
        events = await self._run(url="https://www.youtube.com/playlist?list=PLx")
        errors = [e["msg"] for e in events if e["type"] == "error"]
        self.assertTrue(errors, events)
        self.assertIn("playlist", errors[0].lower())
        self.assertIn("items", errors[0].lower())
        self.assertEqual(len(self._calls()), 1)   # only the resolve probe ran
        self.assertIn("-J", self._calls()[0])

    async def test_non_http_item_rejected_before_any_subprocess(self):
        items = [{"url": "file:///etc/passwd", "title": "x", "audio_format": "140"}]
        events = await self._run(items=json.dumps(items))
        self.assertEqual(events[0]["type"], "error")
        self.assertIn("http", events[0]["msg"].lower())
        self.assertEqual(self._calls(), [])


if __name__ == "__main__":
    unittest.main()
