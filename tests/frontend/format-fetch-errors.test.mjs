// Regression net for #67: a failed format fetch must never be silent, and must
// never leave the previous URL's itags selectable. Before the fix a failed
// resolve kept the old dropdowns, and a stale `140-drc` then failed every item
// of a 110-video queue one by one.

import test from 'node:test';
import assert from 'node:assert/strict';
import { loadPage, fetchInfoFor, singleVideoInfo, sleep } from './harness.mjs';

const GOOD_URL = 'https://youtu.be/good-video';
const BAD_URL = 'https://youtu.be/bad-video';

async function withResolvedVideo(t) {
  const page = await loadPage();
  t.after(() => page.close());
  await fetchInfoFor(page, GOOD_URL, singleVideoInfo());
  assert.equal(page.$$('#audio-format option').length, 1, 'setup: good video populates audio formats');
  assert.equal(page.$$('#video-format option').length, 1, 'setup: good video populates video formats');
  return page;
}

async function errorLines(page) {
  await sleep(50);   // _flushLog runs on the next animation frame
  return page.$$('#log-box .log-error').map((el) => el.textContent);
}

test('a failed resolve clears the previous video\'s formats', async (t) => {
  const page = await withResolvedVideo(t);
  page.fetchStub.json('/video-info', { error: 'ERROR: Video unavailable' }, { ok: false, status: 400 });
  page.$('#url-input').value = BAD_URL;
  await page.window.fetchInfo();

  assert.equal(page.$$('#audio-format option').length, 0, 'stale audio itags must be cleared');
  assert.equal(page.$$('#video-format option').length, 0, 'stale video itags must be cleared');
  assert.ok((await errorLines(page)).some((l) => l.includes('Video unavailable')));
});

test('a title-only resolve with a format error shows the reason', async (t) => {
  const page = await withResolvedVideo(t);
  const info = {
    ...singleVideoInfo('Bot-checked Video'),
    video_formats: [], audio_formats: [],
    error: 'Sign in to confirm you\'re not a bot',
  };
  await fetchInfoFor(page, BAD_URL, info);

  assert.equal(page.$$('#audio-format option').length, 0, 'no stale audio itags');
  assert.equal(page.$$('#video-format option').length, 0, 'no stale video itags');
  const lines = await errorLines(page);
  assert.ok(lines.some((l) => l.includes('not a bot')), 'the reason reaches the log: ' + JSON.stringify(lines));
});

test('a healthy resolve logs no format error', async (t) => {
  const page = await withResolvedVideo(t);
  assert.deepEqual(await errorLines(page), []);
});
