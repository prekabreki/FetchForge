// #130: the page-load yt-dlp update must add ONE short line to the Progress
// log, never pip's raw "Requirement already satisfied" dump. The server sends
// that dump in `output` (for server.log) and the line to show in `summary`.

import test from 'node:test';
import assert from 'node:assert/strict';
import { loadPage, flush, sleep } from './harness.mjs';

const PIP_OUTPUT = [
  'Requirement already satisfied: yt-dlp[default] in c:\\venv\\lib\\site-packages (2026.10.23)',
  'Requirement already satisfied: brotli in c:\\venv\\lib\\site-packages (from yt-dlp[default]) (1.1.0)',
  'Requirement already satisfied: certifi in c:\\venv\\lib\\site-packages (from yt-dlp[default]) (2025.8.3)',
  'Requirement already satisfied: mutagen in c:\\venv\\lib\\site-packages (from yt-dlp[default]) (1.47.0)',
  '',
  '[notice] A new release of pip is available: 25.2 -> 25.3',
  '[notice] To update, run: python.exe -m pip install --upgrade pip',
].join('\r\n');

async function ytdlpLines(page) {
  await flush();
  await sleep(50);   // _flushLog runs on the next animation frame
  return page.$$('#log-box > div')
    .filter((el) => /yt-dlp|Requirement|notice/.test(el.textContent));
}

test('an up-to-date page-load update logs exactly one summary line', async (t) => {
  const page = await loadPage({
    routes: (f) => f.json('/update-ytdlp', {
      ok: true, summary: 'yt-dlp up to date (2026.10.23)', output: PIP_OUTPUT,
    }),
  });
  t.after(() => page.close());

  const lines = await ytdlpLines(page);
  assert.deepEqual(lines.map((l) => l.textContent), ['yt-dlp up to date (2026.10.23)']);
  assert.ok(!page.$('#log-box').textContent.includes('Requirement'), 'no pip dump in the log');
  assert.equal(lines[0].className, '', 'a successful update is not shown as an error');
});

test('a failed page-load update still shows its reason, as an error', async (t) => {
  const page = await loadPage({
    routes: (f) => f.json('/update-ytdlp', {
      ok: false,
      summary: 'yt-dlp update failed: ERROR: Could not find a version that satisfies yt-dlp',
      output: 'Collecting yt-dlp\r\nERROR: Could not find a version that satisfies yt-dlp',
    }),
  });
  t.after(() => page.close());

  const lines = await ytdlpLines(page);
  assert.equal(lines.length, 1, JSON.stringify(lines.map((l) => l.textContent)));
  assert.match(lines[0].textContent, /^yt-dlp update failed: .*Could not find a version/);
  assert.equal(lines[0].className, 'log-error');
});
