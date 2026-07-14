"""Deadlock-freedom of the pipeline dl_worker/enc_worker coordination (#3/#8/#11).

Self-contained: it does NOT import server — it faithfully models the same
primitives (file_q maxsize=1, abort/enc_gone/cancel Events, _put_file,
_send_sentinel, worker loop shapes) so a change to that design can be
regression-tested here, and a 5s wait_for turns any hang into a failure rather
than a hung suite. This model caught a real deadlock during the audit-fix work."""
import asyncio
import unittest

MAX_CONSEC = 3


async def _run(n_videos, *, enc_fail=frozenset(), enc_crash=frozenset(),
               cancel_after_enc=None, cancel_idle=False):
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
                if cancel_after_enc == item:
                    cancelled.set()
                await asyncio.sleep(0)  # fake encode
                if item in enc_crash:
                    raise RuntimeError("boom")
                if cancelled.is_set():
                    break
                if item in enc_fail:
                    consec += 1
                    await msg_q.put(("error", item))
                    if consec >= MAX_CONSEC:
                        abort.set()
                        await msg_q.put(("error", "abort"))
                        break
                    continue
                consec = 0
                await msg_q.put(("ok", item))
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
        if cancel_idle:
            await asyncio.sleep(0.02)
            cancelled.set()
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

    await asyncio.wait_for(driver(), timeout=5)  # a hang → TimeoutError → test failure

    if cancelled.is_set():
        terminal = "cancelled"
    elif abort.is_set():
        terminal = "aborted"
    else:
        terminal = "done"
    return terminal, events


class TestPipelineCoordination(unittest.IsolatedAsyncioTestCase):
    async def test_normal_completion(self):
        t, e = await _run(3)
        self.assertEqual(t, "done")
        self.assertEqual([x for x in e if x[0] == "ok"], [("ok", 1), ("ok", 2), ("ok", 3)])

    async def test_single_encode_failure_continues(self):
        t, e = await _run(3, enc_fail={2})
        self.assertEqual(t, "done")
        self.assertIn(("ok", 1), e)
        self.assertIn(("ok", 3), e)
        self.assertIn(("error", 2), e)

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
