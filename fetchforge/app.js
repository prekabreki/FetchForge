const API = location.origin;   // the server may fall back off 8765 when that port is taken

let currentCQ = '0';
let currentMaxrate = '';
let currentTuneMode = 'uhq';
let currentMode = 'video';        // 'video' (H.265) or 'audio' (WAV/MP3)
let currentAudioPreset = 'wav';   // 'wav' or 'mp3'
let currentVideoDuration = 0;     // duration (secs) of the last /video-info result; 0 for playlists/unknown
let _playlistEntries = [];        // last playlist's per-video entries ({url,title,duration,index,_checked}); empty for single videos (#15)
let _lastAudioFormats = [];       // last fetched audio format list, for auto-preset
let videoEncodeStart = null;
// Smart encode-ETA state (EMA of progress rate). Reset per video.
const ETA_TAU = 25;         // seconds — smoothing time constant ("rock-steady")
const ETA_WARMUP_PCT = 5;   // show "Estimating…" until this %
let etaEmaRate = null;      // EMA of pct-per-second
let etaLastPct = 0;
let etaLastT = 0;           // Date.now() ms of last progress tick (0 = none yet)

// ---- CHARTS ----
class SparkChart {
  constructor(canvasId, color, unit) {
    this.canvas = document.getElementById(canvasId);
    this.ctx = this.canvas.getContext('2d');
    this.color = color;
    this.unit = unit;
    this.data = [];
    this.markers = [];
    this.N = 120;
    this.peak = 0;
    this.current = 0;
  }
  _sync() {
    const w = this.canvas.offsetWidth, h = this.canvas.offsetHeight;
    if (w > 0 && h > 0 && (this.canvas.width !== w || this.canvas.height !== h)) {
      this.canvas.width = w; this.canvas.height = h;
    }
    return this.canvas.width > 0 && this.canvas.height > 0;
  }
  push(val) {
    if (val == null || !isFinite(val) || val < 0) return;
    this.data.push(val);
    if (this.data.length > this.N) this.data.shift();
    this.current = val;
    if (val > this.peak) this.peak = val;
    this.draw();
  }
  addMarker(label, color) {
    this.markers.push({ index: this.data.length, label: label, color: color });
  }
  reset() {
    this.data = []; this.markers = []; this.peak = 0; this.current = 0;
    if (this._sync()) this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
  }
  draw() {
    if (!this._sync()) return;
    const ctx = this.ctx, W = this.canvas.width, H = this.canvas.height;
    const PT = 6, PB = 20, PL = 6, PR = 6;
    const plotW = W - PL - PR, plotH = H - PT - PB;
    ctx.clearRect(0, 0, W, H);

    // Gridlines
    ctx.setLineDash([2, 4]);
    ctx.strokeStyle = 'rgba(154,144,176,0.12)';
    ctx.lineWidth = 1;
    for (const frac of [0.25, 0.5, 0.75]) {
      const gy = PT + plotH * (1 - frac);
      ctx.beginPath(); ctx.moveTo(PL, gy); ctx.lineTo(PL + plotW, gy); ctx.stroke();
    }
    ctx.setLineDash([]);

    if (this.data.length < 2) {
      ctx.setLineDash([3, 3]);
      ctx.strokeStyle = 'rgba(154,144,176,0.3)';
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(PL, PT + plotH); ctx.lineTo(PL + plotW, PT + plotH); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = 'rgba(154,144,176,0.5)';
      ctx.font = '11px monospace';
      ctx.textAlign = 'center';
      ctx.fillText('—', W / 2, H - 5);
      return;
    }

    const maxVal = Math.max(...this.data, 0.001);
    const step = plotW / (this.N - 1);
    const xOff = (this.N - this.data.length) * step;
    const xOf = i => PL + xOff + i * step;
    const yOf = v => PT + plotH - (v / maxVal) * plotH;

    const r = parseInt(this.color.slice(1,3),16);
    const g = parseInt(this.color.slice(3,5),16);
    const b = parseInt(this.color.slice(5,7),16);
    const grad = ctx.createLinearGradient(0, PT, 0, PT + plotH);
    grad.addColorStop(0, `rgba(${r},${g},${b},0.35)`);
    grad.addColorStop(1, `rgba(${r},${g},${b},0.02)`);

    ctx.beginPath();
    this.data.forEach((v, i) => { const x = xOf(i), y = yOf(v); i === 0 ? ctx.moveTo(x,y) : ctx.lineTo(x,y); });
    ctx.lineTo(xOf(this.data.length-1), PT+plotH);
    ctx.lineTo(xOf(0), PT+plotH);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();

    ctx.beginPath();
    this.data.forEach((v, i) => { const x = xOf(i), y = yOf(v); i === 0 ? ctx.moveTo(x,y) : ctx.lineTo(x,y); });
    ctx.strokeStyle = this.color;
    ctx.lineWidth = 1.5;
    ctx.lineJoin = 'round';
    ctx.stroke();

    // Phase markers
    this.markers.forEach(m => {
      if (m.index >= this.data.length) return;
      const mx = xOf(m.index);
      ctx.strokeStyle = m.color || 'rgba(124,92,252,0.4)';
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 3]);
      ctx.beginPath(); ctx.moveTo(mx, PT); ctx.lineTo(mx, PT + plotH); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = m.color || 'rgba(124,92,252,0.4)';
      ctx.font = '9px monospace';
      ctx.textAlign = 'center';
      ctx.fillText(m.label || '', mx, PT + 8);
    });

    ctx.font = '11px monospace';
    ctx.fillStyle = '#6b6089';
    ctx.textAlign = 'left';
    ctx.fillText(this.current.toFixed(1) + ' ' + this.unit, PL, H - 5);
    ctx.fillStyle = '#9a90b0';
    ctx.textAlign = 'right';
    ctx.fillText('↑ ' + this.peak.toFixed(1), W - PR, H - 5);
  }
}

function parseDownloadSpeed(s) {
  if (!s) return null;
  const m = s.match(/([\d.]+)\s*(G|M|K)iB\/s/i);
  if (!m) return null;
  const v = parseFloat(m[1]), u = m[2].toUpperCase();
  return u === 'G' ? v * 1024 : u === 'K' ? v / 1024 : v;
}
function parseEncodeSpeed(s) {
  if (!s) return null;
  const m = s.match(/^([\d.]+)x$/i);
  return m ? parseFloat(m[1]) : null;
}

const chartDl  = new SparkChart('chart-dl',  '#4ecdc4', 'MiB/s');
const chartEnc = new SparkChart('chart-enc', '#7c5cfc', '×');

// ---- PHASE STATE (F1, F4) ----
let _currentPhase = 'idle';
let _lastPhaseText = '';

function setPhaseState(phase) {
  const changed = _currentPhase !== phase;
  _currentPhase = phase;

  // F1: indicator dot
  document.getElementById('phase-dot').className = 'phase-dot ' + phase;

  // F1: label color
  const colors = { idle: 'var(--muted)', downloading: 'var(--accent3)', converting: 'var(--accent)', done: '#68d391', error: 'var(--accent2)' };
  document.getElementById('phase-label').style.color = colors[phase] || 'var(--accent)';

  // F4: bar gradient
  const bar = document.getElementById('bar-fill');
  bar.classList.remove('phase-downloading', 'phase-converting', 'phase-done', 'phase-error');
  if (phase !== 'idle') bar.classList.add('phase-' + phase);

  return changed;
}

// ---- ENCODE BADGES (F2) ----
function showEncodeParams(params) {
  const fields = [
    ['CQ', params.cq],
    ['maxrate', params.maxrate],
    ['preset', params.preset],
    ['tune', params.tune],
    ['pix', params.pix_fmt],
  ];
  const container = document.getElementById('encode-params-pills');
  container.innerHTML = '';
  fields.forEach(function(field) {
    const key = field[0];
    const val = field[1];
    const pill = document.createElement('span');
    pill.className = 'encode-pill';
    const keyEl = document.createElement('span');
    keyEl.className = 'pill-key';
    keyEl.textContent = key;
    pill.appendChild(keyEl);
    pill.appendChild(document.createTextNode(String(val)));
    container.appendChild(pill);
  });
  document.getElementById('encode-params-row').style.display = '';
}

// ---- CELEBRATION (F5) ----
function celebrate() {
  const container = document.getElementById('celebration-container');
  container.innerHTML = '';
  const colors = ['#68d391', '#4ecdc4', '#f6e05e', '#fc8181', '#b794f4', '#63b3ed'];
  for (let i = 0; i < 24; i++) {
    const p = document.createElement('div');
    p.className = 'confetti-particle';
    p.style.left = (Math.random() * 100).toFixed(1) + '%';
    p.style.bottom = '0';
    p.style.background = colors[Math.floor(Math.random() * colors.length)];
    p.style.animationDelay = (Math.random() * 0.4).toFixed(2) + 's';
    p.style.animationDuration = (1.0 + Math.random() * 0.6).toFixed(2) + 's';
    p.style.setProperty('--drift', (Math.random() * 40 - 20).toFixed(0) + 'px');
    container.appendChild(p);
  }
  const card = container.closest('.card');
  card.classList.add('celebrating');
  const barFill = document.getElementById('bar-fill');
  barFill.classList.add('celebrating');
  setTimeout(function() {
    container.innerHTML = '';
    card.classList.remove('celebrating');
    barFill.classList.remove('celebrating');
  }, 2000);
}

// ---- SIZE TRACKER ----
let _sourceBytes = 0;

function resetSizeTracker() {
  _sourceBytes = 0;
  var el = document.getElementById('size-tracker');
  el.classList.remove('visible');
  document.getElementById('st-fill').style.width = '0%';
  document.getElementById('st-pct').textContent = '—';
  document.getElementById('st-output').textContent = '0 MB';
  document.getElementById('st-source').textContent = '/ — MB';
}

function initSizeTracker(sourceBytes) {
  _sourceBytes = sourceBytes;
  if (_sourceBytes <= 0) return;
  var el = document.getElementById('size-tracker');
  el.classList.add('visible');
  document.getElementById('st-source').textContent = '/ ' + (_sourceBytes / 1024 / 1024).toFixed(0) + ' MB';
  document.getElementById('st-output').textContent = '0 MB';
  document.getElementById('st-fill').style.width = '0%';
  document.getElementById('st-fill').className = 'size-tracker-fill st-fill-green';
  document.getElementById('st-pct').textContent = '0%';
  document.getElementById('st-pct').className = 'size-tracker-pct st-green';
  // Position the 100% marker
  document.getElementById('st-mark').style.left = '50%';
}

function updateSizeTracker(outputBytes) {
  if (_sourceBytes <= 0 || outputBytes <= 0) return;
  var ratio = outputBytes / _sourceBytes;
  var pct = (ratio * 100).toFixed(1);
  var outMB = (outputBytes / 1024 / 1024).toFixed(0);

  // Color tiers
  var tier;
  if (ratio < 0.85)      tier = 'green';
  else if (ratio < 1.05)  tier = 'teal';
  else if (ratio < 1.30)  tier = 'orange';
  else                     tier = 'red';

  document.getElementById('st-output').textContent = outMB + ' MB';
  document.getElementById('st-pct').textContent = pct + '%';
  document.getElementById('st-pct').className = 'size-tracker-pct st-' + tier;

  // Bar: 100% of source = 50% of bar width, so output can visually exceed source
  var barPct = Math.min(ratio * 50, 100);
  var fill = document.getElementById('st-fill');
  fill.style.width = barPct.toFixed(1) + '%';
  fill.className = 'size-tracker-fill st-fill-' + tier;
}

// ---- ELAPSED TIMER (F7) ----
let _elapsedInterval = null;
function startElapsedTimer() {
  stopElapsedTimer();
  const el = document.getElementById('stat-elapsed');
  _elapsedInterval = setInterval(function() {
    if (!videoEncodeStart) return;
    const secs = Math.floor((Date.now() - videoEncodeStart) / 1000);
    const h = Math.floor(secs / 3600);
    const m = Math.floor((secs % 3600) / 60);
    const s = secs % 60;
    el.textContent = [h, m, s].map(function(x) { return String(x).padStart(2, '0'); }).join(':');
  }, 1000);
}
function stopElapsedTimer() {
  if (_elapsedInterval) { clearInterval(_elapsedInterval); _elapsedInterval = null; }
}

// ---- WAKE LOCK ----
// Prevents Chrome from freezing this tab during long overnight jobs.
let _wakeLock = null;

async function acquireWakeLock() {
  if (!('wakeLock' in navigator)) return;
  try {
    _wakeLock = await navigator.wakeLock.request('screen');
    _wakeLock.addEventListener('release', () => { _wakeLock = null; });
  } catch {}
}

function releaseWakeLock() {
  if (_wakeLock) { _wakeLock.release(); _wakeLock = null; }
}

// Re-acquire on tab re-focus (wake locks are released when tab loses visibility)
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && _wakeLock === null && _jobRunning) {
    acquireWakeLock();
  }
});

// Warn before closing tab during a job, or with unsaved pasted cookies (Issue #14).
window.addEventListener('beforeunload', (e) => {
  if (_jobRunning || (typeof _pendingCookies === 'string' && _pendingCookies.trim())) {
    e.preventDefault();
    // Modern browsers show a generic prompt; the returnValue must be set.
    e.returnValue = 'You have unsaved pasted cookies. Save before reloading?';
    return e.returnValue;
  }
});

let _jobRunning = false;

// ---- CANCEL STATE (Issue #5) ----
let cancelled = false;            // set by cancelJob(); halts the whole run, no retries
let _currentController = null;    // AbortController for the in-flight SSE fetch
let _lastErrorMsg = '';           // last 'error' SSE msg for the current item

function fmtEta(secs) {
  if (!isFinite(secs) || secs <= 0) return '—';
  secs = Math.round(secs);
  if (secs < 60) return secs + 's';
  return Math.floor(secs / 60) + 'm ' + (secs % 60) + 's';
}

function autoSelectPreset() {
  // Video: Smart Auto is always the right default — server analyses the source.
  // Audio: pick WAV vs MP3 based on best available source bitrate.
  const card = document.querySelector('#video-presets .preset-card[data-preset="auto"]');
  if (card) {
    document.querySelectorAll('#video-presets .preset-card').forEach(c => c.classList.remove('selected'));
    card.classList.add('selected');
    currentCQ = '0';
    currentMaxrate = '';
    currentTuneMode = 'uhq';
    setSelectVal('cq-select', '0');
    setSelectVal('maxrate', '');
  }
  // Unconditional, and outside the card guard: a stale encoder-speed / tune /
  // pixel-format override left behind by a previous video is invisible in the
  // UI but changes every encode that follows it (#51).
  resetEncodeOverridesToAuto();
  autoSelectAudioPreset();
}

function autoSelectAudioPreset() {
  // ≥160 kbps source warrants lossless WAV; below that, MP3 320 is the better default.
  const bestAbr = (_lastAudioFormats || []).reduce((m, f) => Math.max(m, f.abr || 0), 0);
  const target = bestAbr >= 160 ? 'wav' : 'mp3';
  const card = document.querySelector(`#audio-presets [data-audio-preset="${target}"]`);
  if (!card) return;
  document.querySelectorAll('#audio-presets .preset-card').forEach(c => c.classList.remove('selected'));
  card.classList.add('selected');
  currentAudioPreset = target;
}

function selectPreset(el, cq, maxrate, tuneMode) {
  document.querySelectorAll('#video-presets .preset-card').forEach(c => c.classList.remove('selected'));
  el.classList.add('selected');
  currentCQ = cq;
  currentMaxrate = maxrate;
  currentTuneMode = tuneMode || 'uhq';
  setSelectVal('cq-select', cq);
  setSelectVal('maxrate', maxrate);
  // Picking a preset card is the user saying "use the card's settings". Leaving
  // one of the other three on a manual value would encode against the card the
  // UI is now showing as selected (#51).
  resetEncodeOverridesToAuto();
}

// ---- ADVANCED ENCODE OVERRIDES (#51) ----
// The three selects below map 1:1 onto the `preset` / `tune` / `pix_fmt` form
// fields #50 added. "" means auto everywhere — client, wire and server — so a
// panel left alone sends exactly what it sent before this existed.
// The server is the gate: validate_encode_overrides() coerces, clamps and
// falls back. Nothing here re-implements that; the hints restate its rules and
// the option lists offer only values it accepts.
const ADV_OVERRIDE_SELECTS = ['enc-speed-select', 'tune-select', 'pixfmt-select'];

function resetEncodeOverridesToAuto() {
  ADV_OVERRIDE_SELECTS.forEach(id => setSelectVal(id, ''));
  updateHdrPixFmtWarning();
}

// The "Reset to auto" control: all five, one action.
function resetAdvancedToAuto() {
  setSelectVal('cq-select', '0');
  setSelectVal('maxrate', '');
  resetEncodeOverridesToAuto();
}

// One place builds the three fields for all three request shapes (single
// download, batch download, local convert), so they cannot drift apart.
function appendEncodeOverrides(form, opts) {
  form.append('preset', opts.preset || '');
  form.append('tune', opts.tune || '');
  form.append('pix_fmt', opts.pixFmt || '');
}

// HDR is read off the *currently selected* video format, and the option list is
// rebuilt by every resolve, so this can never report a previous video's source.
function isHdrFormat(f) {
  const note = ((f.note || '') + ' ' + (f.label || '')).toLowerCase();
  const vcodec = (f.vcodec || '').toLowerCase();
  // yt-dlp labels HDR formats in format_note ("2160p60 HDR"); vp9 profile 2 and
  // 10-bit AV1 are the HDR-capable YouTube codecs.
  return /\bhdr\b|hlg|pq\b/.test(note)
      || vcodec.startsWith('vp9.2')
      || /^av01\.[^.]+\.[^.]+\.10/.test(vcodec);
}

function selectedSourceIsHdr() {
  if (currentMode === 'audio') return false;
  const sel = document.getElementById('video-format');
  const opt = sel && sel.selectedOptions[0];
  return !!(opt && opt.dataset.hdr === '1');
}

// Forcing 8-bit out of an HDR source stays permitted — it is a real choice —
// but HDR metadata is passthrough only, so the result will look wrong.
function updateHdrPixFmtWarning() {
  const warn = document.getElementById('pixfmt-hdr-warn');
  const sel = document.getElementById('pixfmt-select');
  if (!warn || !sel) return;
  warn.hidden = !(sel.value === 'yuv420p' && selectedSourceIsHdr());
}

// The capability report is built once at startup and served from a cache, so
// the two consumers below (the header warning banner and the tune control)
// share a single in-flight read rather than each fetching it on load. A
// rejected read stays rejected on purpose: both callers degrade to "leave it
// alone", which is the right answer for a diagnostic that could not be taken.
let _capabilitiesPromise = null;
function getCapabilities() {
  if (!_capabilitiesPromise) {
    _capabilitiesPromise = fetch(`${API}/capabilities`).then(r => r.json());
  }
  return _capabilitiesPromise;
}

// uhq availability comes from the startup NVENC probe via GET /capabilities
// (#47) — never re-probed, never hardcoded. When the probe says no, the option
// is disabled with the server's own remedy shown, rather than being silently
// accepted and downgraded during the encode.
async function applyTuneCapability() {
  const sel = document.getElementById('tune-select');
  const opt = sel && sel.querySelector('option[value="uhq"]');
  const why = document.getElementById('tune-unsupported');
  if (!opt || !why) return;
  let d;
  try {
    d = await getCapabilities();
  } catch {
    return;   // a capability read must never disable a control it could not check
  }
  const cap = (d.capabilities || []).find(c => c.key === 'nvenc_uhq_tune');
  if (!cap || cap.ok) return;
  opt.disabled = true;
  opt.textContent = 'uhq — unavailable on this ffmpeg';
  if (sel.value === 'uhq') sel.value = '';
  why.textContent = 'uhq unavailable: ' + (cap.remedy || cap.impact || 'the startup NVENC probe rejected it.');
  why.hidden = false;
}

applyTuneCapability();
// Switching to an HDR format with 8-bit already forced has to raise the warning
// too, so it tracks the format selection, not just the pixel-format selection.
document.getElementById('video-format')?.addEventListener('change', updateHdrPixFmtWarning);

function selectAudioPreset(el, preset) {
  document.querySelectorAll('#audio-presets .preset-card').forEach(c => c.classList.remove('selected'));
  el.classList.add('selected');
  currentAudioPreset = preset;
}

function selectMode(mode) {
  currentMode = mode;
  document.querySelectorAll('.preset-card[data-mode]').forEach(c => {
    c.classList.toggle('selected', c.dataset.mode === mode);
  });
  const isAudio = mode === 'audio';
  document.getElementById('video-presets').style.display = isAudio ? 'none' : '';
  document.getElementById('audio-presets').style.display = isAudio ? '' : 'none';
  document.getElementById('video-only-options').style.display = isAudio ? 'none' : '';
  document.getElementById('convert-toggle-row').style.display = isAudio ? 'none' : '';
  const vfField = document.getElementById('video-format-field');
  if (vfField) vfField.style.display = isAudio ? 'none' : '';
}

async function browseOutputDir() {
  try {
    const r = await fetch(`${API}/browse-folder`);
    const data = await r.json();
    if (data.path) document.getElementById('output-dir').value = data.path;
  } catch (e) {}
}

function setSelectVal(id, val) {
  const sel = document.getElementById(id);
  for (const opt of sel.options) if (opt.value === val) { sel.value = val; break; }
}

function toggleAdvanced() {
  const sec = document.getElementById('advanced-section');
  sec.classList.toggle('open');
  const open = sec.classList.contains('open');
  // aria-expanded is also what rotates the chevron (see .icon-chevron) — the
  // arrow is no longer a glyph this function swaps, so nothing here writes to
  // the toggle's children.
  document.querySelector('.advanced-toggle').setAttribute('aria-expanded', String(open));
}

function toggleConvert() {
  const on = document.getElementById('convert-toggle').checked;
  document.getElementById('conv-settings').style.display = on ? '' : 'none';
  document.getElementById('download-only-check').checked = !on;
  document.getElementById('delete-cache-check').disabled = !on;
}

function toggleDownloadOnly() {
  const downloadOnly = document.getElementById('download-only-check').checked;
  document.getElementById('convert-toggle').checked = !downloadOnly;
  document.getElementById('conv-settings').style.display = downloadOnly ? 'none' : '';
  document.getElementById('delete-cache-check').disabled = downloadOnly;
}

checkCookies();
fetchVersion();
fetchAppVersion();
fetchCapabilities();
loadHistory();
updateYtdlp();
// Several controls are div/span + onclick rather than <button> (they sit inside
// other clickable rows, where a nested button would be invalid). They carry
// role="button" tabindex="0", so this restores the keyboard activation a real
// button would have given them for free.
document.addEventListener('keydown', function (e) {
  if (e.key !== 'Enter' && e.key !== ' ' && e.key !== 'Spacebar') return;
  const el = e.target;
  if (!el || el.getAttribute('role') !== 'button' || el.tagName === 'BUTTON') return;
  e.preventDefault();
  el.click();
});

// ---- HOLD-TO-CONFIRM ----
// Guards the two clears that cannot be undone from the UI: history (a server
// side DELETE) and the job queue (potentially 50 hand-picked entries). A plain
// click does nothing at all. The pointer path wants HOLD_MS of sustained
// press; pointerup, pointerleave and pointercancel each abort it, and the
// abort has to leave no timer behind, because a timer that survives a release
// would delete something later with nothing on screen to explain it.
//
// A hold has no keyboard analogue, so Enter/Space arms the control instead
// (label flips to "Confirm?") and a second press within KEYARM_MS fires it.
// Escape, blur or the timeout disarm it.
const HOLD_MS = 700;      // must stay in step with the CSS fill duration
const KEYARM_MS = 4000;   // how long a keyboard-armed control stays armed
let _holdEl = null, _holdTimer = null;
let _armEl = null, _armTimer = null;

// Resolved at fire time, so this never depends on declaration order, and only
// these two names are ever reachable from a data-hold-action attribute.
function _holdInvoke(name) {
  const allowed = { clearHistory: clearHistory, clearDlQueue: clearDlQueue };
  if (!Object.prototype.hasOwnProperty.call(allowed, name)) return;   // never reach Object.prototype
  const fn = allowed[name];
  if (typeof fn === 'function') fn();
}

function _holdStop() {
  if (_holdTimer !== null) { clearTimeout(_holdTimer); _holdTimer = null; }
  if (_holdEl) { _holdEl.classList.remove('holding'); _holdEl = null; }
}

function _holdDisarm() {
  if (_armTimer !== null) { clearTimeout(_armTimer); _armTimer = null; }
  if (!_armEl) return;
  const el = _armEl;
  _armEl = null;
  el.classList.remove('armed');
  const lbl = el.querySelector('.hold-confirm-label');
  if (lbl) lbl.innerHTML = lbl.dataset.idleLabel;
  el.setAttribute('aria-label', el.dataset.idleAria);
}

function _holdFire(el) {
  _holdStop();
  _holdDisarm();
  _holdInvoke(el.dataset.holdAction);
}

function _holdStart(el) {
  _holdStop();      // a second pointerdown must replace the timer, never stack one
  _holdDisarm();
  _holdEl = el;
  el.classList.add('holding');
  _holdTimer = setTimeout(function () {
    _holdTimer = null;
    const target = _holdEl;
    _holdStop();
    if (target) _holdFire(target);
  }, HOLD_MS);
}

function _holdArm(el) {
  _holdStop();
  _holdDisarm();
  _armEl = el;
  el.classList.add('armed');
  const lbl = el.querySelector('.hold-confirm-label');
  // Same ✕ mark as the idle label, so arming changes the word and nothing else.
  // The idle label is restored from dataset.idleLabel, which is the innerHTML
  // captured at init and therefore already carries its own <svg><use>.
  if (lbl) lbl.innerHTML = '<svg class="icon" aria-hidden="true"><use href="#i-x"/></svg> Confirm?';
  el.setAttribute('aria-label', 'Press Enter again to confirm: ' + el.dataset.idleAria);
  _armTimer = setTimeout(function () { _armTimer = null; _holdDisarm(); }, KEYARM_MS);
}

function _initHoldConfirm() {
  document.querySelectorAll('.hold-confirm').forEach(function (el) {
    const lbl = el.querySelector('.hold-confirm-label');
    if (lbl) lbl.dataset.idleLabel = lbl.innerHTML;
    el.dataset.idleAria = el.getAttribute('aria-label') || '';

    el.addEventListener('pointerdown', function (e) {
      if (e.button > 0) return;                 // primary button / touch / pen only
      e.stopPropagation();                      // history clear sits inside a clickable row
      _holdStart(el);
    });
    ['pointerup', 'pointerleave', 'pointercancel'].forEach(function (evt) {
      el.addEventListener(evt, function () { if (_holdEl === el) _holdStop(); });
    });
    // The click that follows a press must not reach the surrounding row, and
    // must never be the thing that clears.
    el.addEventListener('click', function (e) { e.preventDefault(); e.stopPropagation(); });

    el.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { if (_armEl === el) { e.stopPropagation(); _holdDisarm(); } return; }
      if (e.key !== 'Enter' && e.key !== ' ' && e.key !== 'Spacebar') return;
      e.preventDefault();
      e.stopPropagation();                      // the generic role="button" bridge must not also fire
      if (e.repeat) return;                     // holding Enter is not a second press
      if (_armEl === el) _holdFire(el); else _holdArm(el);
    });
    el.addEventListener('blur', function () { if (_armEl === el) _holdDisarm(); });
  });
}
_initHoldConfirm();

loadChannelLogo();

// ---- HEARTBEAT ----
// Ping the server while this tab is open. When the tab closes the pings stop
// and the server self-exits after its grace window — so the hidden server
// process doesn't linger after you're done. Ping every 5s vs the server's
// ~30s timeout, so a brief stall or refresh never trips it.
function _heartbeat() { fetch(`${API}/heartbeat`).catch(() => {}); }
_heartbeat();
setInterval(_heartbeat, 5000);

// ---- HISTORY ----
let _historyItems = [];
let _historyOpen = false;

async function loadHistory() {
  try {
    const r = await fetch(`${API}/history`);
    _historyItems = await r.json();
    renderHistory();
  } catch {}
}

function renderHistory() {
  const panel = document.getElementById('history-panel');
  const list = document.getElementById('history-list');
  if (!_historyItems || _historyItems.length === 0) {
    panel.classList.add('hidden');
    _historyOpen = false;
    return;
  }
  panel.classList.remove('hidden');
  document.getElementById('history-toggle-label').textContent = 'Recent (' + _historyItems.length + ')';
  list.innerHTML = _historyItems.map((item, i) => {
    const thumb = item.thumbnail
      ? `<img class="history-thumb" src="${escapeAttr(item.thumbnail)}" alt="">`
      : `<div class="history-thumb-blank"></div>`;
    const sub = (item.is_playlist ? `Playlist \u00b7 ${item.count} videos \u00b7 ` : '') + (item.added ? item.added.replace('T', ' ') : '');
    return `<div class="history-item" onclick="loadFromHistory(${i})" role="button" tabindex="0">${thumb}<div class="history-meta"><div class="history-title">${escapeHtml(item.title || item.url)}</div><div class="history-sub">${sub}</div></div></div>`;
  }).join('');
}

function toggleHistory() {
  _historyOpen = !_historyOpen;
  document.getElementById('history-list').classList.toggle('hidden', !_historyOpen);
  // Chevron rotation rides on aria-expanded (see .icon-chevron).
  document.querySelector('.history-header').setAttribute('aria-expanded', String(_historyOpen));
  document.getElementById('history-toggle-label').textContent = 'Recent (' + _historyItems.length + ')';
}

function loadFromHistory(idx) {
  const item = _historyItems[idx];
  if (!item) return;
  document.getElementById('url-input').value = item.url;
  fetchInfo();
}

async function saveToHistory(info, url) {
  const form = new FormData();
  form.append('url', url);
  form.append('title', info.title || '');
  form.append('thumbnail', info.thumbnail || '');
  form.append('is_playlist', info.is_playlist ? 'true' : 'false');
  form.append('count', String(info.count || 1));
  try {
    await fetch(`${API}/history`, { method: 'POST', body: form });
    await loadHistory();
    // Keep panel open if it was open
    if (_historyOpen) {
      const list = document.getElementById('history-list');
      list.classList.remove('hidden');
    }
  } catch {}
}

// Reached only through the hold-to-confirm gesture (or a second keyboard
// press). The list is emptied locally only after the server says it went, so a
// failed DELETE leaves the panel honest instead of showing nothing while the
// history still exists on disk.
async function clearHistory() {
  const n = _historyItems ? _historyItems.length : 0;
  try {
    const r = await fetch(`${API}/history`, { method: 'DELETE' });
    if (!r.ok) { log(`Could not clear history: server returned ${r.status}.`, 'error'); return; }
    _historyItems = [];
    _historyOpen = false;
    renderHistory();
    log(`Download history cleared (${n} entr${n === 1 ? 'y' : 'ies'}).`, 'info');
  } catch {
    log('Could not clear history: no response from the server.', 'error');
  }
}

// Both <img>s are populated after a fetch and sized by CSS, so there is no
// layout shift to fix — only the snap. `.img-loaded` drives a 200ms opacity
// transition; a cached image is already complete by the time onload could be
// attached, so that case is handled explicitly rather than left to fire never.
function _fadeInOnLoad(img) {
  const reveal = () => {
    // A transition cannot start from a box that was never rendered, and the
    // channel logo is display:none until it loads. Un-hide it, let one frame
    // commit that, and only then flip opacity — otherwise the two changes
    // coalesce and it snaps in exactly as it did before. The thumbnail is
    // already on screen by this point, so it fades straight away.
    const wasUnrendered = img.getClientRects().length === 0;
    img.style.display = '';
    if (!wasUnrendered) { img.classList.add('img-loaded'); return; }
    requestAnimationFrame(() => requestAnimationFrame(() => img.classList.add('img-loaded')));
  };
  // A memory-cached image can already be complete by the time we get here, in
  // which case onload will never fire again — reveal it directly.
  if (img.complete && img.naturalWidth > 0) { reveal(); return; }
  img.onload = reveal;
}

function loadChannelLogo() {
  const img = document.getElementById('channel-logo');
  img.src = 'https://yt3.googleusercontent.com/wgn0tWnbhuvuDVMJZ-LBzOZpL0YwLVhwAVKIeWSwv9IpB91IvStBUQg_HSWg8sLWpqsYp7sa=s176-c-k-c0x00ffffff-no-rj';
  _fadeInOnLoad(img);
}

async function fetchVersion() {
  try {
    const r = await fetch(`${API}/ytdlp-version`);
    const d = await r.json();
    document.getElementById('ytdlp-version-label').textContent = 'yt-dlp: ' + d.version;
  } catch {
    document.getElementById('ytdlp-version-label').textContent = 'yt-dlp: unknown';
  }
}

async function fetchAppVersion() {
  try {
    const r = await fetch(`${API}/version`);
    const d = await r.json();
    document.getElementById('app-version-label').textContent = 'v ' + d.version;
    document.getElementById('app-version-dot').style.background = '';
    document.getElementById('app-version-dot').className = 'dot';
  } catch {
    document.getElementById('app-version-label').textContent = 'v —';
  }
}

// #47 — the three startup capability probes (uhq tune, scale_cuda, libplacebo)
// used to fail in silence, so a stale ffmpeg could cost output quality for months
// unnoticed. Render whatever the server's startup report found, naming the PATH
// (the original confusion was not knowing which ffmpeg was in use), the reported
// version and the remedy. The server decides *whether* to warn — from probe
// results, never from a version comparison — this only draws it.
async function fetchCapabilities() {
  const box = document.getElementById('cap-warning');
  if (!box) return;
  let d;
  try {
    d = await getCapabilities();
  } catch {
    return;   // a diagnostic must never break the page it is diagnosing
  }
  const w = d && d.warning;
  if (!w) { box.hidden = true; return; }
  document.getElementById('cap-warning-head').textContent = w.headline;
  const list = document.getElementById('cap-warning-list');
  list.textContent = '';
  (w.items || []).forEach(it => {
    const li = document.createElement('li');
    const name = document.createElement('strong');
    name.textContent = it.name;
    li.appendChild(name);
    // textContent throughout: a filesystem path is untrusted-shaped text and
    // never goes near innerHTML.
    li.appendChild(document.createTextNode(
      ' — ' + [it.impact, it.remedy].filter(Boolean).join(' ')));
    list.appendChild(li);
  });
  const note = document.getElementById('cap-warning-note');
  note.textContent = w.reassurance || '';
  note.hidden = !w.reassurance;
  document.getElementById('cap-warning-path').textContent = w.ffmpeg_path;
  document.getElementById('cap-warning-version').textContent = w.ffmpeg_version;
  box.hidden = false;
}

async function checkCookies() {
  try {
    const r = await fetch(`${API}/check-cookies`);
    const d = await r.json();
    const badge = document.getElementById('cookie-badge');
    const hdr = document.getElementById('cookie-header');
    const dot = document.getElementById('cookie-dot');
    const clearBtn = document.getElementById('cookie-clear-btn');
    if (clearBtn) clearBtn.style.display = d.exists ? '' : 'none';
    if (d.exists && d.valid) {
      const yt = d.youtube_cookies || 0;
      badge.textContent = `✓ cookies loaded (${d.cookies}, ${yt} YouTube)`;
      badge.className = 'badge badge-ok';
      hdr.textContent = 'cookies: ✓';
      dot.className = 'dot';
    } else if (d.exists) {
      badge.textContent = `⚠ cookies.txt invalid: ${d.reason || 'parse error'}`;
      badge.className = 'badge badge-warn';
      hdr.textContent = 'cookies: invalid';
      dot.className = 'dot warn';
    } else {
      badge.textContent = '✗ No cookies — throttling likely';
      badge.className = 'badge badge-warn';
      hdr.textContent = 'cookies: missing';
      dot.className = 'dot warn';
    }
  } catch(e) {
    document.getElementById('cookie-badge').textContent = 'Server not reachable';
  }
}

async function uploadCookies(input) {
  const file = input.files[0];
  if (!file) return;
  const form = new FormData();
  form.append('file', file);
  await fetch(`${API}/upload-cookies`, { method: 'POST', body: form });
  checkCookies();
  log('Cookies uploaded.', 'info');
}

async function clearCookies() {
  try {
    await fetch(`${API}/cookies`, { method: 'DELETE' });
    log('Cookies cleared — downloads will run without authentication.', 'info');
  } catch(e) {
    log('Failed to clear cookies: ' + e, 'error');
  }
  checkCookies();
}

// #12: ask the server to read cookies straight from an installed browser.
// With no args, it scans every browser+profile and picks the richest YouTube
// source; called with (browser, profile) it switches to a specific one.
async function attemptFetchCookies(browser, profile) {
  const btn = document.getElementById('cookie-fetch-btn');
  const box = document.getElementById('cookie-fetch-result');
  const targeted = !!browser;
  btn.disabled = true;
  const prevLabel = btn.textContent;
  btn.textContent = targeted ? 'Switching…' : 'Scanning…';
  box.style.display = 'block';
  box.innerHTML = targeted
    ? `Reading ${escapeHtml(browser)}${profile ? ' / ' + escapeHtml(profile) : ''}…`
    : 'Scanning installed browsers and profiles…';
  try {
    const form = new FormData();
    if (browser) form.append('browser', browser);
    if (profile) form.append('profile', profile);
    const r = await fetch(`${API}/fetch-cookies`, { method: 'POST', body: form });
    const d = await r.json();
    renderCookieFetchResult(d);
    if (d.selected) {
      log(`Loaded ${d.selected.youtube_cookies} YouTube cookie(s) from ${d.selected.browser} / ${d.selected.display_name}.`, 'info');
    } else if (d.error) {
      log('Fetch cookies: ' + d.error, 'error');
    } else if ((d.candidates || []).some(c => c.error)) {
      // #43: a scan where every source errored is a failure, not "logged out".
      const failed = (d.candidates || []).filter(c => c.error);
      log(`Could not read cookies from ${failed.length} of ${d.candidates.length} browser profile(s) — see card #01 for the reasons.`, 'error');
    } else {
      log('No YouTube cookies found in any installed browser.', 'error');
    }
    checkCookies();
  } catch(e) {
    box.innerHTML = `<span style="color:var(--accent2);">Fetch failed: ${escapeHtml(String(e))}</span>`;
    log('Attempt to fetch cookies failed: ' + e, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = prevLabel;
  }
}

let _cookieCandidates = [];   // last scan's candidates, referenced by index for "use this"

// Server-side marker for Chromium App-Bound Encryption (server.py APP_BOUND_ENCRYPTION, #46).
const ABE_REASON = 'app_bound_encryption';

// Error strings come from exceptions — escape, then clip so one long reason
// cannot blow out the row.
function cookieErrorText(msg) {
  const s = String(msg || '');
  return escapeHtml(s.length > 120 ? s.slice(0, 120).trimEnd() + '…' : s);
}

function renderCookieFetchResult(d) {
  const box = document.getElementById('cookie-fetch-result');
  _cookieCandidates = d.candidates || [];
  const errored = _cookieCandidates.filter(c => c.error);
  const abe = _cookieCandidates.filter(c => c.reason === ABE_REASON);
  const rows = [];
  if (d.selected) {
    rows.push(`<div style="color:var(--accent);margin-bottom:6px;">✓ Using <strong>${escapeHtml(d.selected.browser)} / ${escapeHtml(d.selected.display_name)}</strong> — ${d.selected.youtube_cookies} YouTube cookie(s).</div>`);
  } else if (d.error) {
    rows.push(`<div style="color:var(--accent2);margin-bottom:6px;">${escapeHtml(d.error)}</div>`);
  } else if (errored.length) {
    // #43: the scan itself failed — do not diagnose that as "you are logged out".
    const total = _cookieCandidates.length;
    const which = errored.length < total
      ? `${errored.length} of the ${total} scanned sources`
      : (total === 1 ? 'the only browser profile found' : `any of the ${total} scanned sources`);
    rows.push(`<div style="color:var(--accent2);margin-bottom:6px;">Could not read cookies from ${which}. ${errored.length === 1 ? 'Reason' : 'Reasons'} below.</div>`);
  } else if (_cookieCandidates.length) {
    rows.push(`<div style="color:var(--accent2);margin-bottom:6px;">No YouTube cookies found. Log in to YouTube in a browser, then try again.</div>`);
  } else {
    rows.push(`<div style="color:var(--accent2);margin-bottom:6px;">No supported browser profiles were found to scan.</div>`);
  }
  if (_cookieCandidates.length) {
    rows.push('<div style="color:var(--muted);margin:4px 0 2px;">Sources scanned:</div>');
    // #43: list every candidate, errored ones included, each with its reason.
    _cookieCandidates.forEach((c, i) => {
      const isSel = d.selected && c.browser === d.selected.browser && (c.profile_dir || null) === (d.selected.profile_dir || null);
      const canSwitch = !isSel && !c.error && c.youtube_cookies > 0;
      const action = canSwitch
        ? ` <span style="color:var(--accent);cursor:pointer;text-decoration:underline;" onclick="useCookieSource(${i})" role="button" tabindex="0">use this</span>`
        : (isSel ? ' <span style="color:var(--accent);">✓</span>' : '');
      const bits = [];
      if (!c.error || c.youtube_cookies > 0) bits.push(`${c.youtube_cookies} YT`);
      if (c.reason === ABE_REASON) {
        bits.push('<span style="color:var(--accent2);">blocked by browser encryption</span>');
      } else if (c.error) {
        bits.push(`<span style="color:var(--accent2);">failed:</span> <span style="color:var(--muted);">${cookieErrorText(c.error)}</span>`);
      }
      rows.push(`<div style="padding:2px 0;">• ${escapeHtml(c.browser)} / ${escapeHtml(c.display_name)} — ${bits.join(' — ')}${action}</div>`);
    });
  }
  // #46: one summary for App-Bound Encryption, never repeated per profile.
  if (abe.length || d.reason === ABE_REASON) {
    const names = [...new Set(abe.map(c => String(c.browser || '')))].filter(Boolean)
      .map(b => b.charAt(0).toUpperCase() + b.slice(1));
    const who = names.length ? escapeHtml(names.join(', ')) : 'This browser';
    rows.push(`<div style="margin-top:8px;padding:8px 10px;border:1px solid var(--border2);border-radius:var(--radius-sm);background:var(--surface2);">
      <div style="color:var(--accent2);font-weight:600;margin-bottom:4px;">${who} encrypt${names.length > 1 ? '' : 's'} cookies so only the browser can read them</div>
      <div style="color:var(--text2);">Chromium 127+ on Windows locks the cookie key to the browser itself (App-Bound Encryption). No outside program can unwrap it, so FetchForge cannot read these cookies — closing the browser or running as administrator will not help.</div>
      <div style="color:var(--text2);margin-top:4px;">Export them from inside the browser instead: install <strong>Get cookies.txt LOCALLY</strong>, export youtube.com, then use <strong>Load cookies.txt</strong> or the <strong>paste cookies</strong> box in this card.</div>
    </div>`);
  }
  rows.push('<div style="color:var(--muted);margin-top:6px;font-size:11px;">Tip: cookies from a running browser can be rotated by YouTube — for durable auth, log in via a fresh private window and close it before fetching.</div>');
  box.innerHTML = rows.join('');
}

function useCookieSource(i) {
  const c = _cookieCandidates[i];
  if (!c) return;
  attemptFetchCookies(c.browser, c.profile_dir || '');
}

let _pendingCookies = '';

function handleCookiePaste(e) {
  e.preventDefault();
  const text = (e.clipboardData || window.clipboardData).getData('text') || '';
  _pendingCookies = text;
  const lines = text.split('\n').filter(l => l.trim() && !l.trim().startsWith('#')).length;
  const ta = document.getElementById('cookie-paste');
  // Unambiguous captured state: green border + checkmark + bold text.
  ta.value = `✓ Captured ${lines} cookies — click Save and Verify to commit`;
  ta.classList.add('captured');
  const status = document.getElementById('cookie-paste-status');
  status.textContent = `✓ ${lines} captured — ready to save`;
  status.className = 'badge badge-ok';
  status.style.display = '';
}

function clearPastedCookies() {
  _pendingCookies = '';
  const ta = document.getElementById('cookie-paste');
  ta.value = '';
  ta.classList.remove('captured');
  const status = document.getElementById('cookie-paste-status');
  status.style.display = 'none';
}

async function savePastedCookies() {
  const ta = document.getElementById('cookie-paste');
  const status = document.getElementById('cookie-paste-status');
  const text = _pendingCookies;
  if (!text.trim()) {
    status.textContent = '⚠ paste cookies first';
    status.className = 'badge badge-warn';
    status.style.display = '';
    return;
  }
  status.textContent = 'Saving…';
  status.className = 'badge';
  status.style.display = '';
  try {
    const form = new FormData();
    form.append('text', text);
    const r = await fetch(`${API}/paste-cookies`, { method: 'POST', body: form });
    const d = await r.json();
    if (d.valid) {
      const yt = d.youtube_cookies || 0;
      status.textContent = `✓ valid — ${d.cookies} cookies (${yt} YouTube)`;
      status.className = 'badge badge-ok';
      _pendingCookies = '';
      ta.value = '';
      ta.classList.remove('captured');
      log(`Cookies pasted: ${d.cookies} entries, ${yt} YouTube.`, 'info');
      checkCookies();
    } else {
      status.textContent = `✗ ${d.reason || 'invalid'}`;
      status.className = 'badge badge-warn';
    }
  } catch (e) {
    status.textContent = '✗ save failed';
    status.className = 'badge badge-warn';
  }
}

async function updateYtdlp() {
  try {
    const r = await fetch(`${API}/update-ytdlp`, { method: 'POST' });
    const d = await r.json();
    // One line only (#130); the raw pip output is in server.log.
    if (d.summary) log(d.summary, d.ok === false ? 'error' : '');
    fetchVersion();
  } catch {}
}

function formatDuration(secs) {
  const h = Math.floor(secs/3600), m = Math.floor((secs%3600)/60), s = Math.floor(secs%60);
  return [h,m,s].map(x=>String(x).padStart(2,'0')).join(':');
}

// #76 — "SS", "MM:SS" or "H:MM:SS" → whole seconds; '' → null; NaN when unparseable.
function parseTimecode(text) {
  const t = text.trim();
  if (!t) return null;
  if (!/^\d+(:\d{1,2}){0,2}$/.test(t)) return NaN;
  const parts = t.split(':').map(Number);
  if (parts.slice(1).some(p => p > 59)) return NaN;
  return parts.reduce((acc, p) => acc * 60 + p, 0);
}

function fmtClock(secs) {
  const h = Math.floor(secs / 3600), m = Math.floor((secs % 3600) / 60), s = secs % 60;
  const ss = String(s).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`;
}

// Reads card #02's From/To fields against the fetched duration (0 = unknown).
// Returns {section_start, section_end} — both null when no range applies —
// or null after logging why the input is unusable, so callers queue nothing.
function readSectionFields(duration) {
  const start = parseTimecode(document.getElementById('section-start').value);
  const end = parseTimecode(document.getElementById('section-end').value);
  if (Number.isNaN(start) || Number.isNaN(end)) {
    log('Time range: use SS, MM:SS or H:MM:SS.', 'error'); return null;
  }
  if (start !== null && end !== null && end <= start) {
    log('Time range: "To" must be after "From".', 'error'); return null;
  }
  const limit = Math.ceil(duration || 0);
  if (limit > 0 && ((start || 0) >= limit || (end !== null && end > limit))) {
    log(`Time range: the video is only ${fmtClock(limit)} long.`, 'error'); return null;
  }
  if (!start && end === null) return { section_start: null, section_end: null };   // whole video
  return { section_start: start || 0, section_end: end };
}

function clearSectionFields() {
  document.getElementById('section-start').value = '';
  document.getElementById('section-end').value = '';
}

function sectionLabel(q) {
  if (q.section_start == null && q.section_end == null) return '';
  return `${fmtClock(q.section_start || 0)}\u2013${q.section_end == null ? 'end' : fmtClock(q.section_end)}`;
}

function sameQueuedVideo(x, url, section) {
  return x.type === 'youtube' && x.url === url
    && (x.section_start ?? null) === section.section_start
    && (x.section_end ?? null) === section.section_end;
}

function clearFormatSelects() {
  document.getElementById('video-format').innerHTML = '';
  document.getElementById('audio-format').innerHTML = '';
  _lastAudioFormats = [];
}

async function fetchInfo() {
  const url = document.getElementById('url-input').value.trim();
  if (!url) return;
  const btn = document.getElementById('fetch-btn');
  btn.disabled = true; btn.textContent = 'Fetching...';
  log('Fetching video info...', 'info');
  try {
    const r = await fetch(`${API}/video-info?url=${encodeURIComponent(url)}`);
    if (!r.ok) {
      // #67: a failed resolve must not leave the previous URL's itags selectable —
      // a stale pick submitted with a new URL fails every queue item it touches.
      clearFormatSelects();
      const d = await r.json();
      log('Error: ' + d.error, 'error');
      return;
    }
    const info = await r.json();
    const thumbEl = document.getElementById('thumb');
    if (info.thumbnail) {
      thumbEl.src = info.thumbnail;
      thumbEl.style.display = '';
      _fadeInOnLoad(thumbEl);
    } else {
      // Never leave src="" — an empty src resolves to the page URL, which fires
      // a pointless request and paints a broken-image glyph.
      thumbEl.removeAttribute('src');
      thumbEl.style.display = 'none';
      thumbEl.classList.remove('img-loaded');   // so the next real thumbnail fades
    }
    document.getElementById('video-title').textContent = info.is_playlist
      ? 'Playlist: ' + info.title + ' (' + info.count + ' videos)'
      : info.title;
    document.getElementById('video-uploader').textContent = info.uploader ? 'by ' + info.uploader : '';
    document.getElementById('video-duration').textContent = info.is_playlist
      ? 'Formats shown from first video - all videos will use the same selection'
      : (info.duration ? 'Duration: ' + formatDuration(info.duration) : '');
    // Playlists resolve durations per-video server-side; a single /video-info
    // duration isn't representative, so only stamp it for single videos.
    currentVideoDuration = info.is_playlist ? 0 : (info.duration || 0);
    const vSel = document.getElementById('video-format');
    vSel.innerHTML = '';
    info.video_formats.forEach(f => {
      const o = document.createElement('option');
      o.value = f.id; o.textContent = `[${f.id}] ${f.label}`;
      o.dataset.filesize = f.filesize || 0;
      o.dataset.hdr = isHdrFormat(f) ? '1' : '';   // #51 — drives the 8-bit warning
      vSel.appendChild(o);
    });
    const aSel = document.getElementById('audio-format');
    aSel.innerHTML = '';
    info.audio_formats.forEach(f => {
      const o = document.createElement('option');
      o.value = f.id; o.textContent = `[${f.id}] ${f.label}`;
      o.dataset.filesize = f.filesize || 0;
      o.dataset.abr = f.abr || 0;
      aSel.appendChild(o);
    });
    _lastAudioFormats = info.audio_formats || [];
    // #67: the title resolved but the format fetch did not — say why, rather
    // than leaving two silently empty dropdowns.
    if (info.error) log('Could not load formats: ' + info.error, 'error');
    // #15: a resolved playlist gets a per-video checklist (all checked); a
    // single video clears it and behaves as before.
    if (info.is_playlist && Array.isArray(info.entries) && info.entries.length) {
      _playlistEntries = info.entries.map(e => ({ ...e, _checked: true }));
      renderPlaylistSelect();
      document.getElementById('playlist-select').style.display = '';
    } else {
      _playlistEntries = [];
      document.getElementById('playlist-select').style.display = 'none';
    }
    // #76 — a range belongs to the video it was typed for, and is single-video only.
    clearSectionFields();
    document.getElementById('section-row').style.display = info.is_playlist ? 'none' : '';
    // A CSS animation does not replay when its class is already present, and by
    // the second Fetch Info it always is. The block's whole contents have just
    // been rewritten after another 1-3s wait, so it should read as an arrival
    // again: clear the animation, force a style flush, let the class re-apply it.
    const infoEl = document.getElementById('video-info');
    infoEl.style.animation = 'none';
    void infoEl.offsetWidth;            // flush — without this the two writes coalesce
    infoEl.style.animation = '';
    infoEl.classList.add('visible');
    document.getElementById('queue-add-btn').style.display = '';
    autoSelectPreset();
    log('Loaded: ' + info.title, 'info');
    saveToHistory(info, url);
  } finally {
    btn.disabled = false; btn.textContent = 'Fetch Info';
  }
}

function getSelectedExpectedSize() {
  const vSel = document.getElementById('video-format');
  const aSel = document.getElementById('audio-format');
  const aSize = parseInt(aSel.selectedOptions[0]?.dataset.filesize || '0', 10);
  // In audio mode the video stream isn't downloaded — don't count it toward expected size.
  if (currentMode === 'audio') return aSize;
  const vSize = parseInt(vSel.selectedOptions[0]?.dataset.filesize || '0', 10);
  return vSize + aSize;
}

// Read the error text out of a non-OK response (#95). The server sends
// {"error": "..."} for admission/validation failures such as the 409
// single-job guard; a body that won't parse (or has no error) falls back to
// the HTTP status so the failure summary still says something useful.
async function httpErrorText(r) {
  try {
    const body = await r.json();
    if (body && body.error) return body.error;
  } catch {}
  return 'HTTP ' + r.status;
}

// The one SSE reader every request uses (#104). `items` are the queue items the
// request covers; item_done/item_failed map back to them by 1-based idx so the
// caller retries only what actually failed (#28). A non-OK response is a
// failure carrying the server's error text (#95). A terminal `done` marks any
// item still without an outcome successful: that is the legacy single-item
// signal and /convert-local, which report no per-item events. Without `done`
// (a dropped stream, or the encoder abort cap) an item with no outcome is
// failed so it retries instead of being silently dropped.
// Returns { results: Array<{item, ok, cancelled, error}> } in submission order.
async function runSse(endpoint, form, items) {
  let gotDone = false;
  let gotCancelled = false;
  const outcome = new Map(); // idx (1-based) -> { ok, error? }
  _lastErrorMsg = '';        // per-request, never a previous item's reason
  // Fresh AbortController per request so cancelJob() can abort the in-flight stream.
  const controller = new AbortController();
  _currentController = controller;

  try {
    const r = await fetch(endpoint, { method: 'POST', body: form, signal: controller.signal });
    if (!r.ok) {
      // A non-OK response (e.g. the 409 single-job guard) is JSON, not SSE, so
      // its body has no data: lines. Without this check the request would read
      // to the end with no outcome although nothing ran.
      _lastErrorMsg = await httpErrorText(r);
      log(`✕ ${_lastErrorMsg}`, 'error');
    } else {
      const reader = r.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split('\n');
        buf = lines.pop();
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          const raw = line.slice(6).trim();
          if (!raw) continue;
          try {
            const msg = JSON.parse(raw);
            if (msg.type === 'item_done') outcome.set(msg.idx, { ok: true });
            else if (msg.type === 'item_failed') outcome.set(msg.idx, { ok: false, error: msg.msg || 'failed' });
            else if (msg.type === 'done') gotDone = true;
            else if (msg.type === 'cancelled') gotCancelled = true;
            handleMsg(msg);
          } catch {}
        }
      }
    }
  } catch (e) {
    // An aborted fetch (user hit Cancel) is terminal, not a retryable error.
    if (e && e.name === 'AbortError') gotCancelled = true;
    else {
      // A rejected fetch or a mid-stream read error (server exited, connection
      // reset) is a retryable failure. Letting it escape startDownload would
      // leave the job flag set and the Cancel button/wake lock stuck.
      _lastErrorMsg = (e && e.message) ? e.message : String(e);
      log(`✕ ${_lastErrorMsg}`, 'error');
    }
  } finally {
    if (_currentController === controller) _currentController = null;
  }

  // Build results in submission order. An item with no outcome (e.g. the
  // stream died mid-run, or the encoder consecutive-failure abort cap left an
  // already-downloaded item with neither an item event nor `done`) counts as
  // failed so it retries rather than being silently dropped.
  const results = items.map((item, i) => {
    if (gotCancelled || cancelled) return { item, ok: false, cancelled: true };
    const o = outcome.get(i + 1);
    if (o) return { item, ok: !!o.ok, cancelled: false, error: o.error };
    if (gotDone) return { item, ok: true, cancelled: false };
    return { item, ok: false, cancelled: false, error: _lastErrorMsg || 'no result' };
  });
  return { results };
}

// Map a queued youtube item to the server's per-item payload.
function toItemPayload(it) {
  return {
    url: it.url,
    video_format: it.video_format || '',
    audio_format: it.audio_format,
    expected_size: it.expected_size || 0,
    output_dir: it.output_dir || document.getElementById('output-dir').value.trim(),
    tune_mode: it.tune_mode || currentTuneMode,
    title: it.title || it.url,
    duration: it.duration || 0,
    section_start: it.section_start ?? null,
    section_end: it.section_end ?? null,
  };
}

// POST a run of youtube items to /download. A single item and a pipelined
// batch are the same request now (#104); the server always runs an items list.
// Results mirror `items` in submission order.
async function runDownload(items, opts) {
  const form = new FormData();
  // url/audio_format/video_format are required form fields on /download; the
  // server ignores them in items mode but they must be present. Seed from the
  // first item.
  form.append('url', items[0].url);
  form.append('video_format', items[0].video_format || '');
  form.append('audio_format', items[0].audio_format || '');
  form.append('items', JSON.stringify(items.map(toItemPayload)));
  form.append('convert', opts.convert);
  form.append('cq', opts.cq);
  form.append('maxrate', opts.maxrate);
  appendEncodeOverrides(form, opts);
  form.append('pipeline', opts.pipeline);
  form.append('delete_cache', opts.deleteCacheVal);
  form.append('shutdown', 'false');
  form.append('output_dir', opts.outputDir);
  form.append('tune_mode', opts.tuneMode);
  form.append('mode', opts.mode);
  form.append('audio_preset', opts.audioPreset);
  form.append('target_res', opts.targetRes);
  form.append('sharpen', opts.sharpen);
  return runSse(`${API}/download`, form, items);
}

// Local files keep using /convert-local; the shared reader treats the one file
// as a single item (the server reports no per-item events).
async function runLocal(item, opts) {
  const form = new FormData();
  form.append('files', JSON.stringify([item.path]));
  form.append('cq', opts.cq);
  form.append('maxrate', opts.maxrate);
  appendEncodeOverrides(form, opts);
  form.append('shutdown', 'false');
  form.append('output_dir', opts.outputDir);
  form.append('tune_mode', opts.tuneMode);
  form.append('mode', opts.mode);
  form.append('audio_preset', opts.audioPreset);
  form.append('target_res', opts.targetRes);
  form.append('sharpen', opts.sharpen);
  return runSse(`${API}/convert-local`, form, [item]);
}

const MAX_RETRY_ROUNDS = 3;

async function startDownload() {
  // A resolved playlist must go through addToQueue() so the per-video checklist
  // (#15) is honoured. Seeding the raw playlist URL below instead would leave
  // _playlistEntries unread, and the server would expand every entry itself
  // (server.py `_type == "playlist"`) — silently downloading the whole playlist
  // from the top and ignoring the user's selection.
  // Runs whether or not the queue already holds items, so picks are never
  // silently dropped; addToQueue() dedupes by URL.
  if (_playlistEntries.length) {
    addToQueue();
    if (dlQueue.length === 0) return;   // nothing checked, nothing else queued
  }

  // If no queue, seed it from the current URL
  if (dlQueue.length === 0) {
    const url = document.getElementById('url-input').value.trim();
    const vf = document.getElementById('video-format').value;
    const af = document.getElementById('audio-format').value;
    if (!url) { log('Enter a URL first, or add items to the queue.', 'error'); return; }
    const needVideo = currentMode !== 'audio';
    if ((needVideo && !vf) || !af) { log('Fetch video info and select formats first.', 'error'); return; }
    const section = readSectionFields(currentVideoDuration);
    if (!section) return;
    dlQueue.push({
      type: 'youtube', url, title: url,
      video_format: needVideo ? vf : '', audio_format: af,
      expected_size: getSelectedExpectedSize(),
      output_dir: document.getElementById('output-dir').value.trim(),
      tune_mode: currentTuneMode,
      duration: currentVideoDuration || 0,
      ...section,
    });
    renderDlQueue();
  }

  const opts = {
    cq: document.getElementById('cq-select').value,
    maxrate: document.getElementById('maxrate').value,
    // #51 — "" for all three means auto, which is what an untouched panel sends.
    preset: document.getElementById('enc-speed-select').value,
    tune: document.getElementById('tune-select').value,
    pixFmt: document.getElementById('pixfmt-select').value,
    convert: document.getElementById('convert-toggle').checked ? 'true' : 'false',
    pipeline: document.getElementById('pipeline-check').checked ? 'true' : 'false',
    deleteCacheVal: document.getElementById('delete-cache-check').checked ? 'true' : 'false',
    shutdownVal: document.getElementById('shutdown-check').checked ? 'true' : 'false',
    outputDir: document.getElementById('output-dir').value.trim(),
    tuneMode: currentTuneMode,
    mode: currentMode,
    audioPreset: currentAudioPreset,
    targetRes: document.getElementById('target-res').value || '',
    sharpen: document.getElementById('sharpen').checked ? 'true' : 'false',
  };

  _jobRunning = true;
  _queueProgress = { completed: 0, total: 0 };   // stale from a prior run
  cancelled = false;            // fresh run — clear any prior cancel state
  _currentController = null;
  acquireWakeLock();

  document.getElementById('start-btn').classList.add('hidden');
  document.getElementById('cancel-btn').classList.remove('hidden');
  document.getElementById('idle-msg').classList.add('hidden');
  document.getElementById('progress-section').classList.add('visible');
  document.getElementById('dl-status-row').style.display = 'none';
  document.getElementById('size-info-row').style.display = 'none';
  document.getElementById('charts-row').style.display = 'grid';
  document.getElementById('encode-params-row').style.display = 'none';
  document.getElementById('stat-elapsed').textContent = '—';
  etaEmaRate = null; etaLastPct = 0; etaLastT = 0;
  chartDl.reset();
  chartEnc.reset();
  resetSizeTracker();
  setPhaseState('idle');
  _lastPhaseText = '';

  let completed = 0;
  let retryQueue = [];  // items that failed and need retrying
  let allDone = false;

  // The run loop restores the idle UI in `finally` so an unexpected throw can
  // never leave _jobRunning set, Cancel visible, or the wake lock held (#95).
  try {

  // Main loop: drain dlQueue live — new items added mid-job are picked up
  while (dlQueue.length > 0 || retryQueue.length > 0) {
    // User cancelled — stop without starting the next item or any retries.
    if (cancelled) break;

    // If main queue is drained but retries remain, swap them in after a pause
    if (dlQueue.length === 0 && retryQueue.length > 0) {
      if (retryQueue[0]._retries >= MAX_RETRY_ROUNDS - 1) {
        // Max retries exhausted
        log(`✕ ${retryQueue.length} item(s) failed after ${MAX_RETRY_ROUNDS - 1} retries:`, 'error');
        retryQueue.forEach(p => {
          log(`  • ${p.title || p.url}`, 'error');
          recordFailure(p, p._lastError || '', true);
        });
        break;
      }
      const round = retryQueue[0]._retries + 1;
      const waitSecs = 15 * round;
      log(`── Retry round ${round}/${MAX_RETRY_ROUNDS - 1}: ${retryQueue.length} item(s) — waiting ${waitSecs}s ──`, 'info');
      // Interruptible wait so Cancel takes effect during the backoff.
      for (let waited = 0; waited < waitSecs * 1000 && !cancelled; waited += 250) {
        await new Promise(r => setTimeout(r, 250));
      }
      if (cancelled) break;
      // Move retries back into main queue (new items added during wait go first)
      dlQueue.push(...retryQueue);
      retryQueue = [];
      renderDlQueue();
    }

    // Batch mode: when pipelining video conversions, gather the maximal run of
    // consecutive youtube items at the front of the queue and send them as one
    // pipelined /download so download(N+1) overlaps encode(N) across the run.
    // Per-item retry (#28): only the items that actually failed are requeued.
    const canBatch = opts.pipeline === 'true' && opts.convert === 'true' && opts.mode !== 'audio';
    if (canBatch && dlQueue.length >= 2 && dlQueue[0].type === 'youtube' && dlQueue[1].type === 'youtube') {
      const batchItems = [];
      while (dlQueue.length > 0 && dlQueue[0].type === 'youtube') {
        batchItems.push(dlQueue.shift());
      }
      renderDlQueue();
      completed += batchItems.length;
      const totalVisible = completed + dlQueue.length + retryQueue.length;
      // A pipelined batch is one request spanning N videos, so the server's own
      // video_start/progress.overall already tracks the whole run — cede the bar.
      _queueProgress = { completed: 0, total: 0 };
      document.getElementById('overall-section').style.display = '';
      document.getElementById('stat-overall').textContent = completed + ' / ' + totalVisible;
      log(`── Batch of ${batchItems.length} video(s) — pipelined ──`, 'info');
      opts.shutdown = 'false';
      const { results } = await runDownload(batchItems, opts);
      if (results.some(r => r.cancelled) || cancelled) { cancelled = true; break; }
      let anyFailed = false;
      for (const r of results) {
        if (r.ok) continue;
        anyFailed = true;
        r.item._lastError = r.error;
        r.item._retries = (r.item._retries || 0) + 1;
        if (r.item._retries < MAX_RETRY_ROUNDS) {
          log(`⚠ Failed: ${r.item.title || r.item.url} — will retry after remaining items.`, 'error');
          recordFailure(r.item, r.error, false);
          retryQueue.push(r.item);
        } else {
          log(`✕ Failed: ${r.item.title || r.item.url} — max retries exhausted.`, 'error');
          recordFailure(r.item, r.error, true);
        }
      }
      if (!anyFailed) {
        document.getElementById('overall-bar-fill').style.width = (completed / totalVisible * 100).toFixed(1) + '%';
      }
      continue;
    }

    // Pop next item from front of queue
    const item = dlQueue.shift();
    renderDlQueue();
    completed++;

    const totalVisible = completed + dlQueue.length + retryQueue.length;
    const showOverall = totalVisible > 1 || completed > 1;
    _queueProgress = { completed, total: totalVisible };
    if (showOverall) {
      renderOverall(0);
    } else {
      document.getElementById('overall-section').style.display = 'none';
    }

    const label = item.title || item.url;
    const retryTag = item._retries ? ` (retry ${item._retries})` : '';
    if (showOverall || item._retries) {
      log(`── ${completed}/${totalVisible}: ${label}${retryTag}`, 'info');
    }

    // Never send shutdown to server mid-queue — handled client-side after all items complete
    opts.shutdown = 'false';

    _lastErrorMsg = '';
    const { results } = item.type === 'local'
      ? await runLocal(item, opts)
      : await runDownload([item], opts);
    const result = results[0];

    // Cancelled (client abort or server 'cancelled' event): terminal, no retry, stop the run.
    if (result.cancelled || cancelled) {
      cancelled = true;
      break;
    }

    if (!result.ok) {
      const errText = result.error || _lastErrorMsg;
      item._lastError = errText;
      item._retries = (item._retries || 0) + 1;
      if (item._retries < MAX_RETRY_ROUNDS) {
        log(`⚠ Failed: ${label} — will retry after remaining items.`, 'error');
        recordFailure(item, errText, false);  // transient (mid-retry)
        retryQueue.push(item);
      } else {
        log(`✕ Failed: ${label} — max retries exhausted.`, 'error');
        recordFailure(item, errText, true);    // permanent
      }
    }

    if (showOverall && result.ok) {
      renderOverall(100);
    }
  }

  allDone = !cancelled && dlQueue.length === 0 && retryQueue.length === 0;

  if (allDone) {
    document.getElementById('overall-bar-fill').style.width = '100%';
  }
  } finally {
    _jobRunning = false;
    releaseWakeLock();
    stopElapsedTimer();
    document.getElementById('start-btn').classList.remove('hidden');
    document.getElementById('cancel-btn').classList.add('hidden');
  }

  // Shutdown: queue is fully drained, nothing was added during final item
  if (allDone && opts.shutdownVal === 'true') {
    const proceed = await runShutdownCountdown(60);
    if (proceed) {
      log('Countdown elapsed — shutting down.', 'info');
      const token = document.querySelector('meta[name="dlpr-token"]')?.content;
      try {
        await fetch(`${API}/shutdown-now`, { method: 'POST', headers: { 'X-DLPR-Token': token } });
      } catch {}
    } else {
      log('Shutdown cancelled by user.', 'info');
    }
  }
}

// Issue #6 — visible, cancellable shutdown countdown overlay.
// Resolves true if the countdown runs out, false if the user cancels.
function runShutdownCountdown(seconds) {
  return new Promise((resolve) => {
    log(`All jobs complete — shutting down in ${seconds} seconds.`, 'info');

    const overlay = document.createElement('div');
    overlay.className = 'shutdown-overlay';
    overlay.id = 'shutdown-overlay';
    overlay.innerHTML =
      '<div class="shutdown-overlay-card">' +
        '<div class="shutdown-overlay-title">Shutting down PC</div>' +
        '<div class="shutdown-overlay-count" id="shutdown-overlay-count">0:00</div>' +
        '<div class="shutdown-overlay-sub">All jobs are complete. The PC will power off when the timer reaches zero.</div>' +
        '<button class="btn btn-danger" id="shutdown-cancel-btn"><svg class="icon" aria-hidden="true"><use href="#i-x"/></svg> Cancel Shutdown</button>' +
      '</div>';
    document.body.appendChild(overlay);

    let remaining = seconds;
    let timer = null;
    const countEl = document.getElementById('shutdown-overlay-count');

    function fmt(s) {
      const m = Math.floor(s / 60);
      return m + ':' + String(s % 60).padStart(2, '0');
    }
    // Exit along the entry path (see .shutdown-overlay.closing), 180ms.
    //
    // transitionend alone is not safe here and this is the one place in the
    // file where that really bites: a full-screen overlay that never leaves
    // blocks the whole UI at the exact moment the PC is about to power off.
    // The event can simply not arrive — a zero-length transition, a detached
    // element, a dropped event — so a timer removes the node unconditionally
    // and whichever path arrives first wins. `removed` makes the removal
    // idempotent; `closing` makes a second teardown() a no-op.
    const EXIT_MS = 180;
    function teardown() {
      if (timer) { clearInterval(timer); timer = null; }
      const el = document.getElementById('shutdown-overlay');
      if (!el || !el.parentNode) return;
      if (el.classList.contains('closing')) return;

      let removed = false;
      let fallback = null;
      function remove() {
        if (removed) return;
        removed = true;
        if (fallback !== null) { clearTimeout(fallback); fallback = null; }
        el.removeEventListener('transitionend', onEnd);
        if (el.parentNode) el.parentNode.removeChild(el);
      }
      function onEnd(e) { if (e.target === el) remove(); }

      el.addEventListener('transitionend', onEnd);
      fallback = setTimeout(remove, EXIT_MS + 80);
      // Two steps, not one. A CSS animation wins over a transition on the same
      // property, and the entry fadeUp's `both` fill is still applying opacity
      // and transform — dropping it in the same style change as .closing makes
      // the exit snap instead of animate. So: drop the animation, flush, then
      // apply the closing state so the transition has a "before" to run from.
      el.style.animation = 'none';
      void el.offsetWidth;
      el.classList.add('closing');
    }

    countEl.textContent = fmt(remaining);
    document.getElementById('shutdown-cancel-btn').onclick = function () {
      teardown();
      resolve(false);
    };

    timer = setInterval(function () {
      remaining -= 1;
      if (remaining <= 0) {
        teardown();
        resolve(true);
        return;
      }
      countEl.textContent = fmt(remaining);
    }, 1000);
  });
}

// ── Job-level ("overall") progress bar ────────────────────────────────────────
// Two things write this bar: the client queue loop and the server's
// video_start/progress.overall events. Since playlist expansion moved
// client-side (#15) the server usually sees only ONE video per request and
// reports total=1, so letting it drive would hide the bar for a multi-item
// queue. The client queue is therefore the authority whenever it holds more
// than one item; the server's per-request progress is folded in as the
// fraction *within* the current item. When the client cedes control
// (single item, or a pipelined batch where the server does span N videos)
// _queueProgress is zeroed and the original server-driven path runs unchanged.
let _queueProgress = { completed: 0, total: 0 };
function clientOwnsOverall() {
  return _queueProgress.total > 1 || _queueProgress.completed > 1;
}
function renderOverall(itemPct) {
  if (!clientOwnsOverall()) return;   // server-driven path handles display
  const { completed, total } = _queueProgress;
  document.getElementById('overall-section').style.display = '';
  document.getElementById('stat-overall').textContent = completed + ' / ' + total;
  const within = Math.min(100, Math.max(0, itemPct || 0)) / 100;
  const frac = Math.min(1, Math.max(0, ((completed - 1) + within) / total));
  document.getElementById('overall-bar-fill').style.width = (frac * 100).toFixed(1) + '%';
}

function handleMsg(msg) {
  const barBg = document.getElementById('bar-bg');
  if (msg.type === 'video_start') {
    const overallSec = document.getElementById('overall-section');
    videoEncodeStart = Date.now();
    etaEmaRate = null; etaLastPct = 0; etaLastT = 0;
    setPhaseState('idle');
    document.getElementById('encode-params-row').style.display = 'none';
    document.getElementById('stat-elapsed').textContent = '00:00:00';
    resetSizeTracker();
    startElapsedTimer();
    if (clientOwnsOverall()) {
      renderOverall(0);            // start of this item; keep the job-level bar
    } else if (msg.total > 1) {
      overallSec.style.display = '';
      document.getElementById('stat-overall').textContent = msg.current + ' / ' + msg.total;
      const pct = ((msg.current - 1) / msg.total * 100).toFixed(1);
      document.getElementById('overall-bar-fill').style.width = pct + '%';
    } else {
      overallSec.style.display = 'none';
    }

  } else if (msg.type === 'phase') {
    if (!videoEncodeStart) { videoEncodeStart = Date.now(); etaEmaRate = null; etaLastPct = 0; etaLastT = 0; }
    const colonIdx = msg.msg.indexOf(': ');
    var phLabel;
    if (colonIdx !== -1) {
      phLabel = msg.msg.slice(0, colonIdx);
      document.getElementById('phase-label').textContent = phLabel;
      document.getElementById('phase-file').textContent = msg.msg.slice(colonIdx + 2);
    } else {
      phLabel = msg.msg;
      document.getElementById('phase-label').textContent = phLabel;
      document.getElementById('phase-file').textContent = '';
    }
    document.getElementById('bar-fill').style.width = '0%';
    barBg.classList.add('bar-indeterminate');
    document.getElementById('stat-pct').textContent = '—';
    document.getElementById('stat-eta').textContent = '—';

    // F1: detect phase from label text
    var newPhase = /download/i.test(phLabel) ? 'downloading' : 'converting';
    var phaseChanged = setPhaseState(newPhase);

    // F3: phase transition animation
    if (phLabel !== _lastPhaseText) {
      var labelEl = document.getElementById('phase-label');
      var rowEl = document.getElementById('phase-row');
      labelEl.classList.remove('transitioning');
      rowEl.classList.remove('glow');
      void labelEl.offsetWidth;  // force reflow
      labelEl.classList.add('transitioning');
      rowEl.classList.add('glow');
      _lastPhaseText = phLabel;
    }

    // F6: chart phase markers
    if (phaseChanged) {
      var markerLabel = newPhase === 'downloading' ? 'DL' : 'ENC';
      var markerColor = newPhase === 'downloading' ? 'rgba(78,205,196,0.5)' : 'rgba(124,92,252,0.5)';
      chartDl.addMarker(markerLabel, markerColor);
      chartEnc.addMarker(markerLabel, markerColor);
    }

  } else if (msg.type === 'progress') {
    barBg.classList.remove('bar-indeterminate');
    document.getElementById('bar-fill').style.width = msg.pct.toFixed(1) + '%';
    document.getElementById('stat-pct').textContent = msg.pct.toFixed(1) + '%';
    if (msg.overall != null) {
      if (clientOwnsOverall()) renderOverall(msg.overall);
      else document.getElementById('overall-bar-fill').style.width = msg.overall.toFixed(1) + '%';
    }
    if (msg.size) document.getElementById('stat-size').textContent = msg.size;
    if (msg.speed) document.getElementById('stat-speed').textContent = msg.speed;
    if (videoEncodeStart) {
      var now = Date.now();
      if (etaLastT) {
        var dt = (now - etaLastT) / 1000;
        if (dt > 0) {
          var instRate = (msg.pct - etaLastPct) / dt;   // pct per second
          if (instRate > 0 && isFinite(instRate)) {
            var alpha = 1 - Math.exp(-dt / ETA_TAU);
            etaEmaRate = (etaEmaRate == null) ? instRate
                                              : alpha * instRate + (1 - alpha) * etaEmaRate;
          }
        }
      }
      etaLastPct = msg.pct;
      etaLastT = now;

      if (msg.pct >= ETA_WARMUP_PCT && etaEmaRate && etaEmaRate > 0) {
        document.getElementById('stat-eta').textContent = fmtEta((100 - msg.pct) / etaEmaRate);
      } else {
        document.getElementById('stat-eta').textContent = 'Estimating…';
      }
    }
    if (msg.speed) {
      var encSpd = parseEncodeSpeed(msg.speed);
      var dlSpd  = parseDownloadSpeed(msg.speed);
      if (encSpd !== null) chartEnc.push(encSpd);
      else if (dlSpd !== null) chartDl.push(dlSpd);
    }
    if (msg.output_bytes) updateSizeTracker(msg.output_bytes);

  } else if (msg.type === 'size_info') {
    var sign = msg.bloat_pct >= 0 ? '+' : '';
    var color = msg.bloat_pct > 20 ? '#fc8181' : msg.bloat_pct > 0 ? '#f6ad55' : '#68d391';
    var row = document.getElementById('size-info-row');
    document.getElementById('size-info-text').innerHTML =
      'Source: <strong>' + msg.source_mb.toFixed(0) + ' MB</strong>' +
      ' &rarr; Output: <strong>' + msg.output_mb.toFixed(0) + ' MB</strong>' +
      ' <span style="color:' + color + '">(' + sign + msg.bloat_pct.toFixed(1) + '%)</span>';
    row.style.display = '';

  } else if (msg.type === 'source_size') {
    initSizeTracker(msg.bytes);

  } else if (msg.type === 'dl_progress') {
    var dlRow = document.getElementById('dl-status-row');
    dlRow.style.display = '';
    document.getElementById('dl-status-label').textContent = 'DL ' + msg.idx + '/' + msg.total + ' — ' + msg.size;
    document.getElementById('dl-status-pct').textContent = msg.pct.toFixed(1) + '%';
    document.getElementById('dl-status-speed').textContent = msg.speed;
    document.getElementById('dl-status-eta').textContent = 'ETA ' + msg.eta;
    chartDl.push(parseDownloadSpeed(msg.speed));

  } else if (msg.type === 'encode_params') {
    showEncodeParams(msg);

  } else if (msg.type === 'log') {
    log(msg.msg);

  } else if (msg.type === 'error') {
    _lastErrorMsg = msg.msg || '';
    log('ERROR: ' + msg.msg, 'error');
    document.getElementById('phase-label').textContent = '✕ Failed';
    document.getElementById('phase-file').textContent = '';
    barBg.classList.remove('bar-indeterminate');
    setPhaseState('error');
    stopElapsedTimer();

  } else if (msg.type === 'cancelled') {
    // Server confirmed the job was cancelled by the user — distinct from an error.
    cancelled = true;
    log(msg.msg || 'Cancelled by user.', 'info');
    document.getElementById('phase-label').textContent = '✕ Cancelled';
    document.getElementById('phase-file').textContent = '';
    barBg.classList.remove('bar-indeterminate');
    setPhaseState('error');
    stopElapsedTimer();

  } else if (msg.type === 'done') {
    document.getElementById('phase-label').textContent = '✓ Complete';
    document.getElementById('phase-file').textContent = '';
    document.getElementById('bar-fill').style.width = '100%';
    document.getElementById('stat-pct').textContent = '100%';
    document.getElementById('overall-bar-fill').style.width = '100%';
    document.getElementById('dl-status-row').style.display = 'none';
    barBg.classList.remove('bar-indeterminate');
    setPhaseState('done');
    stopElapsedTimer();
    celebrate();
    log(msg.msg, 'done');

  } else if (msg.type === 'cookie_warning') {
    // Server detected the loaded cookies were rotated/invalidated and dropped
    // them for the rest of the run. Surface it and mark the badge stale.
    log('⚠ ' + msg.msg, 'error');
    const badge = document.getElementById('cookie-badge');
    if (badge) { badge.textContent = '⚠ cookies stale — running without them'; badge.className = 'badge badge-warn'; }
    const dot = document.getElementById('cookie-dot');
    if (dot) dot.className = 'dot warn';
    const hdr = document.getElementById('cookie-header');
    if (hdr) hdr.textContent = 'cookies: stale';

  } else if (msg.type === 'shutdown') {
    log('⚠ ' + msg.msg, 'info');
  }
}

const LOG_MAX_NODES = 500;
let _logBuffer = [];        // pending lines, flushed in a rAF
let _logFlushScheduled = false;

function log(msg, type='') {
  _logBuffer.push({ msg, type });
  if (_logFlushScheduled) return;
  _logFlushScheduled = true;
  requestAnimationFrame(_flushLog);
}

function _flushLog() {
  _logFlushScheduled = false;
  const box = document.getElementById('log-box');
  if (!box) { _logBuffer = []; return; }

  // (1) Only auto-scroll if the user was already pinned to the bottom.
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 4;

  const batch = _logBuffer;
  _logBuffer = [];
  const frag = document.createDocumentFragment();
  for (const { msg, type } of batch) {
    const line = document.createElement('div');
    if (type === 'error') line.className = 'log-error';
    else if (type === 'done') line.className = 'log-done';
    else if (type === 'info') line.className = 'log-info';
    line.textContent = msg;
    frag.appendChild(line);
  }
  box.appendChild(frag);

  // (2) Cap node count — trim oldest lines beyond the limit.
  while (box.childNodes.length > LOG_MAX_NODES) {
    box.removeChild(box.firstChild);
  }

  // (1) Auto-scroll only if the user hadn't scrolled up.
  if (atBottom) box.scrollTop = box.scrollHeight;
}

// ---- FAILURE SUMMARY PANEL (Issue #10) ----
// Accumulates failed items for the whole session; survives later completions.
// Each entry: { title, reason, category, permanent }.
let _failures = [];

function categorizeFailure(msg) {
  const m = (msg || '').toLowerCase();
  if (/cookie|sign in|login required|consent|age.?restrict|not a bot|confirm you/.test(m))
    return 'cookies expired';
  if (/yt-dlp|extractor|unable to extract|nsig|player|json|parse|unavailable|private video|removed/.test(m))
    return 'yt-dlp parse error';
  if (/ffmpeg|nvenc|encode|initializeencoder|invalid param|hevc/.test(m))
    return 'ffmpeg crash';
  if (/network|timeout|timed out|connection|resolve host|temporary failure|http error|10054|reset/.test(m))
    return 'network';
  return 'other';
}

function recordFailure(item, reason, permanent) {
  const title = (item && (item.title || item.url || item.path)) || 'Unknown item';
  // Promote an existing transient entry to permanent rather than duplicating.
  const existing = _failures.find(f => (f.title === title));
  if (existing) {
    existing.reason = reason || existing.reason;
    existing.category = categorizeFailure(reason || existing.reason);
    if (permanent) existing.permanent = true;
  } else {
    _failures.push({
      title,
      reason: reason || '(no detail)',
      category: categorizeFailure(reason),
      permanent: !!permanent,
    });
  }
  renderFailurePanel();
}

function renderFailurePanel() {
  const panel = document.getElementById('failure-panel');
  const list = document.getElementById('failure-panel-list');
  const count = document.getElementById('failure-panel-count');
  if (!panel) return;
  if (_failures.length === 0) { panel.classList.remove('visible'); return; }
  panel.classList.add('visible');
  count.textContent = _failures.length;
  list.innerHTML = _failures.map(f => {
    const cls = f.permanent ? 'permanent' : 'transient';
    // Permanent vs transient is carried by the mark alone, so unlike the
    // decorative icons these get an accessible name rather than aria-hidden.
    const icon = f.permanent
      ? '<svg class="icon" role="img" aria-label="Permanent failure"><use href="#i-x"/></svg>'
      : '<svg class="icon" role="img" aria-label="Will be retried"><use href="#i-alert"/></svg>';
    const reason = (f.reason || '').replace(/\s+/g, ' ').slice(0, 160);
    return '<li class="failure-item ' + cls + '">' +
      '<span class="failure-item-icon">' + icon + '</span>' +
      '<span class="failure-item-body">' +
        '<span class="failure-item-title" title="' + escapeAttr(f.title) + '">' + escapeHtml(f.title) + '</span>' +
        '<span class="failure-item-reason"><span class="failure-item-cat">' + escapeHtml(f.category) + '</span> · ' + escapeHtml(reason) + '</span>' +
      '</span>' +
    '</li>';
  }).join('');
}

function dismissFailurePanel() {
  _failures = [];
  renderFailurePanel();
}

function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function escapeAttr(s) {
  return escapeHtml(s).replace(/"/g, '&quot;');
}

// ---- JOB QUEUE ----
let dlQueue = []; // array of {url, title, video_format, audio_format}

// #15 — render the resolved playlist as a per-video checklist.
function renderPlaylistSelect() {
  const box = document.getElementById('playlist-items');
  box.innerHTML = _playlistEntries.map((e, i) => {
    const dur = e.duration ? ' · ' + formatDuration(e.duration) : '';
    return `<label style="display:flex;align-items:center;gap:8px;padding:5px 10px;border-bottom:1px solid var(--border);cursor:pointer;">
      <input type="checkbox" ${e._checked ? 'checked' : ''} onchange="_playlistEntries[${i}]._checked=this.checked;updatePlaylistCount()">
      <span style="color:var(--muted);font-size: 11px;flex-shrink:0;width:28px;">${e.index}</span>
      <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeAttr(e.title)}">${escapeHtml(e.title)}</span>
      <span style="color:var(--muted);font-size: 11px;flex-shrink:0;">${dur}</span>
    </label>`;
  }).join('');
  updatePlaylistCount();
}

function updatePlaylistCount() {
  document.getElementById('playlist-sel-count').textContent = _playlistEntries.filter(e => e._checked).length;
  document.getElementById('playlist-total').textContent = _playlistEntries.length;
}

function playlistSelectAll(v) { _playlistEntries.forEach(e => e._checked = v); renderPlaylistSelect(); }
function playlistInvert() { _playlistEntries.forEach(e => e._checked = !e._checked); renderPlaylistSelect(); }

function addToQueue() {
  // Playlist: expand each checked entry into its own queue item (#15). Leave
  // video_format empty so the server takes the highest-available stream per
  // video (mixed 720p/1080p lists — #13); the picked audio format still rides
  // along but the highest-available selector ignores it when video_format is
  // blank, so a video missing the exact audio itag still succeeds.
  if (_playlistEntries.length) {
    const af = document.getElementById('audio-format').value;
    if (!af) { log('Select an audio format first.', 'error'); return; }
    const chosen = _playlistEntries.filter(e => e._checked);
    if (!chosen.length) { log('No videos selected.', 'error'); return; }
    const outDir = document.getElementById('output-dir').value.trim();
    let added = 0;
    chosen.forEach(e => {
      if (dlQueue.find(x => x.type === 'youtube' && x.url === e.url)) return;
      dlQueue.push({
        type: 'youtube', url: e.url, title: e.title || e.url,
        video_format: '', audio_format: af,
        expected_size: 0,              // no per-entry size from flat-playlist; duration gates completeness
        output_dir: outDir,
        tune_mode: currentTuneMode,
        duration: e.duration || 0,
      });
      added++;
    });
    log(`Queued ${added} video(s) from playlist${added !== chosen.length ? ' (' + (chosen.length - added) + ' already queued)' : ''}.`, 'info');
    renderDlQueue();
    document.getElementById('url-input').value = '';
    document.getElementById('video-info').classList.remove('visible');
    document.getElementById('queue-add-btn').style.display = 'none';
    document.getElementById('playlist-select').style.display = 'none';
    _playlistEntries = [];
    currentVideoDuration = 0;
    return;
  }

  const url = document.getElementById('url-input').value.trim();
  const vf = document.getElementById('video-format').value;
  const af = document.getElementById('audio-format').value;
  const title = document.getElementById('video-title').textContent || url;
  const needVideo = currentMode !== 'audio';
  if (!url || (needVideo && !vf) || !af) { log('Fetch video info and select formats first.', 'error'); return; }
  const section = readSectionFields(currentVideoDuration);
  if (!section) return;
  // Two different ranges of one video are two jobs; the same range twice is one.
  if (dlQueue.find(x => sameQueuedVideo(x, url, section))) { log('Already in queue.', 'info'); return; }
  dlQueue.push({
    type: 'youtube', url, title,
    video_format: needVideo ? vf : '', audio_format: af,
    expected_size: getSelectedExpectedSize(),
    output_dir: document.getElementById('output-dir').value.trim(),
    tune_mode: currentTuneMode,
    duration: currentVideoDuration || 0,   // captured from /video-info
    ...section,
  });
  log('Queued: ' + title, 'info');
  renderDlQueue();
  // Clear URL input for next entry
  document.getElementById('url-input').value = '';
  document.getElementById('video-info').classList.remove('visible');
  document.getElementById('queue-add-btn').style.display = 'none';
  clearSectionFields();
  currentVideoDuration = 0; // stale until the next /video-info fetch
}

function renderDlQueue() {
  const sec = document.getElementById('dl-queue-section');
  const items = document.getElementById('dl-queue-items');
  const count = document.getElementById('dl-queue-count');
  if (dlQueue.length === 0) { sec.style.display = 'none'; return; }
  sec.style.display = 'block';
  count.textContent = dlQueue.length;
  items.innerHTML = dlQueue.map((q, i) => {
    const tag = q.type === 'local'
      ? '<span style="background:var(--accent3);color:#111;font-size: 11px;padding:1px 5px;border-radius:3px;margin-right:6px;">ENC</span>'
      : '<span style="background:var(--accent);color:#111;font-size: 11px;padding:1px 5px;border-radius:3px;margin-right:6px;">DL</span>';
    const fmtLabel = q.video_format
      ? `${escapeHtml(q.video_format)}+${escapeHtml(q.audio_format)}`
      : (currentMode === 'audio' ? `audio:${escapeHtml(q.audio_format)}` : `best+${escapeHtml(q.audio_format)}`);
    const detail = q.type === 'local'
      ? `<span class="queue-detail">${q.size_gb} GB</span>`
      : `<span class="queue-detail">${sectionLabel(q) ? `<span class="queue-range">${sectionLabel(q)}</span> \u00b7 ` : ''}${fmtLabel}${q.expected_size > 0 && !sectionLabel(q) ? ' \u00b7 ' + (q.expected_size / 1024**3).toFixed(1) + 'GB' : ''}</span>`;
    const tip = q.type === 'local' ? q.path : q.url;
    const tuneCtl = q.type === 'youtube'
      ? `<select onchange="dlQueue[${i}].tune_mode=this.value" aria-label="Encode quality">
           <option value="uhq"${q.tune_mode !== 'hq' ? ' selected' : ''}>Excellent (Slow)</option>
           <option value="hq"${q.tune_mode === 'hq' ? ' selected' : ''}>Great (Fast)</option>
         </select>`
      : '';
    const dirCtl = q.type === 'youtube'
      ? `<input type="text" class="queue-dir" value="${escapeAttr(q.output_dir || '')}" placeholder="(default out)"
           aria-label="Output folder for this item" onchange="dlQueue[${i}].output_dir=this.value.trim()">`
      : '';
    return `<div class="queue-row">
      <span class="queue-title" title="${escapeAttr(tip)}">${tag}${escapeHtml(q.title)}</span>
      ${detail}
      ${tuneCtl}
      ${dirCtl}
      <span class="queue-remove" onclick="removeDlQueue(${i})" role="button" tabindex="0" aria-label="Remove item from queue"><svg class="icon" aria-hidden="true"><use href="#i-x"/></svg></span>
    </div>`;
  }).join('');
}

function removeDlQueue(idx) {
  dlQueue.splice(idx, 1);
  renderDlQueue();
}

// Reached only through the hold-to-confirm gesture (or a second keyboard
// press) — the queue can hold dozens of hand-picked entries and nothing here
// restores them.
function clearDlQueue() {
  const n = dlQueue.length;
  dlQueue = [];
  renderDlQueue();
  log(`Job queue cleared (${n} item${n === 1 ? '' : 's'} removed).`, 'info');
}

// ---- LOCAL CONVERSION ----
let localFiles = []; // array of {name, path, size_gb}

function probeAndSelectPreset() {
  // Smart Auto analyses the source on the server — just ensure it's selected
  autoSelectPreset();
}

async function browseLocalFiles() {
  const r = await fetch(`${API}/browse-files`);
  const d = await r.json();
  if (!d.files || d.files.length === 0) return;
  d.files.forEach(f => {
    if (!localFiles.find(x => x.path === f.path)) localFiles.push(f);
  });
  log(`Added ${d.files.length} file(s)`, 'info');
  renderLocalFiles();
  probeAndSelectPreset();
}

async function browseLocalFolder() {
  const r = await fetch(`${API}/browse-source-folder`);
  const d = await r.json();
  if (!d.path) return;
  document.getElementById('local-folder').value = d.path;
  await scanFolder();
}

async function scanFolder() {
  const folder = document.getElementById('local-folder').value.trim();
  if (!folder) return;
  const form = new FormData();
  form.append('folder', folder);
  const r = await fetch(`${API}/scan-folder`, { method: 'POST', body: form });
  const d = await r.json();
  if (d.error) { log('Error: ' + d.error, 'error'); return; }
  d.files.forEach(f => {
    if (!localFiles.find(x => x.path === f.path)) localFiles.push(f);
  });
  log(`Scanned: ${d.files.length} file(s) found in ${d.folder}`, 'info');
  renderLocalFiles();
  probeAndSelectPreset();
}

function renderLocalFiles() {
  const list = document.getElementById('local-file-list');
  const items = document.getElementById('local-file-items');
  const count = document.getElementById('local-file-count');
  if (localFiles.length === 0) { list.style.display = 'none'; return; }
  list.style.display = 'block';
  count.textContent = localFiles.length;
  items.innerHTML = localFiles.map((f, i) => `
    <div style="display:flex;justify-content:space-between;align-items:center;padding:5px 10px;border-bottom:1px solid var(--border);">
      <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeAttr(f.path)}">${escapeHtml(f.name)}</span>
      <span style="color:var(--muted);margin:0 12px;flex-shrink:0;">${f.size_gb} GB</span>
      <span style="color:var(--accent2);cursor:pointer;flex-shrink:0;" onclick="removeLocalFile(${i})" role="button" tabindex="0" aria-label="Remove file from list"><svg class="icon" aria-hidden="true"><use href="#i-x"/></svg></span>
    </div>
  `).join('');
}

function removeLocalFile(idx) {
  localFiles.splice(idx, 1);
  renderLocalFiles();
}

function clearLocalFiles() {
  localFiles = [];
  renderLocalFiles();
}

function addLocalToQueue() {
  if (localFiles.length === 0) { log('No files loaded. Browse files or scan a folder first.', 'error'); return; }
  let added = 0;
  localFiles.forEach(f => {
    if (!dlQueue.find(x => x.type === 'local' && x.path === f.path)) {
      dlQueue.push({ type: 'local', title: f.name, path: f.path, size_gb: f.size_gb });
      added++;
    }
  });
  log(`Added ${added} local file(s) to queue.`, 'info');
  renderDlQueue();
  // Clear local file list — they're in the queue now
  localFiles = [];
  renderLocalFiles();
}

async function startLocalConvert() {
  if (localFiles.length === 0) { log('No files loaded. Browse files or scan a folder first.', 'error'); return; }
  // Add to queue and start immediately
  addLocalToQueue();
  startDownload();
}

async function cancelJob() {
  // (1) Mark the whole run cancelled so the queue/retry loops stop.
  cancelled = true;
  // (2) Abort the in-flight SSE fetch/reader so runSse unwinds immediately.
  if (_currentController) { try { _currentController.abort(); } catch {} }
  // (3) Terminate server-side ffmpeg/yt-dlp.
  try { await fetch(`${API}/cancel`, { method: 'POST' }); } catch {}
  log('Cancelled.', 'error');
  document.getElementById('start-btn').classList.remove('hidden');
  document.getElementById('cancel-btn').classList.add('hidden');
  document.getElementById('phase-label').textContent = '✕ Cancelled';
  setPhaseState('error');
  stopElapsedTimer();
}
