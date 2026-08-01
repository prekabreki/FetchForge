// jsdom harness for the single-file frontend.
//
// It loads the REAL fetchforge/index.html and lets its own <script> run, so the
// tests drive the app's actual functions against the app's actual DOM. Nothing
// here re-implements or copy-pastes page logic.
//
// Why not extract the <script> and eval it: `let`/`const` at the top of a direct
// eval are scoped to the eval, so extracted functions close over their own copy
// of `dlQueue` / `_queueProgress` / `currentTuneMode` and a harness that assigns
// those names is talking to a different binding than the app is. That trap burnt
// a debugging cycle in PR #38 and is recorded in .memories/local-testing-gotchas.md.
// The inverse — a harness that pokes globals and looks green while testing its own
// state — is the failure this file exists to avoid. So: no global poking. Every
// test reaches the app through a real event, a real DOM value, or a stubbed
// network response, and observes the result in the DOM or on the wire.

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { JSDOM } from 'jsdom';

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const REPO_ROOT = path.resolve(HERE, '..', '..');

// mutation-check.mjs points this at a deliberately broken copy to prove the
// suite actually catches regressions.
export const INDEX_HTML = process.env.FETCHFORGE_INDEX_HTML
  ? path.resolve(process.env.FETCHFORGE_INDEX_HTML)
  : path.join(REPO_ROOT, 'fetchforge', 'index.html');

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Let queued microtasks/awaits settle. Two macrotask hops is enough for the
// promise chains the page uses (fetch -> json -> render).
export const flush = async (n = 3) => {
  for (let i = 0; i < n; i++) await new Promise((r) => setImmediate(r));
};

// ---------------------------------------------------------------------------
// SSE stream double: what /download and /convert-local return.
// ---------------------------------------------------------------------------
class SseStream {
  constructor(request) {
    this.request = request;
    this._queue = [];
    this._waiters = [];
    this._ended = false;
  }

  _push(chunk) {
    const w = this._waiters.shift();
    if (w) w(chunk);
    else this._queue.push(chunk);
  }

  /** Emit one SSE event exactly as server.py's _sse() frames it. */
  send(obj) {
    const text = 'data: ' + JSON.stringify(obj) + '\n\n';
    this._push({ done: false, value: new TextEncoder().encode(text) });
  }

  /** End the response, which is how runOneItem/runBatch decide an item is done. */
  close() {
    if (this._ended) return;
    this._ended = true;
    this._push({ done: true, value: undefined });
  }

  _reader() {
    return {
      read: () => {
        if (this._queue.length) return Promise.resolve(this._queue.shift());
        if (this._ended) return Promise.resolve({ done: true, value: undefined });
        return new Promise((r) => this._waiters.push(r));
      },
      cancel: () => Promise.resolve(),
    };
  }

  _response() {
    return {
      ok: true,
      status: 200,
      body: { getReader: () => this._reader() },
      json: async () => ({}),
      text: async () => '',
    };
  }
}

// ---------------------------------------------------------------------------
// fetch stub. Unrouted requests get a never-resolving promise (the recipe in
// .memories/local-testing-gotchas.md) so page-load fetches neither hit the
// network nor blow up on response shape.
// ---------------------------------------------------------------------------
class FetchStub {
  constructor() {
    this.calls = [];       // every request, in order
    this.streams = [];     // SSE streams handed out, in order
    this._routes = [];
    this._streamWaiters = [];
  }

  /** Respond to any request whose URL contains `pattern` with JSON `data`.
   *  Re-registering the same pattern replaces the previous response. */
  json(pattern, data, { ok = true, status = 200 } = {}) {
    this._routes = this._routes.filter((r) => r.pattern !== pattern);
    this._routes.push({
      pattern,
      handler: () => ({ ok, status, json: async () => data, text: async () => JSON.stringify(data) }),
    });
    return this;
  }

  /** Respond to `pattern` with a test-driven SSE stream (see nextStream). */
  sse(pattern) {
    this._routes.push({ pattern, stream: true });
    return this;
  }

  /** Resolve with the Nth SSE stream opened by the page (0-based), waiting if needed.
   *  Rejects rather than hanging if the page never opens it. */
  nextStream(index = this.streams.length, timeoutMs = 5000) {
    if (this.streams[index]) return Promise.resolve(this.streams[index]);
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        reject(new Error(
          `the page never opened SSE stream #${index} within ${timeoutMs}ms ` +
          `(requests so far: ${this.calls.map((c) => c.method + ' ' + c.url).join(', ') || 'none'})`,
        ));
      }, timeoutMs);
      this._streamWaiters.push({
        index,
        resolve: (s) => { clearTimeout(timer); resolve(s); },
      });
    });
  }

  /** Requests to `pattern`, newest last, with FormData bodies decoded to objects. */
  callsTo(pattern) {
    return this.calls.filter((c) => c.url.includes(pattern));
  }

  _install(window) {
    const self = this;
    window.fetch = function (input, init = {}) {
      const url = String(input);
      const call = { url, method: (init.method || 'GET').toUpperCase(), form: decodeForm(init.body) };
      self.calls.push(call);

      const route = self._routes.find((r) => url.includes(r.pattern));
      if (!route) return new Promise(() => {});   // never resolves, on purpose

      if (route.stream) {
        const stream = new SseStream(call);
        const idx = self.streams.push(stream) - 1;
        self._streamWaiters
          .filter((w) => w.index === idx)
          .forEach((w) => w.resolve(stream));
        self._streamWaiters = self._streamWaiters.filter((w) => w.index !== idx);
        return Promise.resolve(stream._response());
      }
      return Promise.resolve(route.handler(call));
    };
  }
}

function decodeForm(body) {
  if (!body || typeof body.forEach !== 'function') return null;
  const out = {};
  body.forEach((v, k) => { out[k] = typeof v === 'string' ? v : '[blob]'; });
  return out;
}

// ---------------------------------------------------------------------------
// Page loader.
// ---------------------------------------------------------------------------

/**
 * Load fetchforge/index.html under jsdom with its <script> executing for real.
 * Returns { window, document, fetchStub, $, $$, close } — call close() in a
 * finally/after hook or the heartbeat interval and rAF log pump keep node alive.
 */
export async function loadPage({ routes } = {}) {
  const html = fs.readFileSync(INDEX_HTML, 'utf8')
    // The server substitutes this at serve time; leaving the raw placeholder in
    // would only matter for /shutdown-now, but keep the page honest anyway.
    .replace('__DLPR_TOKEN__', 'test-token');

  const fetchStub = new FetchStub();
  if (routes) routes(fetchStub);

  const dom = new JSDOM(html, {
    url: 'http://localhost:8765/',
    runScripts: 'dangerously',
    pretendToBeVisual: true,   // requestAnimationFrame — _flushLog and _fadeInOnLoad need it
    beforeParse(window) {
      // jsdom has no canvas 2d context and SparkChart constructs two at load.
      // A callable, self-returning Proxy absorbs any ctx.foo(...) / ctx.bar = x.
      const ctx = new Proxy(function () {}, {
        get: (t, p) => (p === Symbol.toPrimitive ? () => '' : ctx),
        set: () => true,
        apply: () => ctx,
      });
      window.HTMLCanvasElement.prototype.getContext = () => ctx;

      // Encoding APIs the SSE reader uses; jsdom does not expose them on window.
      if (!window.TextDecoder) window.TextDecoder = TextDecoder;
      if (!window.TextEncoder) window.TextEncoder = TextEncoder;

      fetchStub._install(window);
    },
  });

  const { window } = dom;
  await flush();   // let the load-time fetches settle into their (pending) state

  return {
    dom,
    window,
    document: window.document,
    fetchStub,
    $: (sel) => window.document.querySelector(sel),
    $$: (sel) => Array.from(window.document.querySelectorAll(sel)),
    /** Read a style property the page set inline (display toggles, bar widths). */
    styleOf: (sel, prop) => window.document.querySelector(sel).style[prop],
    close: () => window.close(),
  };
}

// ---------------------------------------------------------------------------
// Event helpers. Real constructed events, dispatched at real elements — jsdom
// has no PointerEvent, but the handlers only read `.button`, which MouseEvent
// carries, and listeners dispatch purely by type string.
// ---------------------------------------------------------------------------
export function pointer(window, el, type, { button = 0 } = {}) {
  el.dispatchEvent(new window.MouseEvent(type, { bubbles: true, cancelable: true, button }));
}

export function key(window, el, k, { repeat = false } = {}) {
  el.dispatchEvent(new window.KeyboardEvent('keydown', {
    key: k, bubbles: true, cancelable: true, repeat,
  }));
}

export function blur(window, el) {
  el.dispatchEvent(new window.FocusEvent('blur', { bubbles: false }));
}

// ---------------------------------------------------------------------------
// Fixtures for /video-info.
// ---------------------------------------------------------------------------
export const VIDEO_FORMATS = [
  { id: '299', label: '1080p60 mp4', filesize: 500 * 1024 * 1024, vcodec: 'avc1' },
];
export const AUDIO_FORMATS = [
  { id: '140', label: 'm4a 128k', filesize: 5 * 1024 * 1024, abr: 128 },
];

export function singleVideoInfo(title = 'A Single Video') {
  return {
    title, uploader: 'Chan', duration: 300, is_playlist: false,
    video_formats: VIDEO_FORMATS, audio_formats: AUDIO_FORMATS,
  };
}

export function playlistInfo(entries) {
  return {
    title: 'A Playlist', uploader: 'Chan', is_playlist: true, count: entries.length,
    video_formats: VIDEO_FORMATS, audio_formats: AUDIO_FORMATS,
    entries: entries.map((e, i) => ({ url: e.url, title: e.title, duration: 100 + i, index: i + 1 })),
  };
}

/** Drive the real fetchInfo() for `url`, with `info` served from /video-info. */
export async function fetchInfoFor(page, url, info) {
  page.fetchStub.json('/video-info', info);
  page.$('#url-input').value = url;
  await page.window.fetchInfo();
  await flush();
}

/**
 * Put N single videos on the job queue the way a user does: fetch info for each
 * URL, then press Add to Queue. Never touches `dlQueue` (a script-scope `let`).
 */
export async function queueVideos(page, urls) {
  for (const [i, url] of urls.entries()) {
    await fetchInfoFor(page, url, singleVideoInfo('Video ' + (i + 1)));
    page.window.addToQueue();
    await flush();
  }
}
