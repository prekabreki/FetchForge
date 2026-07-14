"""history.json concurrency + atomicity (H-5 / #15). Points HISTORY_PATH at a
temp file so the real history is never touched."""
import asyncio
import tempfile
import unittest
from pathlib import Path

from fetchforge import server


class TestHistory(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig = server.HISTORY_PATH
        self.tmp = Path(tempfile.mkdtemp(prefix="dlpr_hist_"))
        server.HISTORY_PATH = self.tmp / "history.json"

    def tearDown(self):
        server.HISTORY_PATH = self._orig
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_concurrent_saves_all_survive(self):
        await asyncio.gather(*[
            server._save_history_entry({"url": f"u{i}", "title": str(i)})
            for i in range(20)
        ])
        h = await server._load_history()
        self.assertEqual(len({e["url"] for e in h}), 20)

    async def test_dedupe_by_url(self):
        await server._save_history_entry({"url": "u0", "title": "a"})
        await server._save_history_entry({"url": "u0", "title": "b"})
        h = await server._load_history()
        self.assertEqual(sum(1 for e in h if e["url"] == "u0"), 1)

    async def test_atomic_no_tmp_left(self):
        await server._save_history_entry({"url": "u", "title": "t"})
        self.assertFalse(Path(str(server.HISTORY_PATH) + ".tmp").exists())


if __name__ == "__main__":
    unittest.main()
