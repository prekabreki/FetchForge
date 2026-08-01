// The hold-to-confirm state machine (_holdStart/_holdStop/_holdArm/_holdDisarm).
//
// It guards the only two irreversible actions in the UI — clearing the server-side
// download history, and clearing a job queue that can hold dozens of hand-picked
// entries. Every assertion here is about one of two things: the gesture DOES clear
// when it should, and it does NOT clear when it shouldn't. Both directions matter;
// a guard that never fires is as broken as one that fires by accident.
//
// The control is selected by its data-hold-action attribute and its state is read
// from classes and from the label HTML captured at load — never from glyphs or
// label text, which are being restyled independently.

import test from 'node:test';
import assert from 'node:assert/strict';
import { loadPage, queueVideos, pointer, key, blur, sleep, flush } from './harness.mjs';

const HOLD_MS = 700;      // must stay in step with the page constant + CSS fill
const KEYARM_MS = 4000;

const QUEUED = 'block';
const CLEARED = 'none';

/** A page with two items on the job queue and the clear control located. */
async function withQueue(t) {
  const page = await loadPage();
  t.after(() => page.close());
  await queueVideos(page, ['https://youtu.be/aaaaaaaaaaa', 'https://youtu.be/bbbbbbbbbbb']);
  assert.equal(page.styleOf('#dl-queue-section', 'display'), QUEUED, 'setup: queue should be populated');
  const el = page.$('[data-hold-action="clearDlQueue"]');
  assert.ok(el, 'setup: the clear-queue hold control should exist');
  return { page, el, state: () => page.styleOf('#dl-queue-section', 'display') };
}

test('both hold controls are initialised by _initHoldConfirm', async (t) => {
  const page = await loadPage();
  t.after(() => page.close());

  const controls = page.$$('.hold-confirm');
  assert.equal(controls.length, 2, 'history clear + queue clear');
  for (const el of controls) {
    assert.ok(el.dataset.holdAction, 'each control names the action it invokes');
    assert.notEqual(el.dataset.idleAria, undefined, 'idle aria-label was captured for restore');
    const lbl = el.querySelector('.hold-confirm-label');
    assert.ok(lbl, 'each control has a label element');
    assert.notEqual(lbl.dataset.idleLabel, undefined, 'idle label markup was captured for restore');
    assert.equal(el.getAttribute('role'), 'button');
    assert.equal(el.getAttribute('tabindex'), '0', 'reachable by keyboard');
  }
  assert.deepEqual(
    controls.map((e) => e.dataset.holdAction).sort(),
    ['clearDlQueue', 'clearHistory'],
    'only the two known destructive actions are wired',
  );
});

test('a plain click does not clear the queue', async (t) => {
  const { page, el, state } = await withQueue(t);
  pointer(page.window, el, 'pointerdown');
  pointer(page.window, el, 'pointerup');
  el.dispatchEvent(new page.window.MouseEvent('click', { bubbles: true, cancelable: true }));
  await sleep(HOLD_MS + 150);
  assert.equal(state(), QUEUED, 'a click is not a hold');
});

test('a sustained pointer hold clears the queue', async (t) => {
  const { page, el, state } = await withQueue(t);
  pointer(page.window, el, 'pointerdown');
  assert.ok(el.classList.contains('holding'), 'the fill animation starts on pointerdown');
  await sleep(HOLD_MS + 150);
  assert.equal(state(), CLEARED, 'holding past HOLD_MS fires the action');
  assert.ok(!el.classList.contains('holding'), 'the holding class is cleaned up after firing');
});

test('releasing early aborts, and leaves no timer behind', async (t) => {
  const { page, el, state } = await withQueue(t);
  pointer(page.window, el, 'pointerdown');
  await sleep(150);
  pointer(page.window, el, 'pointerup');
  assert.ok(!el.classList.contains('holding'), 'release clears the visual immediately');
  // A timer that survived the release would delete the queue here, long after
  // there is anything on screen to explain it.
  await sleep(HOLD_MS + 300);
  assert.equal(state(), QUEUED, 'no orphaned timer fired after the release');
});

for (const abort of ['pointerup', 'pointerleave', 'pointercancel']) {
  test(`${abort} aborts an in-flight hold`, async (t) => {
    const { page, el, state } = await withQueue(t);
    pointer(page.window, el, 'pointerdown');
    await sleep(100);
    pointer(page.window, el, abort);
    await sleep(HOLD_MS + 300);
    assert.equal(state(), QUEUED);
  });
}

test('a secondary mouse button never starts a hold', async (t) => {
  const { page, el, state } = await withQueue(t);
  pointer(page.window, el, 'pointerdown', { button: 2 });
  assert.ok(!el.classList.contains('holding'), 'right-press is ignored outright');
  await sleep(HOLD_MS + 200);
  assert.equal(state(), QUEUED);
});

test('re-pressing does not stack timers', async (t) => {
  const { page, el, state } = await withQueue(t);
  // Press, release, press again: the second press must replace the first timer.
  pointer(page.window, el, 'pointerdown');
  await sleep(100);
  pointer(page.window, el, 'pointerup');
  pointer(page.window, el, 'pointerdown');
  await sleep(200);
  pointer(page.window, el, 'pointerup');
  await sleep(HOLD_MS + 300);
  assert.equal(state(), QUEUED, 'two partial holds do not add up to one complete hold');
});

test('Enter arms the control rather than firing it', async (t) => {
  const { page, el, state } = await withQueue(t);
  const lbl = el.querySelector('.hold-confirm-label');
  const idleLabel = lbl.dataset.idleLabel;
  const idleAria = el.dataset.idleAria;

  key(page.window, el, 'Enter');

  assert.ok(el.classList.contains('armed'), 'the control shows an armed state');
  assert.notEqual(lbl.innerHTML, idleLabel, 'the label changes to ask for confirmation');
  assert.notEqual(el.getAttribute('aria-label'), idleAria, 'the accessible name changes too');
  assert.match(el.getAttribute('aria-label'), /confirm/i, 'and says what a second press will do');
  assert.equal(state(), QUEUED, 'arming is not firing');
});

test('a second Enter fires the armed control', async (t) => {
  const { page, el, state } = await withQueue(t);
  key(page.window, el, 'Enter');
  key(page.window, el, 'Enter');
  await flush();
  assert.equal(state(), CLEARED);
  assert.ok(!el.classList.contains('armed'), 'firing disarms');
});

test('Space arms the control as well', async (t) => {
  const { page, el, state } = await withQueue(t);
  key(page.window, el, ' ');
  assert.ok(el.classList.contains('armed'));
  key(page.window, el, ' ');
  await flush();
  assert.equal(state(), CLEARED);
});

// ---- the regression this suite exists for --------------------------------
// A held-down Enter key auto-repeats. Without the e.repeat guard the repeats
// read as second presses, so resting a finger on Enter over a focused control
// silently deletes the queue.
test('auto-repeat Enter does not count as the second press', async (t) => {
  const { page, el, state } = await withQueue(t);
  key(page.window, el, 'Enter');                     // real press: arms
  assert.ok(el.classList.contains('armed'));
  for (let i = 0; i < 5; i++) key(page.window, el, 'Enter', { repeat: true });
  await flush();
  assert.equal(state(), QUEUED, 'auto-repeat must not confirm an irreversible action');
  assert.ok(el.classList.contains('armed'), 'and it leaves the control armed, waiting for a real press');
});

test('auto-repeat Enter cannot arm an idle control either', async (t) => {
  const { page, el, state } = await withQueue(t);
  for (let i = 0; i < 5; i++) key(page.window, el, 'Enter', { repeat: true });
  assert.ok(!el.classList.contains('armed'));
  assert.equal(state(), QUEUED);
});

test('an unrelated key neither arms nor fires', async (t) => {
  const { page, el, state } = await withQueue(t);
  key(page.window, el, 'a');
  key(page.window, el, 'Tab');
  assert.ok(!el.classList.contains('armed'));
  assert.equal(state(), QUEUED);
});

test('Escape disarms and restores the idle label', async (t) => {
  const { page, el, state } = await withQueue(t);
  const lbl = el.querySelector('.hold-confirm-label');
  const idleLabel = lbl.dataset.idleLabel;
  const idleAria = el.dataset.idleAria;

  key(page.window, el, 'Enter');
  key(page.window, el, 'Escape');

  assert.ok(!el.classList.contains('armed'));
  assert.equal(lbl.innerHTML, idleLabel, 'the exact idle markup comes back');
  assert.equal(el.getAttribute('aria-label'), idleAria);
  assert.equal(state(), QUEUED);
});

test('blurring away disarms', async (t) => {
  const { page, el, state } = await withQueue(t);
  key(page.window, el, 'Enter');
  blur(page.window, el);
  assert.ok(!el.classList.contains('armed'));
  assert.equal(state(), QUEUED);
  // Coming back needs a fresh arm, not a bare confirm.
  key(page.window, el, 'Enter');
  await flush();
  assert.equal(state(), QUEUED, 'the first press after a blur re-arms');
  assert.ok(el.classList.contains('armed'));
});

test('the armed window expires on its own', async (t) => {
  const { page, el, state } = await withQueue(t);
  const lbl = el.querySelector('.hold-confirm-label');

  key(page.window, el, 'Enter');
  await sleep(KEYARM_MS + 300);

  assert.ok(!el.classList.contains('armed'), 'the arm times out instead of persisting');
  assert.equal(lbl.innerHTML, lbl.dataset.idleLabel);
  assert.equal(state(), QUEUED);

  key(page.window, el, 'Enter');
  await flush();
  assert.equal(state(), QUEUED, 'a press after expiry re-arms rather than firing');
});

test('starting a pointer hold cancels a pending keyboard arm', async (t) => {
  const { page, el, state } = await withQueue(t);
  key(page.window, el, 'Enter');
  pointer(page.window, el, 'pointerdown');
  assert.ok(!el.classList.contains('armed'), 'the two paths do not stack');
  assert.ok(el.classList.contains('holding'));
  pointer(page.window, el, 'pointerup');
  await sleep(HOLD_MS + 200);
  assert.equal(state(), QUEUED);
});
