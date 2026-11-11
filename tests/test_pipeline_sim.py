"""Deadlock-freedom of the pipeline coordinator (#3/#8/#11/#93).

These tests drive the REAL `fetchforge.server.run_pipelined` with fake
fetch/process coroutines. The coordinator owns `file_q` (maxsize=1),
abort/enc_gone/cancel events, the None sentinels and the drain-without-delete,
so these scenarios exercise production code rather than a hand-written mirror.
A 5s wait_for turns any hang into a failure rather than a hung suite.
"""
import asyncio
import json
import unittest

from fetchforge import server


def _terminal(events):
    """Map the coordinator's terminal SSE event to a short label."""
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


async def _run(n_videos, *, enc_fail=frozenset(), enc_crash=frozenset(),
               cancel_after_enc=None, cancel_idle=False):
    cancel = asyncio.Event()
    events = []
    items = list(range(1, n_videos + 1))

    async def fetch(item, emit, stopping):
        await asyncio.sleep(0)  # fake download
        return None if cancel.is_set() else item

    async def process(item, emit):
        if cancel_after_enc == item:
            cancel.set()
        await asyncio.sleep(0)  # fake encode
        if item in enc_crash:
            raise RuntimeError("boom")
        if cancel.is_set():
            return True
        if item in enc_fail:
            await emit(("error", item))
            return False
        await emit(("ok", item))
        return True

    async def driver():
        async for msg in server.run_pipelined(items, fetch, process, cancel):
            events.append(msg)

    timer = None
    if cancel_idle:
        async def _cancel_later():
            await asyncio.sleep(0.02)
            cancel.set()
        timer = asyncio.create_task(_cancel_later())
    try:
        await asyncio.wait_for(driver(), timeout=5)  # a hang → TimeoutError → failure
    finally:
        if timer is not None:
            timer.cancel()

    return _terminal(events), events


def _tagged(events, tag):
    return [x[1] for x in events if isinstance(x, tuple) and x[0] == tag]


class TestPipelineCoordination(unittest.IsolatedAsyncioTestCase):
    async def test_normal_completion(self):
        t, e = await _run(3)
        self.assertEqual(t, "done")
        self.assertEqual(_tagged(e, "ok"), [1, 2, 3])

    async def test_single_encode_failure_continues(self):
        t, e = await _run(3, enc_fail={2})
        self.assertEqual(t, "done")
        self.assertIn(1, _tagged(e, "ok"))
        self.assertIn(3, _tagged(e, "ok"))
        self.assertEqual(_tagged(e, "error"), [2])

    async def test_consecutive_failures_abort(self):
        t, _ = await _run(5, enc_fail={1, 2, 3})
        self.assertEqual(t, "aborted")

    async def test_cancel_while_encoder_idle(self):
        t, _ = await _run(4, cancel_idle=True)
        self.assertEqual(t, "cancelled")

    async def test_cancel_mid_encode(self):
        t, _ = await _run(4, cancel_after_enc=2)
        self.assertEqual(t, "cancelled")

    async def test_worker_crash_terminates(self):
        t, _ = await _run(4, enc_crash={2})
        self.assertIn(t, ("done", "aborted"))  # must terminate, not hang


if __name__ == "__main__":
    unittest.main()
