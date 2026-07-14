"""Batch-pipeline engine (#26): the pure `_resolve_batch_items` helper and the
async pipeline simulation for the skip-and-continue / per-item-outcome semantics.

The resolve tests import `server` and exercise the pure helper directly. The
simulation is self-contained (does NOT import server) — it faithfully mirrors
the real dl_worker/enc_worker primitives (file_q maxsize=1, abort/enc_gone/cancel
Events, None sentinels, 2-None drain) plus the new per-item `item_done`/
`item_failed` events and skip-and-continue-on-download-failure behaviour, so a
change to that coordination can be regression-tested and a 5s wait_for turns any
hang into a failure rather than a hung suite. (CLAUDE.md mandated gate.)"""
import asyncio
import json
import unittest

from fetchforge import server


class BatchResolveTest(unittest.TestCase):
    def test_items_json_builds_parallel_structures(self):
        items = [
            {"url": "https://youtu.be/AAA", "video_format": "137", "audio_format": "140",
             "expected_size": 1000, "output_dir": "/out/a", "tune_mode": "uhq",
             "title": "First", "duration": 10.0},
            {"url": "https://youtu.be/BBB", "video_format": "248", "audio_format": "251",
             "expected_size": 2000, "output_dir": "/out/b", "tune_mode": "hq",
             "title": "Second", "duration": 20.0},
        ]
        urls, titles, durations, per_item = server._resolve_batch_items(json.dumps(items))
        self.assertEqual(urls, ["https://youtu.be/AAA", "https://youtu.be/BBB"])
        self.assertEqual(titles["https://youtu.be/BBB"], "Second")
        self.assertEqual(durations["https://youtu.be/AAA"], 10.0)
        self.assertEqual(per_item[1]["tune_mode"], "hq")
        self.assertEqual(per_item[0]["video_format"], "137")

    def test_rejects_non_http_url(self):
        items = [{"url": "file:///etc/passwd", "video_format": "1", "audio_format": "2",
                  "expected_size": 0, "output_dir": "", "tune_mode": "uhq",
                  "title": "x", "duration": 0}]
        with self.assertRaises(ValueError):
            server._resolve_batch_items(json.dumps(items))


# ── Pipeline simulation (skip-and-continue + per-item outcomes) ───────────────
# Mirrors tests/test_pipeline_sim.py's primitives, adding the batch semantics:
# a download failure emits item_failed and CONTINUES (does not set abort), while
# a successful encode emits item_done. Item 2's download fails here.
MAX_CONSEC = 3


async def _run_batch(n_videos, *, dl_fail=frozenset()):
    """batch=True model. dl_fail = set of 1-based indices whose download fails."""
    cancelled = asyncio.Event()
    file_q = asyncio.Queue(maxsize=1)
    msg_q = asyncio.Queue()
    abort = asyncio.Event()
    enc_gone = asyncio.Event()
    events = []

    def _stopping():
        return abort.is_set() or cancelled.is_set() or enc_gone.is_set()

    async def _put_file(item):
        while not _stopping():
            try:
                file_q.put_nowait(item)
                return True
            except asyncio.QueueFull:
                await asyncio.sleep(0)
        return False

    async def _send_sentinel():
        while not enc_gone.is_set():
            try:
                file_q.put_nowait(None)
                return
            except asyncio.QueueFull:
                await asyncio.sleep(0)

    async def dl_worker():
        try:
            for i in range(1, n_videos + 1):
                if _stopping():
                    break
                await asyncio.sleep(0)  # fake download
                if cancelled.is_set():
                    break
                if i in dl_fail:
                    # Batch: emit item_failed and CONTINUE — do NOT set abort.
                    await msg_q.put(("item_failed", i))
                    continue
                if not await _put_file(i):
                    break
        finally:
            await _send_sentinel()
            await msg_q.put(None)

    async def enc_worker():
        consec = 0
        try:
            while True:
                item = await file_q.get()
                if item is None:
                    break
                if cancelled.is_set():
                    break
                await asyncio.sleep(0)  # fake encode
                consec = 0
                await msg_q.put(("item_done", item))
        finally:
            enc_gone.set()
            while True:
                try:
                    file_q.get_nowait()
                except asyncio.QueueEmpty:
                    break
            await msg_q.put(None)

    dl = asyncio.create_task(dl_worker())
    en = asyncio.create_task(enc_worker())

    async def driver():
        done = 0
        while done < 2:
            m = await msg_q.get()
            if m is None:
                done += 1
                continue
            events.append(m)
        for r in await asyncio.gather(dl, en, return_exceptions=True):
            if isinstance(r, Exception):
                abort.set()
                events.append(("error", "worker crashed: %s" % r))

    await asyncio.wait_for(driver(), timeout=5)  # a hang → TimeoutError → failure

    if cancelled.is_set():
        terminal = "cancelled"
    elif abort.is_set():
        terminal = "aborted"
    else:
        terminal = "done"
    return terminal, events


class TestBatchPipelineSim(unittest.IsolatedAsyncioTestCase):
    async def test_download_failure_skips_and_continues(self):
        # 3-item batch; item 2's download fails.
        t, e = await _run_batch(3, dl_fail={2})
        self.assertEqual(t, "done")  # terminates, no deadlock, not aborted
        done = [x[1] for x in e if x[0] == "item_done"]
        failed = [x[1] for x in e if x[0] == "item_failed"]
        self.assertEqual(sorted(done), [1, 3])   # 1 and 3 reached the encoder
        self.assertEqual(failed, [2])            # exactly one item_failed(idx=2)

    async def test_all_succeed(self):
        t, e = await _run_batch(3)
        self.assertEqual(t, "done")
        self.assertEqual(sorted(x[1] for x in e if x[0] == "item_done"), [1, 2, 3])
        self.assertEqual([x for x in e if x[0] == "item_failed"], [])


if __name__ == "__main__":
    unittest.main()
