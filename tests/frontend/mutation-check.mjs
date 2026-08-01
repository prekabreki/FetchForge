// Proves the suite in this directory is load-bearing.
//
// The failure mode for a harness like this is looking green while testing
// nothing — the danger zone called out on issue #42. So: take the real
// index.html, break one guard in it, run the tests that claim to cover that
// guard against the broken copy, and require them to FAIL. A mutation that
// survives means the test that "covers" it does not.
//
// Nothing is written to fetchforge/index.html; every mutant is a temp copy
// under .tmp/ that the harness is pointed at via FETCHFORGE_INDEX_HTML.
//
//   node mutation-check.mjs          (or: npm run test:mutations)

import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { INDEX_HTML } from './harness.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const TMP = path.join(HERE, '.tmp');

// Each mutation removes one guard and names the tests that must notice.
// `find` must match exactly once in index.html; if it stops matching, the
// check fails loudly rather than quietly proving nothing.
const MUTATIONS = [
  {
    id: 'hold/keyboard-auto-repeat',
    guard: 'the e.repeat guard on the hold-to-confirm keyboard path',
    breaks: 'a resting finger on Enter auto-repeats into a second press, clearing the queue',
    find: 'if (e.repeat) return;',
    replace: 'if (false) return;',
    file: 'hold-confirm.test.mjs',
    expect: [
      'auto-repeat Enter does not count as the second press',
      'auto-repeat Enter cannot arm an idle control either',
    ],
  },
  {
    id: 'hold/secondary-button',
    guard: 'the e.button > 0 guard on pointerdown',
    breaks: 'a right-click begins a destructive hold',
    find: 'if (e.button > 0) return;',
    replace: 'if (false) return;',
    file: 'hold-confirm.test.mjs',
    expect: ['a secondary mouse button never starts a hold'],
  },
  {
    id: 'pr38/playlist-checklist-on-start',
    guard: 'the playlist branch at the top of startDownload()',
    breaks: 'Start seeds the raw playlist URL, so the server expands the whole playlist and the checklist is ignored',
    find: 'if (_playlistEntries.length) {\n    addToQueue();',
    replace: 'if (false) {\n    addToQueue();',
    file: 'queue-and-progress.test.mjs',
    expect: [
      'Start with an empty queue downloads only the ticked playlist entries',
      'Start with nothing ticked aborts instead of falling back to the playlist URL',
    ],
  },
  {
    id: 'pr38/client-owns-overall',
    guard: 'clientOwnsOverall()',
    breaks: 'every total:1 video_start hides the job bar, so a multi-item queue shows no job-level progress',
    find: 'return _queueProgress.total > 1 || _queueProgress.completed > 1;',
    replace: 'return false;',
    file: 'queue-and-progress.test.mjs',
    expect: ['the job bar stays visible and advances across a 3-item queue of total:1 requests'],
  },
  {
    id: 'pr38/overall-fraction',
    guard: 'the completed-items term in renderOverall()',
    breaks: 'the job bar restarts from zero on every item instead of advancing monotonically',
    find: '((completed - 1) + within) / total',
    replace: 'within / total',
    file: 'queue-and-progress.test.mjs',
    expect: ['the job bar stays visible and advances across a 3-item queue of total:1 requests'],
  },
];

const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

function countOccurrences(haystack, needle) {
  let n = 0;
  for (let i = haystack.indexOf(needle); i !== -1; i = haystack.indexOf(needle, i + needle.length)) n++;
  return n;
}

/** Parse `node --test --test-reporter=tap` output into { name: passed }. */
function parseTap(out) {
  const results = new Map();
  for (const line of out.split('\n')) {
    const m = /^\s*(not ok|ok)\s+\d+\s+-\s+(.*?)\s*$/.exec(line);
    if (m) results.set(m[2], m[1] === 'ok');
  }
  return results;
}

const source = fs.readFileSync(INDEX_HTML, 'utf8').replace(/\r\n/g, '\n');
fs.rmSync(TMP, { recursive: true, force: true });
fs.mkdirSync(TMP, { recursive: true });

const problems = [];
console.log(`mutation check against ${INDEX_HTML}\n`);

for (const m of MUTATIONS) {
  const hits = countOccurrences(source, m.find);
  if (hits !== 1) {
    problems.push(
      `[${m.id}] anchor matched ${hits} times, expected exactly 1. index.html moved under the ` +
      `mutation check — re-point it at ${m.guard} before trusting this suite.\n    anchor: ${JSON.stringify(m.find)}`,
    );
    console.log(`✗ ${m.id}: ANCHOR NOT FOUND (${hits} matches)`);
    continue;
  }

  const mutantPath = path.join(TMP, m.id.replace(/\W+/g, '-') + '.html');
  fs.writeFileSync(mutantPath, source.replace(m.find, m.replace));

  const pattern = m.expect.map(escapeRe).join('|');
  const run = spawnSync(
    process.execPath,
    // A mutant can make a test hang rather than fail (a queue loop that never
    // finishes because the app took a path the test does not feed). The timeout
    // turns that into a plain failure instead of a stalled check.
    ['--test', '--test-timeout=20000', '--test-reporter=tap',
      `--test-name-pattern=^(?:${pattern})$`, m.file],
    { cwd: HERE, encoding: 'utf8', env: { ...process.env, FETCHFORGE_INDEX_HTML: mutantPath } },
  );
  const results = parseTap((run.stdout || '') + (run.stderr || ''));

  const survived = [];
  for (const name of m.expect) {
    if (!results.has(name)) survived.push(`${name} — did not run at all (renamed?)`);
    else if (results.get(name)) survived.push(`${name} — PASSED against the broken copy`);
  }

  if (survived.length) {
    problems.push(`[${m.id}] mutation survived:\n    ` + survived.join('\n    '));
    console.log(`✗ ${m.id}: mutation SURVIVED`);
    survived.forEach((s) => console.log(`    ${s}`));
  } else {
    console.log(`✓ ${m.id}: caught by ${m.expect.length} test(s) — ${m.guard}`);
    m.expect.forEach((n) => console.log(`    not ok - ${n}`));
  }
}

console.log('');
if (problems.length) {
  console.log(`FAILED — ${problems.length} of ${MUTATIONS.length} mutation(s) not caught:\n`);
  problems.forEach((p) => console.log('  ' + p + '\n'));
  process.exit(1);
}
console.log(`OK — all ${MUTATIONS.length} mutations were caught by the suite.`);
