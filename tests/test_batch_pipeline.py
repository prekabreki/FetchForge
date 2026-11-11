"""Batch-pipeline engine (#26/#93): the pure `_resolve_batch_items` helper and
the async pipeline simulation for skip-and-continue / per-item-outcome semantics.

The resolve tests import `server` and exercise the pure helper directly. The
simulation drives the REAL `fetchforge.server.run_pipelined` coordinator with
fake fetch/process coroutines, so the skip-and-continue / item_done / item_failed
semantics are exercised against production code rather than a hand-written
mirror. A 5s wait_for turns any hang into a failure.
"""
import asyncio
import json
import unittest

from fetchforge import ytdlp, items as runner


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
        urls, titles, durations, per_item = ytdlp._resolve_batch_items(json.dumps(items))
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
            ytdlp._resolve_batch_items(json.dumps(items))


# ── Pipeline simulation (skip-and-continue + per-item outcomes) ───────────────
# Drives runner.run_pipelined with fake bodies: a download failure emits
# item_failed and CONTINUES (returns None, no abort), while a successful encode
# emits item_done. Item 2's download fails here.
def _terminal(events):
    for msg in events:
        if not isinstance(msg, str):
            continue
        try:
            evt = json.loads(msg[6:].strip())
        except (ValueError, TypeError):
            continue
        if evt.get("type") == "cancelled":
            return "cancelled"
        if evt.get("type") == "done":
            return "done"
    return "aborted"


async def _run_batch(n_videos, *, dl_fail=frozenset()):
    """batch=True model. dl_fail = set of 1-based indices whose download fails."""
    cancel = asyncio.Event()
    events = []
    items = list(range(1, n_videos + 1))

    async def fetch(item, emit, stopping):
        await asyncio.sleep(0)  # fake download
        if item in dl_fail:
            # Batch: emit item_failed and CONTINUE — no abort.
            await emit(("item_failed", item))
            return None
        return item

    async def process(item, emit):
        await asyncio.sleep(0)  # fake encode
        await emit(("item_done", item))
        return True

    async def driver():
        async for msg in runner.run_pipelined(items, fetch, process, cancel):
            events.append(msg)

    await asyncio.wait_for(driver(), timeout=5)  # a hang → TimeoutError → failure
    return _terminal(events), events


def _tagged(events, tag):
    return sorted(x[1] for x in events if isinstance(x, tuple) and x[0] == tag)


class TestBatchPipelineSim(unittest.IsolatedAsyncioTestCase):
    async def test_download_failure_skips_and_continues(self):
        # 3-item batch; item 2's download fails.
        t, e = await _run_batch(3, dl_fail={2})
        self.assertEqual(t, "done")  # terminates, no deadlock, not aborted
        self.assertEqual(_tagged(e, "item_done"), [1, 3])   # 1 and 3 reached the encoder
        self.assertEqual(_tagged(e, "item_failed"), [2])    # exactly one item_failed(idx=2)

    async def test_all_succeed(self):
        t, e = await _run_batch(3)
        self.assertEqual(t, "done")
        self.assertEqual(_tagged(e, "item_done"), [1, 2, 3])
        self.assertEqual(_tagged(e, "item_failed"), [])


if __name__ == "__main__":
    unittest.main()
