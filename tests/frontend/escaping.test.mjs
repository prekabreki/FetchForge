// Titles and paths are interpolated into innerHTML in several renderers.
// A title containing `&`, `<` or `"` must render literally and must never
// create elements or break an attribute (audit finding M3, #98).

import test from 'node:test';
import assert from 'node:assert/strict';
import { loadPage, fetchInfoFor, flush, singleVideoInfo } from './harness.mjs';

const TITLE = 'Tom & Jerry <b>Remastered</b> "4K"';

// The queue row prepends a DL/ENC tag element, so the title itself is the
// text node after it.
function queueTitleText(page) {
  const el = page.$('#dl-queue-items .queue-title');
  assert.ok(el, 'the queue row rendered');
  return el.lastChild.textContent;
}

test('a queue title with markup characters renders literally', async (t) => {
  const page = await loadPage();
  t.after(() => page.close());

  await fetchInfoFor(page, 'https://youtu.be/evil', singleVideoInfo(TITLE));
  page.window.addToQueue();
  await flush();

  assert.equal(queueTitleText(page), TITLE, 'the queue row shows the title verbatim');
  assert.equal(page.$('#dl-queue-items b'), null, 'no <b> element is created from the title');
});

test('a history title with markup characters renders literally', async (t) => {
  const page = await loadPage({
    routes: (f) => f.json('/history', [{
      title: TITLE,
      url: 'https://youtu.be/evil',
      thumbnail: '',
      added: '2026-11-07T10:00:00',
    }]),
  });
  t.after(() => page.close());
  await flush();

  const titleEl = page.$('#history-list .history-title');
  assert.ok(titleEl, 'the history entry rendered');
  assert.equal(titleEl.textContent, TITLE, 'the history title shows verbatim');
  assert.equal(page.$('#history-list b'), null, 'no <b> element is created from the title');
});
