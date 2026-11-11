// #102: the encode-parameter pills come from the typed `encode_params` event.
// Before the fix the UI regex-parsed the "Encode params: ..." log line, so
// rewording that line silently emptied the pills row.

import test from 'node:test';
import assert from 'node:assert/strict';
import { loadPage, queueVideos, flush } from './harness.mjs';

async function startOne(page) {
  await queueVideos(page, ['https://youtu.be/one']);
  const run = page.window.startDownload();
  const stream = await page.fetchStub.nextStream(0);
  return { run, stream };
}

const pillTexts = (page) =>
  page.$$('#encode-params-pills .encode-pill').map((p) => p.textContent);

test('an encode_params event renders the pills row', async (t) => {
  const page = await loadPage({ routes: (f) => f.sse('/download') });
  t.after(() => page.close());
  const { run, stream } = await startOne(page);

  stream.send({
    type: 'encode_params',
    cq: 22, maxrate: '20M', preset: 'p4', tune: 'uhq', pix_fmt: 'yuv420p',
  });
  await flush();

  assert.notEqual(page.styleOf('#encode-params-row', 'display'), 'none', 'the row is shown');
  assert.deepEqual(pillTexts(page), ['CQ22', 'maxrate20M', 'presetp4', 'tuneuhq', 'pixyuv420p']);

  stream.send({ type: 'done', msg: 'ok' });
  stream.close();
  await run;
});

test('a log line containing "Encode params:" does not render the pills', async (t) => {
  const page = await loadPage({ routes: (f) => f.sse('/download') });
  t.after(() => page.close());
  const { run, stream } = await startOne(page);

  stream.send({ type: 'log', msg: 'Encode params: CQ=22 maxrate=20M preset=p4 tune=uhq pix=yuv420p' });
  await flush();

  assert.equal(page.styleOf('#encode-params-row', 'display'), 'none', 'the row stays hidden');
  assert.equal(page.$$('#encode-params-pills .encode-pill').length, 0, 'no pills are built from log text');

  stream.send({ type: 'done', msg: 'ok' });
  stream.close();
  await run;
});
