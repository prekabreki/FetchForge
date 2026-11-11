// Issue #99: an ffmpeg without NVENC must reach the existing capability banner.
// The server owns *whether* to warn; this proves the page draws the no-NVENC
// report and omits the "GPU encoding itself is working" reassurance that would
// be false in that mode.

import test from 'node:test';
import assert from 'node:assert/strict';
import { loadPage, flush } from './harness.mjs';

const NO_NVENC_REPORT = {
  ffmpeg_path: '/usr/bin/ffmpeg',
  ffmpeg_version: '6.1.1',
  capabilities: [
    {
      key: 'nvenc',
      name: 'NVENC video encoding (hevc_nvenc)',
      ok: false,
      cause: 'encoder_missing',
      impact: 'Video conversion is unavailable — download-only and audio extraction still work.',
      remedy: 'Install an ffmpeg built with NVENC (--enable-nvenc) on a machine with an NVIDIA GPU.',
    },
    {
      key: 'nvenc_uhq_tune',
      name: 'Best-quality encode mode (uhq)',
      ok: false,
      cause: 'nvenc_missing',
      impact: 'Video conversion is unavailable.',
      remedy: 'NVENC is unavailable, so this mode cannot run.',
    },
  ],
  warning: {
    headline: 'Video conversion is unavailable — this ffmpeg has no NVENC encoder',
    ffmpeg_path: '/usr/bin/ffmpeg',
    ffmpeg_version: '6.1.1',
    items: [{
      name: 'NVENC video encoding (hevc_nvenc)',
      impact: 'Video conversion is unavailable — download-only and audio extraction still work.',
      remedy: 'Install an ffmpeg built with NVENC (--enable-nvenc) on a machine with an NVIDIA GPU.',
    }],
    text: 'Video conversion is unavailable | ffmpeg: /usr/bin/ffmpeg (6.1.1)',
  },
};

const HEALTHY_REPORT = {
  ffmpeg_path: '/usr/bin/ffmpeg',
  ffmpeg_version: '7.1.1',
  capabilities: [
    { key: 'nvenc', name: 'NVENC video encoding (hevc_nvenc)', ok: true, cause: 'supported', impact: '', remedy: '' },
    { key: 'nvenc_uhq_tune', name: 'Best-quality encode mode (uhq)', ok: true, cause: 'supported', impact: '', remedy: '' },
    { key: 'scale_cuda', name: 'scale_cuda', ok: true, cause: 'supported', impact: '', remedy: '' },
    { key: 'libplacebo', name: 'libplacebo', ok: true, cause: 'supported', impact: '', remedy: '' },
  ],
  warning: null,
};

test('the no-NVENC warning renders with its remedy and no reassurance', async (t) => {
  const page = await loadPage({ routes: (f) => f.json('/capabilities', NO_NVENC_REPORT) });
  t.after(() => page.close());
  await flush();

  assert.equal(page.$('#cap-warning').hidden, false, 'the banner must be visible');
  assert.match(page.$('#cap-warning-head').textContent, /Video conversion is unavailable/);
  assert.match(page.$('#cap-warning-head').textContent, /no NVENC encoder/);
  assert.match(page.$('#cap-warning-list').textContent, /NVENC video encoding/);
  assert.match(page.$('#cap-warning-list').textContent, /--enable-nvenc/);
  assert.equal(page.$('#cap-warning-note').hidden, true, 'no false GPU-working reassurance');
  assert.equal(page.$('#cap-warning-path').textContent, '/usr/bin/ffmpeg');
  assert.equal(page.$('#cap-warning-version').textContent, '6.1.1');
});

test('a healthy capability report leaves the banner hidden', async (t) => {
  const page = await loadPage({ routes: (f) => f.json('/capabilities', HEALTHY_REPORT) });
  t.after(() => page.close());
  await flush();

  assert.equal(page.$('#cap-warning').hidden, true);
});
