// Regression net for #95 (audit finding H4).
//
// The /download SSE reader never looked at r.ok. A 409 from the single-job
// admission is a JSON body with no `data:` lines, so the loop reached the end
// with gotError false and reported the item done although nothing ran. A fetch
// or mid-stream read rejection other than AbortError was rethrown, and
// startDownload had no try/finally, so the job flag, Cancel button and wake
// lock stayed stuck.
//
// Observed through the real DOM (failure summary, Start/Cancel buttons) and the
// real stream double — no page globals are touched.

import test from 'node:test';
import assert from 'node:assert/strict';
import { loadPage, queueVideos, flush } from './harness.mjs';

const BUSY = 'Another download or conversion is already running. Wait for it to finish and try again.';

const failureEntries = (page) => page.$$('#failure-panel-list .failure-item');

test('a /download 409 leaves the item recorded as failed, not done', async (t) => {
  const page = await loadPage({
    routes: (f) => {
      f.json('/download', { error: BUSY }, { ok: false, status: 409 });
      f.json('/cancel', { status: 'cancelled' });
    },
  });
  t.after(() => page.close());
  await queueVideos(page, ['https://youtu.be/busy']);

  const run = page.window.startDownload();
  let runError = null;
  run.catch((e) => { runError = e; });
  await flush(10);

  assert.equal(runError, null, 'a 409 must not escape startDownload as a rejection');

  const entries = failureEntries(page);
  assert.equal(entries.length, 1, 'the 409 item is recorded in the failure summary');
  assert.match(entries[0].textContent, /Video 1/, 'the item is identified by its title');
  assert.match(entries[0].textContent, /already running/i, 'the server error text is kept');
  assert.ok(
    entries[0].classList.contains('transient'),
    'a 409 is retryable, so it goes to the retry queue like any other failure',
  );
  assert.equal(page.$('#failure-panel-count').textContent, '1');

  // End the run without waiting out the 15s retry backoff: a real Cancel click
  // stops the loop (cancel semantics are unchanged).
  page.$('#cancel-btn').dispatchEvent(new page.window.MouseEvent('click', { bubbles: true }));
  await run;

  assert.equal(runError, null, 'the run still resolved after cancelling');
});

test('a stream that errors mid-read leaves the Start button visible and Cancel hidden afterwards', async (t) => {
  const page = await loadPage({ routes: (f) => f.sse('/download') });
  t.after(() => page.close());
  await queueVideos(page, ['https://youtu.be/dies', 'https://youtu.be/after']);

  const run = page.window.startDownload();
  const runOutcome = run.then(() => null, (e) => e);

  const dying = await page.fetchStub.nextStream(0);
  dying.send({ type: 'phase', msg: 'Downloading: dies' });
  await flush();
  dying.fail(new Error('socket hang up'));

  // A second queued item keeps the loop out of its retry backoff and proves the
  // mid-read error did not escape startDownload (which would reject here and
  // never request the second item).
  const next = await page.fetchStub.nextStream(1);
  next.send({ type: 'cancelled', msg: 'cancelled' });
  next.close();

  assert.equal(await runOutcome, null, 'the mid-read error did not escape startDownload');

  assert.ok(!page.$('#start-btn').classList.contains('hidden'), 'Start is available again');
  assert.ok(page.$('#cancel-btn').classList.contains('hidden'), 'Cancel is hidden again');

  const entries = failureEntries(page);
  assert.equal(entries.length, 1, 'the dropped stream is recorded as a retryable failure');
  assert.match(entries[0].textContent, /socket hang up/i, 'the read error is kept as the reason');
  assert.ok(entries[0].classList.contains('transient'), 'a read error retries');
});

test('a stream that closes with no outcome leaves the item failed', async (t) => {
  const page = await loadPage({
    routes: (f) => {
      f.sse('/download');
      f.json('/cancel', { status: 'cancelled' });
    },
  });
  t.after(() => page.close());
  await queueVideos(page, ['https://youtu.be/silent']);

  const run = page.window.startDownload();
  const s = await page.fetchStub.nextStream(0);
  s.send({ type: 'phase', msg: 'Downloading: silent' });
  // A clean close with neither an item event nor the terminal `done`: this is
  // the dropped-stream / encoder-abort-cap shape, so it must not count as done.
  s.close();
  await flush(10);

  const entries = failureEntries(page);
  assert.equal(entries.length, 1, 'an item with no outcome is recorded as failed');
  assert.match(entries[0].textContent, /Video 1/, 'the item is identified by its title');
  assert.match(entries[0].textContent, /no result/i, 'the missing-outcome reason is kept');
  assert.ok(entries[0].classList.contains('transient'), 'a missing outcome retries');

  // End the run without waiting out the retry backoff.
  page.$('#cancel-btn').dispatchEvent(new page.window.MouseEvent('click', { bubbles: true }));
  await run;
});
