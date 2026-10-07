// #76 — capture only a From–To part of a single video.
//
// Observed the way the server sees it (the decoded /download form body, or the
// per-item `items` JSON of a pipelined batch) and the way the user sees it (the
// queue row, the log, the From/To row's visibility).

import test from 'node:test';
import assert from 'node:assert/strict';
import { loadPage, fetchInfoFor, singleVideoInfo, playlistInfo, flush, sleep } from './harness.mjs';

const URL_A = 'https://youtu.be/range-a';
const URL_B = 'https://youtu.be/range-b';

function hourLongVideo(title = 'Hour Long') {
  return { ...singleVideoInfo(title), duration: 3600 };
}

async function pageWithVideo(t, url = URL_A) {
  const page = await loadPage({ routes: (f) => f.sse('/download') });
  t.after(() => page.close());
  await fetchInfoFor(page, url, hourLongVideo());
  return page;
}

function setRange(page, from, to) {
  page.$('#section-start').value = from;
  page.$('#section-end').value = to;
}

async function errors(page) {
  await sleep(50);   // _flushLog runs on the next animation frame
  return page.$$('#log-box .log-error').map((el) => el.textContent);
}
const queueRows = (page) => page.$$('#dl-queue-items > div');
const downloads = (page) => page.fetchStub.callsTo('/download');

async function startAndFinish(page) {
  const run = page.window.startDownload();
  const s = await page.fetchStub.nextStream(0);
  s.send({ type: 'done', msg: 'ok' });
  s.close();
  await run;
}

test('a From/To range rides the /download request as whole seconds', async (t) => {
  const page = await pageWithVideo(t);
  setRange(page, '12:30', '18:00');
  await startAndFinish(page);

  const form = downloads(page)[0].form;
  assert.equal(form.section_start, '750');
  assert.equal(form.section_end, '1080');
});

test('H:MM:SS and bare seconds parse, and a blank To means to the end', async (t) => {
  const page = await pageWithVideo(t);
  setRange(page, '0:45:05', '');
  await startAndFinish(page);
  const form = downloads(page)[0].form;
  assert.equal(form.section_start, '2705');
  assert.equal(form.section_end, '');
});

test('blank fields send no range at all', async (t) => {
  const page = await pageWithVideo(t);
  await startAndFinish(page);
  const form = downloads(page)[0].form;
  assert.equal(form.section_start, '');
  assert.equal(form.section_end, '');
});

test('To before From is refused and nothing is queued or sent', async (t) => {
  const page = await pageWithVideo(t);
  setRange(page, '18:00', '12:30');
  page.window.addToQueue();
  await flush();
  assert.equal(queueRows(page).length, 0);
  assert.match((await errors(page)).join('\n'), /must be after/);

  await page.window.startDownload();
  assert.equal(downloads(page).length, 0, 'Start must not send the bad range either');
});

test('garbage and past-the-end times are refused', async (t) => {
  const page = await pageWithVideo(t);
  for (const [from, to, why] of [['12:3x', '', /SS, MM:SS/], ['1:75', '', /SS, MM:SS/], ['', '2:00:00', /only 1:00:00 long/]]) {
    setRange(page, from, to);
    page.window.addToQueue();
    await flush();
    assert.equal(queueRows(page).length, 0, `${from}-${to} must not queue`);
    assert.match((await errors(page)).at(-1), why);
  }
});

test('the queue row shows the range, and two ranges of one video are two jobs', async (t) => {
  const page = await pageWithVideo(t);
  setRange(page, '12:30', '18:00');
  page.window.addToQueue();
  await flush();
  assert.equal(page.$('#section-start').value, '', 'fields clear once the item is queued');

  await fetchInfoFor(page, URL_A, hourLongVideo());
  setRange(page, '40:00', '');
  page.window.addToQueue();
  await flush();

  await fetchInfoFor(page, URL_A, hourLongVideo());
  setRange(page, '12:30', '18:00');
  page.window.addToQueue();
  await flush();

  const ranges = page.$$('#dl-queue-items .queue-range').map((el) => el.textContent);
  assert.deepEqual(ranges, ['12:30–18:00', '40:00–end'], 'the identical third range is deduped');
});

test('a pipelined batch carries each item its own range', async (t) => {
  const page = await loadPage({ routes: (f) => f.sse('/download') });
  t.after(() => page.close());
  page.$('#convert-toggle').checked = true;
  page.$('#pipeline-check').checked = true;

  await fetchInfoFor(page, URL_A, hourLongVideo('A'));
  setRange(page, '1:00', '2:00');
  page.window.addToQueue();
  await fetchInfoFor(page, URL_B, hourLongVideo('B'));
  page.window.addToQueue();
  await flush();

  const run = page.window.startDownload();
  const s = await page.fetchStub.nextStream(0);
  const items = JSON.parse(downloads(page)[0].form.items);
  assert.deepEqual(items.map((i) => [i.url, i.section_start, i.section_end]),
    [[URL_A, 60, 120], [URL_B, null, null]]);
  s.send({ type: 'item_done', idx: 1 });
  s.send({ type: 'item_done', idx: 2 });
  s.send({ type: 'done', msg: 'ok' });
  s.close();
  await run;
});

test('a playlist hides the range row, and a new fetch clears stale values', async (t) => {
  const page = await pageWithVideo(t);
  setRange(page, '1:00', '2:00');
  await fetchInfoFor(page, 'https://www.youtube.com/playlist?list=PLx',
    playlistInfo([{ url: URL_B, title: 'B' }]));
  assert.equal(page.$('#section-row').style.display, 'none');
  assert.equal(page.$('#section-start').value, '');

  await fetchInfoFor(page, URL_A, hourLongVideo());
  assert.equal(page.$('#section-row').style.display, '');
});
