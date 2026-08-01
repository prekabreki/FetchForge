// Regression net for the two bugs fixed in PR #38.
//
// 1. Start with a resolved playlist must honour the per-video checklist (#15).
//    Before the fix, pressing Start with an empty queue seeded the raw playlist
//    URL instead, and the server expanded the whole playlist from the top —
//    silently downloading videos the user had unticked.
//
// 2. The job-level ("overall") bar must survive a queue of single-video
//    requests. Since playlist expansion moved client-side the server sees one
//    video per request and reports total: 1, so a server-driven bar hides
//    itself on every item. The client queue owns the bar when it holds more
//    than one item, and cedes it for a single item or a pipelined batch.
//
// Both are observed the way the server would see them — the decoded form body
// of the real fetch — and the way the user would see them, in the DOM.

import test from 'node:test';
import assert from 'node:assert/strict';
import { loadPage, fetchInfoFor, queueVideos, playlistInfo, flush } from './harness.mjs';

const PLAYLIST_URL = 'https://www.youtube.com/playlist?list=PLtest';
const ENTRIES = [
  { url: 'https://youtu.be/entry-one', title: 'Entry One' },
  { url: 'https://youtu.be/entry-two', title: 'Entry Two' },
  { url: 'https://youtu.be/entry-three', title: 'Entry Three' },
];

async function withPlaylist(t) {
  const page = await loadPage({ routes: (f) => f.sse('/download') });
  t.after(() => page.close());
  await fetchInfoFor(page, PLAYLIST_URL, playlistInfo(ENTRIES));
  const boxes = page.$$('#playlist-items input[type="checkbox"]');
  assert.equal(boxes.length, ENTRIES.length, 'setup: one checkbox per playlist entry');
  assert.ok(boxes.every((b) => b.checked), 'setup: a resolved playlist starts all-checked');
  return { page, boxes };
}

/** Untick one entry through its real change handler. */
function untick(page, box) {
  box.checked = false;
  box.dispatchEvent(new page.window.Event('change', { bubbles: true }));
}

/** URLs the page actually POSTed to /download, in order. */
const requestedUrls = (page) => page.fetchStub.callsTo('/download').map((c) => c.form.url);

// ---------------------------------------------------------------------------
// #38 bug 1 — playlist checklist honoured on Start
// ---------------------------------------------------------------------------

test('Start with an empty queue downloads only the ticked playlist entries', async (t) => {
  const { page, boxes } = await withPlaylist(t);
  untick(page, boxes[1]);
  assert.equal(page.$('#playlist-sel-count').textContent, '2', 'the count reflects the untick');

  const run = page.window.startDownload();

  const first = await page.fetchStub.nextStream(0);
  assert.equal(requestedUrls(page)[0], ENTRIES[0].url, 'first request is the first ticked entry');
  assert.equal(
    page.fetchStub.callsTo('/download')[0].form.video_format, '',
    'playlist items leave video_format blank so each takes its best stream (#13)',
  );
  first.send({ type: 'done', msg: 'ok' });
  first.close();

  const second = await page.fetchStub.nextStream(1);
  assert.equal(requestedUrls(page)[1], ENTRIES[2].url, 'unticked entry two is skipped');
  second.send({ type: 'done', msg: 'ok' });
  second.close();

  await run;

  assert.deepEqual(requestedUrls(page), [ENTRIES[0].url, ENTRIES[2].url]);
  assert.ok(
    !requestedUrls(page).includes(PLAYLIST_URL),
    'the raw playlist URL must never be sent — the server would expand it and ignore the picks',
  );
});

test('Start with nothing ticked aborts instead of falling back to the playlist URL', async (t) => {
  const { page } = await withPlaylist(t);
  page.window.playlistSelectAll(false);
  await flush();
  assert.equal(page.$('#playlist-sel-count').textContent, '0');

  // Deliberately not awaited: the abort path returns before the first await, and
  // if the abort is ever broken this must fail on the assertion below rather than
  // block forever on a run that was never supposed to start.
  page.window.startDownload();
  await flush();

  assert.equal(page.fetchStub.callsTo('/download').length, 0, 'no download is started at all');
  assert.ok(
    !page.$('#start-btn').classList.contains('hidden'),
    'the run never began, so Start stays available',
  );
});

test('Start merges the ticked entries into an already-populated queue', async (t) => {
  const page = await loadPage({ routes: (f) => f.sse('/download') });
  t.after(() => page.close());

  await queueVideos(page, ['https://youtu.be/preexisting']);
  await fetchInfoFor(page, PLAYLIST_URL, playlistInfo(ENTRIES));
  const boxes = page.$$('#playlist-items input[type="checkbox"]');
  untick(page, boxes[0]);
  untick(page, boxes[1]);

  const run = page.window.startDownload();
  for (let i = 0; i < 2; i++) {
    const s = await page.fetchStub.nextStream(i);
    s.send({ type: 'done', msg: 'ok' });
    s.close();
  }
  await run;

  assert.deepEqual(
    requestedUrls(page),
    ['https://youtu.be/preexisting', ENTRIES[2].url],
    'the queued item runs first, then the one ticked playlist entry',
  );
});

// ---------------------------------------------------------------------------
// #38 bug 2 — the job-level bar across N single-video requests
// ---------------------------------------------------------------------------

const overall = (page) => ({
  visible: page.styleOf('#overall-section', 'display') !== 'none',
  label: page.$('#stat-overall').textContent,
  width: parseFloat(page.styleOf('#overall-bar-fill', 'width')) || 0,
});

test('the job bar stays visible and advances across a 3-item queue of total:1 requests', async (t) => {
  const page = await loadPage({ routes: (f) => f.sse('/download') });
  t.after(() => page.close());

  await queueVideos(page, [
    'https://youtu.be/one', 'https://youtu.be/two', 'https://youtu.be/three',
  ]);

  const run = page.window.startDownload();
  const samples = [];
  const labels = [];

  for (let i = 0; i < 3; i++) {
    const s = await page.fetchStub.nextStream(i);
    samples.push(overall(page));                       // client set the bar before the request
    // The server only ever sees one video per request, so it says total: 1.
    s.send({ type: 'video_start', current: 1, total: 1 });
    await flush();
    samples.push(overall(page));
    labels.push(overall(page).label);
    s.send({ type: 'progress', pct: 50, overall: 50, speed: '', eta: '', size: '' });
    await flush();
    samples.push(overall(page));
    s.send({ type: 'done', msg: 'ok' });
    s.close();
  }
  await run;

  assert.ok(
    samples.every((s) => s.visible),
    'the bar must never be hidden mid-queue by a total:1 video_start\n' + JSON.stringify(samples, null, 2),
  );
  assert.deepEqual(labels, ['1 / 3', '2 / 3', '3 / 3'], 'the counter tracks the client queue, not the request');

  const widths = samples.map((s) => s.width);
  for (let i = 1; i < widths.length; i++) {
    assert.ok(widths[i] >= widths[i - 1], `bar went backwards at sample ${i}: ${widths.join(' -> ')}`);
  }
  assert.ok(widths.at(-1) > widths[0], `bar never advanced: ${widths.join(' -> ')}`);
  // (completed-1 + within)/total, with the server's per-request overall folded
  // in as the fraction within the current item.
  assert.deepEqual(widths, [0, 0, 16.7, 33.3, 33.3, 50, 66.7, 66.7, 83.3]);
});

test('a single-item queue cedes the bar to the server', async (t) => {
  const page = await loadPage({ routes: (f) => f.sse('/download') });
  t.after(() => page.close());

  await queueVideos(page, ['https://youtu.be/solo']);
  const run = page.window.startDownload();
  const s = await page.fetchStub.nextStream(0);

  s.send({ type: 'video_start', current: 1, total: 1 });
  await flush();
  assert.equal(overall(page).visible, false, 'one video, one request: no job-level bar');

  s.send({ type: 'progress', pct: 10, overall: 42, speed: '', eta: '', size: '' });
  await flush();
  assert.equal(overall(page).width, 42, 'the server writes the bar directly on the ceded path');

  s.send({ type: 'done', msg: 'ok' });
  s.close();
  await run;
});

test('a pipelined batch cedes the bar to the server, which spans the whole run', async (t) => {
  const page = await loadPage({ routes: (f) => f.sse('/download') });
  t.after(() => page.close());

  page.$('#convert-toggle').checked = true;
  page.$('#pipeline-check').checked = true;
  await queueVideos(page, ['https://youtu.be/b1', 'https://youtu.be/b2']);

  const run = page.window.startDownload();
  const s = await page.fetchStub.nextStream(0);

  const calls = page.fetchStub.callsTo('/download');
  assert.equal(calls.length, 1, 'two consecutive youtube items go out as ONE pipelined request');
  assert.equal(JSON.parse(calls[0].form.items).length, 2);
  assert.equal(calls[0].form.pipeline, 'true');

  // Here the server really does span the run, so its numbers must win.
  s.send({ type: 'video_start', current: 1, total: 2 });
  await flush();
  assert.equal(overall(page).visible, true);
  assert.equal(overall(page).label, '1 / 2', 'the server drives the counter for a batch');

  s.send({ type: 'video_start', current: 2, total: 2 });
  await flush();
  assert.equal(overall(page).label, '2 / 2');
  assert.equal(overall(page).width, 50);

  s.send({ type: 'item_done', idx: 1 });
  s.send({ type: 'item_done', idx: 2 });
  s.send({ type: 'done', msg: 'ok' });
  s.close();
  await run;
});
