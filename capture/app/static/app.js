'use strict';

// ── State ──────────────────────────────────────────────────────────────────────
const S = {
  token: null,
  connected: false,
  cameraReady: false,
  aeLocked: false,
  aeInfo: null,
  diskFreeGb: null,
  mode: 'free-still',
  sessions: [],
  expandedIds: new Set(),
  expandedConditions: new Set(),
  activeSessionId: null,
  activePlateId: null,
  addingConditionFor: null,
  addingPlatesFor: null,
  bulkAddingFor: null,
  editingConditionFor: null,
  showNewSession: false,
  thumbnails: [],
  calibFraction: 1 / 1.89,         // band WIDTH as fraction of frame width = the calibration (default FOV 1.89 cm)
  calibLeftFrac: 0.08,             // band LEFT position as fraction of frame width (cosmetic; not part of calibration)
  calibTopFrac: (1 - 0.18) / 2,    // band TOP position as fraction of frame height (cosmetic; not part of calibration)
};

// Spatial-calibration overlay constants.
const CALIB_DEFAULT_FOV = 1.89;      // cm, at max magnification
const CALIB_DEFAULT_LEFT_FRAC = 0.08; // default left position, 8% from the image's left
const CALIB_BAND_FRAC = 0.18;        // band height as a fraction of image height
const CALIB_MIN_W = 40;              // px
const CALIB_FULL_W = 4056;           // full-res still width (µm/px reference)
const CALIB_VIDEO_W = 2028;      // video width

// ── Guided (Colony Survival) capture ───────────────────────────────────────────
const GUIDED_CIRCLE_FRAC = 0.35;     // aim circle radius = this × shorter displayed dim
const G = {
  active: false,
  sessionId: null,
  queue: [],            // [{plateId, conditionLabel, plateNumber, folderName, capturedFilename}]
  index: 0,             // next plate to capture; === queue.length means complete
  inFlight: false,
};

// ── Utilities ──────────────────────────────────────────────────────────────────
function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function fmtDate(iso) {
  return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

function today() {
  return new Date().toISOString().slice(0, 10);
}

function announce(msg) {
  const el = document.getElementById('live-region');
  el.textContent = '';
  requestAnimationFrame(() => { el.textContent = msg; });
}

// ── API ────────────────────────────────────────────────────────────────────────
async function api(path, opts = {}) {
  if (!S.token) { showTokenPrompt(); throw new Error('No token'); }
  const headers = { 'X-Auth-Token': S.token, ...opts.headers };
  let body = opts.body;
  if (body !== undefined && typeof body === 'object') {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(body);
  }
  const resp = await fetch(path, { method: opts.method || 'GET', headers, body });
  if (resp.status === 401) {
    sessionStorage.removeItem('token');
    S.token = null;
    showTokenPrompt();
    throw new Error('Unauthorized');
  }
  if (!resp.ok) {
    let detail = resp.statusText;
    try { detail = (await resp.clone().json()).detail ?? detail; } catch {}
    const err = new Error(detail);
    err.status = resp.status;
    throw err;
  }
  return resp;
}

async function apiJson(path, opts = {}) {
  return (await api(path, opts)).json();
}

// ── Token management ───────────────────────────────────────────────────────────
function initToken() {
  const params = new URLSearchParams(location.search);
  const urlToken = params.get('token');
  if (urlToken) {
    sessionStorage.setItem('token', urlToken);
    params.delete('token');
    const qs = params.toString();
    history.replaceState(null, '', location.pathname + (qs ? '?' + qs : ''));
  }
  S.token = sessionStorage.getItem('token');
}

function showTokenPrompt() {
  document.getElementById('token-overlay').hidden = false;
  document.getElementById('app').hidden = true;
  document.getElementById('token-error').hidden = true;
  document.getElementById('token-input').value = '';
  setTimeout(() => document.getElementById('token-input').focus(), 50);
}

function hideTokenPrompt() {
  document.getElementById('token-overlay').hidden = true;
  document.getElementById('app').hidden = false;
}

async function handleTokenSubmit(e) {
  e.preventDefault();
  const val = document.getElementById('token-input').value.trim();
  if (!val) return;
  try {
    const r = await fetch('/status', { headers: { 'X-Auth-Token': val } });
    if (r.status === 401) throw new Error('bad token');
    S.token = val;
    sessionStorage.setItem('token', val);
    hideTokenPrompt();
    startApp();
  } catch {
    document.getElementById('token-error').hidden = false;
  }
}

// ── Preview ────────────────────────────────────────────────────────────────────
function initPreview() {
  document.getElementById('preview-img').src =
    `/preview.mjpg?token=${encodeURIComponent(S.token)}`;
}

// ── Keyboard helpers ──────────────────────────────────────────────────────────
function isTyping() {
  const el = document.activeElement;
  return el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT' || el.isContentEditable);
}

const _QUAD_ORDER = ['NW', 'NE', 'SW', 'SE'];

function primaryCapture() {
  if (!S.cameraReady) return;
  if (S.mode === 'free-still') {
    const btn = document.getElementById('still-btn');
    if (!btn.disabled) captureFreeStill();
  } else if (S.mode === 'free-video') {
    const btn = document.getElementById('video-btn');
    if (!btn.disabled) captureFreeVideo();
  } else if (S.mode === 'session' && S.activeSessionId && S.activePlateId) {
    const sess = getActiveSession();
    if (!sess) return;
    if (sess.assay_mode === 'motility') {
      const btn = document.getElementById('mot-btn');
      if (btn && !btn.disabled) captureMotility(sess.id, S.activePlateId);
    } else if (!sess.assay_config.quadrants) {
      const btn = document.getElementById('surv-btn');
      if (btn && !btn.disabled) captureSurvival(sess.id, S.activePlateId);
    }
  }
}

function captureQuadrantByIndex(idx) {
  const q = _QUAD_ORDER[idx];
  const btn = document.querySelector(`.btn-quadrant[data-q="${q}"]`);
  if (!btn || btn.disabled) return;
  const sess = getActiveSession();
  if (!sess || !sess.assay_config.quadrants) return;
  captureQuadrant(sess.id, S.activePlateId, q, btn);
}

function openAddConditionForm() {
  if (!S.activeSessionId) return;
  S.expandedIds.add(S.activeSessionId);
  saveExpandedIds();
  S.addingConditionFor = S.activeSessionId;
  renderSessionSidebar();
  setTimeout(() => document.querySelector('.ac-name')?.focus(), 0);
}

function toggleShortcutsOverlay() {
  const ov = document.getElementById('shortcuts-overlay');
  ov.hidden = !ov.hidden;
  if (!ov.hidden) document.getElementById('shortcuts-close').focus();
}

// ── AE Lock ────────────────────────────────────────────────────────────────────
async function toggleAELock() {
  const btn = document.getElementById('ae-lock-btn');
  btn.disabled = true;
  try {
    if (S.aeLocked) {
      await api('/camera/ae/unlock', { method: 'POST' });
      S.aeLocked = false;
      S.aeInfo = null;
    } else {
      S.aeInfo = await apiJson('/camera/ae/lock', { method: 'POST' });
      S.aeLocked = true;
    }
    renderAE();
  } catch (err) {
    announce(`AE toggle failed: ${err.message}`);
  } finally {
    btn.disabled = false;
  }
}

function renderAE() {
  const btn = document.getElementById('ae-lock-btn');
  const label = document.getElementById('ae-lock-label');
  const readout = document.getElementById('ae-readout');
  const chip = document.getElementById('ae-chip');

  btn.setAttribute('aria-pressed', String(S.aeLocked));
  if (S.aeLocked && S.aeInfo) {
    const exp = (S.aeInfo.exposure_us / 1000).toFixed(1);
    const gain = (S.aeInfo.analogue_gain ?? 0).toFixed(2);
    label.textContent = 'Unlock AE';
    readout.textContent = `${exp}ms ×${gain}`;
    readout.hidden = false;
    chip.textContent = `AE ${exp}ms`;
    chip.className = 'status-chip mono warn';
  } else {
    label.textContent = 'Lock AE';
    readout.hidden = true;
    chip.textContent = 'AE';
    chip.className = 'status-chip mono';
  }
}

// ── EV bias ──────────────────────────────────────────────────────────────────
let _evDebounce = null;

function _formatEv(v) {
  return (v >= 0 ? '+' : '') + Number(v).toFixed(1);
}

async function initEvBias() {
  const slider = document.getElementById('ev-slider');
  const readout = document.getElementById('ev-readout');
  try {
    const { value } = await apiJson('/camera/ev');
    slider.value = value;
    readout.textContent = _formatEv(value);
  } catch (err) {
    announce(`EV load failed: ${err.message}`);
  }
}

async function postEv(value) {
  try {
    await api('/camera/ev', { method: 'POST', body: { value } });
  } catch (err) {
    announce(`EV update failed: ${err.message}`);
  }
}

function onEvInput(e) {
  const value = parseFloat(e.target.value);
  document.getElementById('ev-readout').textContent = _formatEv(value);
  if (_evDebounce) clearTimeout(_evDebounce);
  _evDebounce = setTimeout(() => { _evDebounce = null; postEv(value); }, 150);
}

function onEvChange(e) {
  if (_evDebounce) { clearTimeout(_evDebounce); _evDebounce = null; }
  postEv(parseFloat(e.target.value));
}

// ── Spatial calibration ────────────────────────────────────────────────────────
// FOV width in cm is resolution-independent. A 1-cm band occupies
// fraction = 1/FOV_cm of the frame width, so FOV_cm = 1/fraction and
// um_per_px(width) = FOV_cm * 10000 / width.

function isCalibrating() {
  return !document.getElementById('calib-overlay').hidden;
}

function renderCalibRect() {
  const img = document.getElementById('preview-img');
  const rect = document.getElementById('calib-rect');
  const readout = document.getElementById('calib-readout');
  // Geometry is measured against the image's rendered content box, so
  // letterboxing inside .preview-wrap is handled correctly.
  const iw = img.clientWidth, ih = img.clientHeight;
  const ix = img.offsetLeft, iy = img.offsetTop;
  if (!iw || !ih) return;

  // Width fraction IS the calibration: fraction = (rightX - leftX) / imageWidth.
  // Left/top fractions only position the band over the ruler and never affect it.
  const minFrac = CALIB_MIN_W / iw;
  const widthFrac = Math.max(minFrac, Math.min(S.calibFraction, 1));
  const leftFrac = Math.max(0, Math.min(S.calibLeftFrac, 1 - widthFrac));
  const topFrac = Math.max(0, Math.min(S.calibTopFrac, 1 - CALIB_BAND_FRAC));
  S.calibFraction = widthFrac;
  S.calibLeftFrac = leftFrac;
  S.calibTopFrac = topFrac;

  const left = ix + leftFrac * iw;
  const width = widthFrac * iw;
  const height = CALIB_BAND_FRAC * ih;
  const top = iy + topFrac * ih;

  rect.style.left = `${left}px`;
  rect.style.top = `${top}px`;
  rect.style.width = `${width}px`;
  rect.style.height = `${height}px`;

  const fov = 1 / widthFrac;
  const still = 10000 / (widthFrac * CALIB_FULL_W);
  const video = 10000 / (widthFrac * CALIB_VIDEO_W);
  readout.textContent =
    `FOV: ${fov.toFixed(2)} cm · ${still.toFixed(2)} µm/px (still) · ${video.toFixed(2)} µm/px (video)`;
  readout.style.left = `${left}px`;
  readout.style.top = `${top + height + 6}px`;

  const handle = document.getElementById('calib-handle');
  handle.setAttribute('aria-valuenow', fov.toFixed(2));
  handle.setAttribute('aria-valuetext', `${fov.toFixed(2)} centimetres field of view`);
}

// Image content-box geometry in .preview-wrap pixel coordinates.
function _calibGeom() {
  const img = document.getElementById('preview-img');
  const wrapRect = document.querySelector('.preview-wrap').getBoundingClientRect();
  return {
    ix: img.offsetLeft, iy: img.offsetTop,
    iw: img.clientWidth, ih: img.clientHeight,
    wrapLeft: wrapRect.left, wrapTop: wrapRect.top,
  };
}

// Right-edge handle: drag adjusts WIDTH (left edge stays put), which changes the
// calibration. fraction = (rightX - leftX) / imageWidth.
function onCalibResizeDown(e) {
  e.preventDefault();
  e.stopPropagation();  // takes priority over the body move-drag
  const handle = e.currentTarget;
  try { handle.setPointerCapture(e.pointerId); } catch {}

  const move = (ev) => {
    const { ix, iw, wrapLeft } = _calibGeom();
    if (!iw) return;
    const leftX = ix + S.calibLeftFrac * iw;
    const px = ev.clientX - wrapLeft;
    const rightX = Math.min(Math.max(px, leftX + CALIB_MIN_W), ix + iw);
    S.calibFraction = (rightX - leftX) / iw;
    renderCalibRect();
  };
  const up = () => {
    handle.removeEventListener('pointermove', move);
    handle.removeEventListener('pointerup', up);
    handle.removeEventListener('pointercancel', up);
  };
  handle.addEventListener('pointermove', move);
  handle.addEventListener('pointerup', up);
  handle.addEventListener('pointercancel', up);
}

// Band body: drag TRANSLATES the whole rectangle on both axes, preserving width
// — so it does not change the calibration, only the band's position over the
// ruler. Vertical position is purely cosmetic.
function onCalibMoveDown(e) {
  e.preventDefault();
  const rect = e.currentTarget;
  const g0 = _calibGeom();
  if (!g0.iw || !g0.ih) return;
  // Keep the grabbed point fixed under the pointer on both axes.
  const grabX = (e.clientX - g0.wrapLeft) - (g0.ix + S.calibLeftFrac * g0.iw);
  const grabY = (e.clientY - g0.wrapTop) - (g0.iy + S.calibTopFrac * g0.ih);
  rect.classList.add('dragging');
  try { rect.setPointerCapture(e.pointerId); } catch {}

  const move = (ev) => {
    const g = _calibGeom();
    if (!g.iw || !g.ih) return;
    const newLeftX = (ev.clientX - g.wrapLeft) - grabX;
    const newTopY = (ev.clientY - g.wrapTop) - grabY;
    let leftFrac = (newLeftX - g.ix) / g.iw;
    let topFrac = (newTopY - g.iy) / g.ih;
    leftFrac = Math.max(0, Math.min(leftFrac, 1 - S.calibFraction));      // x edges stay in image
    topFrac = Math.max(0, Math.min(topFrac, 1 - CALIB_BAND_FRAC));        // y edges stay in image
    S.calibLeftFrac = leftFrac;
    S.calibTopFrac = topFrac;
    renderCalibRect();
  };
  const up = () => {
    rect.classList.remove('dragging');
    rect.removeEventListener('pointermove', move);
    rect.removeEventListener('pointerup', up);
    rect.removeEventListener('pointercancel', up);
  };
  rect.addEventListener('pointermove', move);
  rect.addEventListener('pointerup', up);
  rect.addEventListener('pointercancel', up);
}

function onCalibHandleKey(e) {
  const step = e.shiftKey ? 0.05 : 0.01;  // fraction-of-width per keypress
  let f = S.calibFraction;
  if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') f -= step;
  else if (e.key === 'ArrowRight' || e.key === 'ArrowUp') f += step;
  else return;
  e.preventDefault();
  const img = document.getElementById('preview-img');
  const iw = img.clientWidth || 1;
  const minFrac = CALIB_MIN_W / iw;
  S.calibFraction = Math.max(minFrac, Math.min(f, 1 - S.calibLeftFrac));
  renderCalibRect();
}

function renderCalibList(data) {
  const list = document.getElementById('calib-list');
  list.innerHTML = '';
  const cals = data.calibrations || [];
  if (cals.length === 0) {
    list.innerHTML = '<p class="calib-empty">No saved calibrations.</p>';
    return;
  }
  for (const c of cals) {
    const row = document.createElement('div');
    row.className = 'calib-row' + (c.label === data.active ? ' active' : '');

    const pick = document.createElement('button');
    pick.className = 'calib-row__pick';
    pick.textContent = `${c.label} · ${c.fov_cm.toFixed(2)} cm`;
    pick.title = `Use ${c.label} (${c.fov_cm.toFixed(2)} cm field of view)`;
    pick.addEventListener('click', () => selectCalibration(c.label, c.fov_cm));

    const del = document.createElement('button');
    del.className = 'calib-row__del';
    del.textContent = '×';
    del.setAttribute('aria-label', `Delete calibration ${c.label}`);
    del.addEventListener('click', () => deleteCalibrationEntry(c.label));

    row.appendChild(pick);
    row.appendChild(del);
    list.appendChild(row);
  }
}

async function openCalibration() {
  let data = { calibrations: [], active: null };
  try {
    data = await apiJson('/camera/calibration');
  } catch (err) {
    announce(`Calibration load failed: ${err.message}`);
  }
  const entry = data.active ? (data.calibrations || []).find(c => c.label === data.active) : null;
  S.calibFraction = entry ? 1 / entry.fov_cm : 1 / CALIB_DEFAULT_FOV;
  S.calibLeftFrac = CALIB_DEFAULT_LEFT_FRAC;          // start at the default x each time
  S.calibTopFrac = (1 - CALIB_BAND_FRAC) / 2;         // vertically centered each time
  document.getElementById('calib-label').value = entry ? entry.label : '';
  renderCalibList(data);

  document.getElementById('calib-overlay').hidden = false;
  document.getElementById('calib-overlay').setAttribute('aria-hidden', 'false');
  document.getElementById('calib-panel').hidden = false;
  document.getElementById('calibrate-btn').setAttribute('aria-pressed', 'true');
  renderCalibRect();
  announce('Calibration mode on');
}

function closeCalibration() {
  document.getElementById('calib-overlay').hidden = true;
  document.getElementById('calib-overlay').setAttribute('aria-hidden', 'true');
  document.getElementById('calib-panel').hidden = true;
  document.getElementById('calibrate-btn').setAttribute('aria-pressed', 'false');
}

function toggleCalibration() {
  if (isCalibrating()) closeCalibration();
  else openCalibration();
}

async function saveCalibration() {
  const input = document.getElementById('calib-label');
  const label = input.value.trim();
  if (!label) { announce('Enter a calibration name'); input.focus(); return; }
  const fov_cm = 1 / S.calibFraction;
  const btn = document.getElementById('calib-save');
  btn.disabled = true;
  try {
    const data = await apiJson('/camera/calibration', { method: 'POST', body: { label, fov_cm } });
    renderCalibList(data);
    announce(`Saved calibration ${label} (${fov_cm.toFixed(2)} cm)`);
  } catch (err) {
    announce(`Save failed: ${err.message}`);
  } finally {
    btn.disabled = false;
  }
}

async function selectCalibration(label, fovCm) {
  try {
    const data = await apiJson('/camera/calibration/active', { method: 'POST', body: { label } });
    S.calibFraction = 1 / fovCm;
    document.getElementById('calib-label').value = label;
    renderCalibRect();
    renderCalibList(data);
    announce(`Calibration ${label} active`);
  } catch (err) {
    announce(`Activation failed: ${err.message}`);
  }
}

async function deleteCalibrationEntry(label) {
  try {
    const data = await apiJson(`/camera/calibration/${encodeURIComponent(label)}`, { method: 'DELETE' });
    const entry = data.active ? (data.calibrations || []).find(c => c.label === data.active) : null;
    if (entry) {
      S.calibFraction = 1 / entry.fov_cm;
      document.getElementById('calib-label').value = entry.label;
    }
    renderCalibRect();
    renderCalibList(data);
    announce(`Deleted calibration ${label}`);
  } catch (err) {
    announce(`Delete failed: ${err.message}`);
  }
}

// ── Status polling ─────────────────────────────────────────────────────────────
let _statusInterval = null;
let _focusInterval = null;

function startPolling() {
  clearInterval(_statusInterval);
  clearInterval(_focusInterval);
  pollStatus();
  pollFocus();
  _statusInterval = setInterval(pollStatus, 10_000);
  _focusInterval = setInterval(pollFocus, 500);

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      clearInterval(_focusInterval);
    } else {
      pollFocus();
      _focusInterval = setInterval(pollFocus, 500);
    }
  });
}

async function pollStatus() {
  try {
    const h = await fetch('/health');
    const wasConnected = S.connected;
    S.connected = h.ok;
    if (!wasConnected && S.connected) announce('Reconnected to station');
  } catch {
    S.connected = false;
  }

  if (S.connected && S.token) {
    try {
      const st = await apiJson('/status');
      S.cameraReady = st.camera_ready;
      S.aeLocked = st.ae_locked;
      S.diskFreeGb = st.disk_free_gb;
    } catch {}
  }

  renderStatusBar();
}

async function pollFocus() {
  if (!S.token || !S.cameraReady) return;
  try {
    const d = await apiJson('/focus');
    document.getElementById('focus-readout').textContent = `focus ${d.score.toFixed(0)}`;
  } catch {}
}

function renderStatusBar() {
  const ind = document.getElementById('conn-indicator');
  const label = document.getElementById('conn-label');
  if (S.connected) { ind.className = 'status-dot ok'; label.textContent = 'Connected'; }
  else             { ind.className = 'status-dot err'; label.textContent = 'Disconnected'; }

  const camChip = document.getElementById('camera-chip');
  if (S.cameraReady) { camChip.textContent = 'Camera ready'; camChip.className = 'status-chip ok'; }
  else               { camChip.textContent = 'Camera offline'; camChip.className = 'status-chip err'; }

  if (S.diskFreeGb !== null) {
    const diskChip = document.getElementById('disk-chip');
    diskChip.textContent = `${S.diskFreeGb.toFixed(1)} GB`;
    diskChip.className = S.diskFreeGb > 5 ? 'status-chip mono ok'
                       : S.diskFreeGb > 2 ? 'status-chip mono warn'
                       :                    'status-chip mono err';
  }

  renderAE();
}

// ── Mode tabs ──────────────────────────────────────────────────────────────────
function switchMode(mode) {
  S.mode = mode;
  document.querySelectorAll('.tab').forEach(t => {
    const on = t.dataset.mode === mode;
    t.classList.toggle('active', on);
    t.setAttribute('aria-selected', String(on));
  });
  document.querySelectorAll('.mode-body').forEach(p => {
    p.hidden = p.dataset.panel !== mode;
  });
  refreshThumbnails();
}

// ── Free Still ─────────────────────────────────────────────────────────────────
async function captureFreeStill() {
  const btn = document.getElementById('still-btn');
  const msg = document.getElementById('still-msg');
  const origHtml = btn.innerHTML;
  btn.disabled = true;
  btn.textContent = 'Capturing…';
  msg.textContent = ''; msg.hidden = true;
  try {
    const d = await apiJson('/capture/free/still', { method: 'POST', body: {} });
    announce(`Captured: ${d.filename}`);
    msg.textContent = d.filename; msg.hidden = false;
    await refreshThumbnails();
  } catch (err) {
    announce(`Capture failed: ${err.message}`);
    msg.textContent = `Error: ${err.message}`; msg.hidden = false;
  } finally {
    btn.innerHTML = origHtml;
    btn.disabled = false;
  }
}

// ── Free Video ─────────────────────────────────────────────────────────────────
async function captureFreeVideo() {
  const btn = document.getElementById('video-btn');
  const durEl = document.getElementById('video-dur');
  const dur = Math.max(1, parseInt(durEl.value) || 5);
  const wrap = document.getElementById('video-progress');
  const bar = document.getElementById('video-bar');
  const ctr = document.getElementById('video-counter');

  btn.disabled = true; durEl.disabled = true; wrap.hidden = false;
  const t0 = Date.now();
  const tickId = setInterval(() => {
    const pct = Math.min(100, ((Date.now() - t0) / (dur * 1000)) * 100);
    const rem = Math.max(0, dur - (Date.now() - t0) / 1000);
    bar.style.width = `${pct}%`;
    ctr.textContent = `${rem.toFixed(1)}s remaining`;
  }, 100);

  try {
    const d = await apiJson('/capture/free/video', { method: 'POST', body: { duration_s: dur } });
    announce(`Recorded: ${d.filename}`);
    await refreshThumbnails();
  } catch (err) {
    announce(`Recording failed: ${err.message}`);
  } finally {
    clearInterval(tickId);
    btn.disabled = false; durEl.disabled = false; wrap.hidden = true; bar.style.width = '0%';
  }
}

function initVideoDuration() {
  const input = document.getElementById('video-dur');
  const btn = document.getElementById('video-btn');
  const upd = () => { btn.innerHTML = `Record ${parseInt(input.value) || 5}s <span class="kbd-hint">[Space]</span>`; };
  input.addEventListener('input', upd); upd();
}

// ── Session / condition expand state (sessionStorage) ────────────────────────
function saveExpandedIds() {
  try { sessionStorage.setItem('expandedIds', JSON.stringify([...S.expandedIds])); } catch {}
}
function initExpandedIds() {
  try {
    const saved = JSON.parse(sessionStorage.getItem('expandedIds') || '[]');
    S.expandedIds = new Set(saved);
  } catch { S.expandedIds = new Set(); }
}
function saveExpandedConditions() {
  try { sessionStorage.setItem('expandedConds', JSON.stringify([...S.expandedConditions])); } catch {}
}
function initExpandedConditions() {
  try {
    const saved = JSON.parse(sessionStorage.getItem('expandedConds') || '[]');
    S.expandedConditions = new Set(saved);
  } catch { S.expandedConditions = new Set(); }
}

function condKey(sessId, condId, name) { return `${sessId}:${condId}:${name}`; }

function toggleCondition(sessId, cond) {
  const k = condKey(sessId, cond.condition_id, cond.name);
  if (S.expandedConditions.has(k)) { S.expandedConditions.delete(k); }
  else { S.expandedConditions.add(k); }
  saveExpandedConditions();
  renderSessionSidebar();
}

function groupByCondition(plates) {
  const map = new Map();
  for (const plate of plates) {
    const k = condKey('', plate.condition_id, plate.name);
    if (!map.has(k)) map.set(k, { condition_id: plate.condition_id, name: plate.name, plates: [] });
    map.get(k).plates.push(plate);
  }
  return [...map.values()];
}

function condDisplay(cond) {
  const p = cond.plates[0] || {};
  return {
    strain: p.condition_name || cond.name,
    treatment: p.treatment_label || cond.condition_id,
  };
}

// Colony Survival is a survival session flagged via assay_config; capture is
// byte-identical to plain single-still survival.
function isColony(sess) {
  return sess?.assay_mode === 'survival' && !!sess.assay_config?.colony;
}

// ── Sessions ───────────────────────────────────────────────────────────────────
async function loadSessions() {
  try { S.sessions = await apiJson('/sessions'); } catch {}
  renderSessionSidebar();
}

function getActiveSession() { return S.sessions.find(s => s.id === S.activeSessionId) ?? null; }
function getActivePlate() {
  const sess = getActiveSession();
  return sess?.plates.find(p => p.id === S.activePlateId) ?? null;
}

function toggleSession(id) {
  if (S.expandedIds.has(id)) { S.expandedIds.delete(id); } else { S.expandedIds.add(id); }
  saveExpandedIds();
  S.addingPlateFor = null;
  renderSessionSidebar();
}

function selectPlate(sessionId, plateId) {
  S.activeSessionId = sessionId;
  S.activePlateId = plateId;
  S.expandedIds.add(sessionId);
  // Auto-expand the condition containing this plate
  const sess = S.sessions.find(s => s.id === sessionId);
  const plate = sess?.plates.find(p => p.id === plateId);
  if (plate) {
    S.expandedConditions.add(condKey(sessionId, plate.condition_id, plate.name));
    saveExpandedConditions();
  }
  saveExpandedIds();
  switchMode('session');
  renderSessionSidebar();
  renderSessionCapture();
  refreshThumbnails();
}

function renderSessionSidebar() {
  const list = document.getElementById('session-list');
  list.innerHTML = '';

  if (S.sessions.length === 0) {
    list.innerHTML =
      '<p style="padding:12px 14px;font-size:11px;color:var(--text-dim);margin:0">No experiments yet.</p>';
    return;
  }

  for (const sess of [...S.sessions].reverse()) {
    const isExpanded = S.expandedIds.has(sess.id);
    const item = document.createElement('div');
    item.className = 'session-item' + (isExpanded ? ' open' : '');
    if (S.activeSessionId !== null) {
      item.classList.add(sess.id === S.activeSessionId ? 'active' : 'inactive');
    }
    item.setAttribute('role', 'listitem');

    const hdr = document.createElement('div');
    hdr.className = 'session-hdr';
    hdr.setAttribute('tabindex', '0');
    hdr.setAttribute('role', 'button');
    hdr.setAttribute('aria-expanded', String(isExpanded));
    hdr.innerHTML = `
      <svg class="s-chevron" viewBox="0 0 10 10" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
        <polyline points="3,2 7,5 3,8"/>
      </svg>
      <span class="mode-badge ${isColony(sess) ? 'colony' : esc(sess.assay_mode)}">${sess.assay_mode === 'motility' ? 'MOT' : (isColony(sess) ? 'COL' : 'SRV')}</span>
      <span class="s-name" title="${esc(sess.name)}">${esc(sess.name)}</span>
      <span class="s-date">${esc(fmtDate(sess.created_at))}</span>
      <button class="sess-del-btn" aria-label="Delete experiment" title="Delete experiment">×</button>
    `;
    hdr.addEventListener('click', e => {
      if (e.target.classList.contains('sess-del-btn')) return;
      toggleSession(sess.id);
    });
    hdr.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleSession(sess.id); }
    });
    hdr.querySelector('.sess-del-btn').addEventListener('click', e => {
      e.stopPropagation();
      confirmDeleteSession(sess);
    });
    item.appendChild(hdr);

    if (isExpanded) {
      const sec = document.createElement('div');
      sec.className = 'plates-section';

      const conditions = groupByCondition(sess.plates);
      const itemLabel = sess.assay_mode === 'motility' ? 'video' : 'plate';

      if (conditions.length === 0) {
        sec.innerHTML =
          '<p style="font-size:11px;color:var(--text-dim);margin:4px 0 6px">No conditions yet.</p>';
      }

      for (const cond of conditions) {
        const condIdx = conditions.indexOf(cond);
        const display = condDisplay(cond);
        const ck = condKey(sess.id, cond.condition_id, cond.name);
        const isCondOpen = S.expandedConditions.has(ck);
        const condEl = document.createElement('div');
        condEl.className = 'cond-group' + (isCondOpen ? ' open' : '');

        const condHdr = document.createElement('div');
        condHdr.className = 'cond-hdr';
        condHdr.setAttribute('tabindex', '0');
        condHdr.setAttribute('role', 'button');
        condHdr.setAttribute('aria-expanded', String(isCondOpen));
        condHdr.innerHTML = `
          <svg class="s-chevron" viewBox="0 0 10 10" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="3,2 7,5 3,8"/>
          </svg>
          <span class="cond-label">${esc(display.strain)} / ${esc(display.treatment)}</span>
          <span class="cond-count">${cond.plates.length}</span>
          <button class="cond-up-btn" aria-label="Move condition up" title="Move up"${condIdx === 0 ? ' disabled' : ''}>▲</button>
          <button class="cond-down-btn" aria-label="Move condition down" title="Move down"${condIdx === conditions.length - 1 ? ' disabled' : ''}>▼</button>
          <button class="cond-edit-btn" aria-label="Rename condition" title="Rename condition">✎</button>
          <button class="cond-del-btn" aria-label="Delete condition" title="Delete condition">×</button>`;
        condHdr.addEventListener('click', e => {
          if (e.target.classList.contains('cond-del-btn')) return;
          toggleCondition(sess.id, cond);
        });
        condHdr.addEventListener('keydown', e => {
          if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleCondition(sess.id, cond); }
        });
        condHdr.querySelector('.cond-up-btn').addEventListener('click', e => {
          e.stopPropagation();
          moveCondition(sess, conditions, condIdx, -1);
        });
        condHdr.querySelector('.cond-down-btn').addEventListener('click', e => {
          e.stopPropagation();
          moveCondition(sess, conditions, condIdx, +1);
        });
        condHdr.querySelector('.cond-edit-btn').addEventListener('click', e => {
          e.stopPropagation();
          S.editingConditionFor = (S.editingConditionFor === ck) ? null : ck;
          renderSessionSidebar();
        });
        condHdr.querySelector('.cond-del-btn').addEventListener('click', e => {
          e.stopPropagation();
          confirmDeleteCondition(sess, cond);
        });
        condEl.appendChild(condHdr);

        if (S.editingConditionFor === ck) {
          const p0 = cond.plates[0] || {};
          const strainVal = p0.condition_name ?? cond.name;
          const treatmentVal = p0.treatment_label ?? cond.condition_id;
          const form = document.createElement('div');
          form.className = 'add-plate-form';
          form.innerHTML = `
            <input class="rc-strain" type="text" placeholder="Strain label" value="${esc(strainVal)}">
            <input class="rc-treatment" type="text" placeholder="Treatment label" value="${esc(treatmentVal)}">
            <div class="form-actions">
              <button class="btn btn-sm btn-primary rc-save">Save</button>
              <button class="btn btn-sm rc-cancel">Cancel</button>
            </div>`;
          form.querySelector('.rc-save').addEventListener('click', () => submitRenameCondition(sess.id, cond, form));
          form.querySelector('.rc-cancel').addEventListener('click', () => {
            S.editingConditionFor = null; renderSessionSidebar();
          });
          setTimeout(() => form.querySelector('.rc-strain')?.focus(), 0);
          condEl.appendChild(form);
        }

        if (isCondOpen) {
          const platesDiv = document.createElement('div');
          platesDiv.className = 'cond-plates';
          const sorted = [...cond.plates].sort((a, b) => a.plate_number - b.plate_number);
          for (const plate of sorted) {
            const isActive = plate.id === S.activePlateId && sess.id === S.activeSessionId;
            const el = document.createElement('div');
            el.className = 'plate-item' + (isActive ? ' active' : '');
            el.setAttribute('tabindex', '0');
            el.innerHTML = `
              <span class="plate-dot"></span>
              <span class="plate-name">${itemLabel} ${String(plate.plate_number).padStart(2, '0')}</span>
              <button class="plate-del-btn" aria-label="Delete ${itemLabel}" title="Delete ${itemLabel}">×</button>`;
            el.addEventListener('click', e => {
              if (e.target.classList.contains('plate-del-btn')) return;
              selectPlate(sess.id, plate.id);
            });
            el.addEventListener('keydown', e => { if (e.key === 'Enter') selectPlate(sess.id, plate.id); });
            el.querySelector('.plate-del-btn').addEventListener('click', e => {
              e.stopPropagation();
              confirmDeletePlate(sess.id, plate);
            });
            platesDiv.appendChild(el);
          }

          // "+ Add plates" inside this condition
          const aplKey = ck;
          const isAddingPlates = S.addingPlatesFor === aplKey;
          if (isAddingPlates) {
            const lastNum = Math.max(0, ...cond.plates.map(p => p.plate_number));
            const nextNum = lastNum + 1;
            const addForm = document.createElement('div');
            addForm.className = 'add-plates-form';
            addForm.innerHTML = `
              <div style="display:flex;align-items:center;gap:5px;font-size:11px;color:var(--text-dim)">
                <span>Add</span>
                <input class="apl-count mono" type="number" value="1" min="1" max="50" style="width:40px">
                <span>${itemLabel}s from #${nextNum}</span>
              </div>
              <div class="form-actions">
                <button class="btn btn-sm btn-primary apl-submit">Add</button>
                <button class="btn btn-sm apl-cancel">Cancel</button>
              </div>`;
            addForm.querySelector('.apl-submit').addEventListener('click', () => {
              const count = Math.max(1, parseInt(addForm.querySelector('.apl-count').value) || 1);
              submitAddPlatesInCondition(sess.id, cond, count);
            });
            addForm.querySelector('.apl-cancel').addEventListener('click', () => {
              S.addingPlatesFor = null; renderSessionSidebar();
            });
            setTimeout(() => addForm.querySelector('.apl-count')?.select(), 0);
            platesDiv.appendChild(addForm);
          } else {
            const addPlatesBtn = document.createElement('button');
            addPlatesBtn.className = 'btn btn-sm add-plate-btn';
            addPlatesBtn.innerHTML = `<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.5">
              <line x1="6" y1="2" x2="6" y2="10"/><line x1="2" y1="6" x2="10" y2="6"/>
            </svg> Add ${itemLabel}s`;
            addPlatesBtn.addEventListener('click', () => {
              S.addingPlatesFor = aplKey; renderSessionSidebar();
            });
            platesDiv.appendChild(addPlatesBtn);
          }
          condEl.appendChild(platesDiv);
        }
        sec.appendChild(condEl);
      }

      // "+ Add condition" button / form
      const isAddingCond = S.addingConditionFor === sess.id;
      const addCondBtn = document.createElement('button');
      addCondBtn.className = 'btn btn-sm add-plate-btn';
      addCondBtn.innerHTML = `<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.5">
        <line x1="6" y1="2" x2="6" y2="10"/><line x1="2" y1="6" x2="10" y2="6"/>
      </svg> Add condition <span class="kbd-hint">[N]</span>`;
      addCondBtn.hidden = isAddingCond;
      addCondBtn.addEventListener('click', () => { S.addingConditionFor = sess.id; renderSessionSidebar(); });
      sec.appendChild(addCondBtn);

      if (isAddingCond) {
        const form = document.createElement('div');
        form.className = 'add-plate-form';
        form.innerHTML = `
          <input class="ac-name" type="text" placeholder="Name (e.g. WT)" required>
          <input class="ac-cond" type="text" placeholder="Condition ID (e.g. 10J)" required>
          <label class="field-label" style="flex-direction:row;align-items:center;gap:6px;text-transform:none;letter-spacing:0">
            ×<input class="ac-rep mono" type="number" value="1" min="1" max="50" style="width:44px"> replicates
          </label>
          <div class="ac-preview" style="font-size:10px;color:var(--text-dim);font-family:var(--mono);min-height:13px"></div>
          <p class="ac-error form-error" hidden></p>
          <div class="form-actions">
            <button class="btn btn-sm btn-primary ac-submit">Create</button>
            <button class="btn btn-sm ac-cancel">Cancel</button>
          </div>`;

        const errEl = form.querySelector('.ac-error');
        const sessForPreview = S.sessions.find(s => s.id === sess.id);
        const previewLabel = sessForPreview?.assay_mode === 'motility' ? 'video' : 'plate';
        const updatePreview = () => {
          const n = form.querySelector('.ac-name').value.trim() || 'Name';
          const c = form.querySelector('.ac-cond').value.trim() || 'CondID';
          const r = Math.max(1, parseInt(form.querySelector('.ac-rep').value) || 1);
          form.querySelector('.ac-preview').textContent =
            r > 1 ? `${n} / ${c} — ${previewLabel}s 1 through ${r}` : `${n} / ${c} — ${previewLabel} 1`;
        };
        ['ac-name', 'ac-cond', 'ac-rep'].forEach(cls => {
          form.querySelector(`.${cls}`).addEventListener('input', () => { updatePreview(); errEl.hidden = true; });
        });
        updatePreview();
        form.querySelector('.ac-submit').addEventListener('click', () => submitAddCondition(sess.id, form));
        form.querySelector('.ac-cancel').addEventListener('click', () => {
          S.addingConditionFor = null; renderSessionSidebar();
        });
        setTimeout(() => form.querySelector('.ac-name')?.focus(), 0);
        sec.appendChild(form);
      }

      // "Bulk add" button / form
      const isBulkAdding = S.bulkAddingFor === sess.id;
      const bulkBtn = document.createElement('button');
      bulkBtn.className = 'btn btn-sm add-plate-btn';
      bulkBtn.innerHTML = `<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.5">
        <line x1="6" y1="2" x2="6" y2="10"/><line x1="2" y1="6" x2="10" y2="6"/>
      </svg> Bulk add`;
      bulkBtn.hidden = isBulkAdding;
      bulkBtn.addEventListener('click', () => { S.bulkAddingFor = sess.id; renderSessionSidebar(); });
      sec.appendChild(bulkBtn);

      if (isBulkAdding) {
        const form = document.createElement('div');
        form.className = 'add-plate-form';
        form.innerHTML = `
          <label class="field-label">Strains
            <textarea class="bulk-strains" rows="2" placeholder="N2 WT, CB1234"></textarea>
          </label>
          <label class="field-label">Treatments
            <textarea class="bulk-treatments" rows="2" placeholder="0J 10J 50J"></textarea>
          </label>
          <label class="field-label" style="flex-direction:row;align-items:center;gap:6px;text-transform:none;letter-spacing:0">
            ×<input class="bulk-rep mono" type="number" value="5" min="1" max="50" style="width:44px"> replicates per condition
          </label>
          <div class="bulk-preview" style="font-size:10px;color:var(--text-dim);font-family:var(--mono);min-height:13px"></div>
          <p class="bulk-error form-error" hidden></p>
          <div class="form-actions">
            <button class="btn btn-sm btn-primary bulk-submit">Create</button>
            <button class="btn btn-sm bulk-cancel">Cancel</button>
          </div>`;

        const updatePreview = () => {
          const strains = parseBulkList(form.querySelector('.bulk-strains').value);
          const treatments = parseBulkList(form.querySelector('.bulk-treatments').value);
          const r = Math.max(1, Math.min(50, parseInt(form.querySelector('.bulk-rep').value) || 1));
          const n = strains.length, m = treatments.length;
          form.querySelector('.bulk-preview').textContent =
            `This will create ${n} strains × ${m} treatments × ${r} replicates = ${n * m * r} plates`;
        };
        ['bulk-strains', 'bulk-treatments', 'bulk-rep'].forEach(cls => {
          form.querySelector(`.${cls}`).addEventListener('input', () => { updatePreview(); form.querySelector('.bulk-error').hidden = true; });
        });
        updatePreview();
        form.querySelector('.bulk-submit').addEventListener('click', () => submitBulkAdd(sess.id, form));
        form.querySelector('.bulk-cancel').addEventListener('click', () => {
          S.bulkAddingFor = null; renderSessionSidebar();
        });
        setTimeout(() => form.querySelector('.bulk-strains')?.focus(), 0);
        sec.appendChild(form);
      }

      item.appendChild(sec);
    }

    list.appendChild(item);
  }
}

function renderNewSessionForm() {
  const form = document.getElementById('new-session-form');
  const btn = document.getElementById('new-session-btn');
  form.hidden = !S.showNewSession;
  btn.textContent = S.showNewSession ? '✕' : 'New';
  const mode = document.querySelector('input[name="ns-mode"]:checked')?.value ?? 'motility';
  document.getElementById('ns-motility-cfg').hidden = mode !== 'motility';
  document.getElementById('ns-survival-cfg').hidden = mode !== 'survival';
  document.getElementById('ns-colony-cfg').hidden = mode !== 'colony';
}

async function submitCreateSession() {
  const name = document.getElementById('ns-name').value.trim();
  if (!name) { document.getElementById('ns-name').focus(); return; }
  const uiMode = document.querySelector('input[name="ns-mode"]:checked').value;
  // Colony Survival is plain survival on the backend (byte-identical capture),
  // distinguished only by the assay_config.colony flag the guided UI keys off.
  const assay_mode = uiMode === 'colony' ? 'survival' : uiMode;
  let assay_config;
  if (uiMode === 'motility') {
    assay_config = { duration_s: parseInt(document.getElementById('ns-duration').value) || 30 };
  } else if (uiMode === 'colony') {
    assay_config = { quadrants: false, colony: true };
  } else {
    assay_config = { quadrants: document.getElementById('ns-quadrants').checked };
  }
  try {
    const sess = await apiJson('/sessions', {
      method: 'POST', body: { name, assay_mode, assay_config },
    });
    S.sessions.push(sess);
    S.expandedIds.add(sess.id);
    saveExpandedIds();
    S.showNewSession = false;
    document.getElementById('ns-name').value = '';
    renderNewSessionForm();
    renderSessionSidebar();
    announce(`Experiment created: ${sess.name}`);
  } catch (err) {
    announce(`Failed: ${err.message}`);
  }
}

function parseBulkList(value) {
  return value.split(/[\s,]+/).map(x => x.trim()).filter(Boolean);
}

async function submitBulkAdd(sessionId, form) {
  const strains = parseBulkList(form.querySelector('.bulk-strains').value);
  const treatments = parseBulkList(form.querySelector('.bulk-treatments').value);
  const replicates = Math.max(1, Math.min(50, parseInt(form.querySelector('.bulk-rep').value) || 1));
  const errEl = form.querySelector('.bulk-error');
  if (!strains.length || !treatments.length) {
    errEl.textContent = 'Enter at least one strain and one treatment.';
    errEl.hidden = false;
    return;
  }

  const submitBtn = form.querySelector('.bulk-submit');
  submitBtn.disabled = true;

  let created = 0, skipped = 0, failed = 0;
  for (const strain of strains) {           // outer loop: strains
    for (const treatment of treatments) {   // inner loop: treatments
      try {
        await apiJson(`/sessions/${sessionId}/plates`, {
          method: 'POST',
          body: { condition_id: treatment, name: strain, condition_name: strain, plate_number: 1, replicates },
        });
        created++;
      } catch (err) {
        if (err.status === 409) {
          skipped++;
        } else {
          failed++;
          console.error(err);
        }
      }
    }
  }

  S.bulkAddingFor = null;
  await loadSessions();
  announce(`Created ${created}, skipped ${skipped} existing, ${failed} failed`);
}

async function submitAddCondition(sessionId, form) {
  const name = form.querySelector('.ac-name').value.trim();
  const cond = form.querySelector('.ac-cond').value.trim();
  const replicates = Math.max(1, parseInt(form.querySelector('.ac-rep').value) || 1);
  if (!cond || !name) return;
  const errEl = form.querySelector('.ac-error');
  const sess = S.sessions.find(s => s.id === sessionId);
  if (sess) {
    const exists = sess.plates.some(p => p.condition_id === cond && p.name === name);
    if (exists) {
      errEl.textContent = `Condition "${name} / ${cond}" already exists. Use "+ Add plates" to extend it.`;
      errEl.hidden = false;
      return;
    }
  }
  try {
    const updated = await apiJson(`/sessions/${sessionId}/plates`, {
      method: 'POST', body: { condition_id: cond, name, condition_name: name, plate_number: 1, replicates },
    });
    const idx = S.sessions.findIndex(s => s.id === sessionId);
    if (idx >= 0) S.sessions[idx] = updated;
    S.expandedConditions.add(condKey(sessionId, cond, name));
    saveExpandedConditions();
    S.addingConditionFor = null;
    renderSessionSidebar();
    announce(`Condition ${name} / ${cond} added`);
  } catch (err) {
    errEl.textContent = `Failed: ${err.message}`;
    errEl.hidden = false;
  }
}

async function submitAddPlatesInCondition(sessionId, cond, count) {
  const lastNum = Math.max(0, ...cond.plates.map(p => p.plate_number));
  const itemLabel = S.sessions.find(s => s.id === sessionId)?.assay_mode === 'motility' ? 'video' : 'plate';
  try {
    const updated = await apiJson(`/sessions/${sessionId}/plates`, {
      method: 'POST',
      body: { condition_id: cond.condition_id, name: cond.name, condition_name: cond.name, plate_number: lastNum + 1, replicates: count },
    });
    const idx = S.sessions.findIndex(s => s.id === sessionId);
    if (idx >= 0) S.sessions[idx] = updated;
    S.addingPlatesFor = null;
    renderSessionSidebar();
    announce(`${count} ${itemLabel}(s) added to ${cond.name} / ${cond.condition_id}`);
  } catch (err) {
    announce(`Failed: ${err.message}`);
  }
}

async function confirmDeletePlate(sessionId, plate) {
  const n = plate.folder_name;
  const sess = S.sessions.find(s => s.id === sessionId);
  const itemLabel = sess?.assay_mode === 'motility' ? 'video' : 'plate';
  const msg = `Delete ${itemLabel} ${n} and all its captures?\nThis can be undone manually from .trash on the Pi.`;
  if (!confirm(msg)) return;
  try {
    const updated = await apiJson(`/sessions/${sessionId}/plates/${plate.id}`, { method: 'DELETE' });
    const idx = S.sessions.findIndex(s => s.id === sessionId);
    if (idx >= 0) S.sessions[idx] = updated;
    if (S.activePlateId === plate.id && S.activeSessionId === sessionId) {
      S.activePlateId = null;
      renderSessionCapture();
    }
    renderSessionSidebar();
    announce(`${itemLabel.charAt(0).toUpperCase() + itemLabel.slice(1)} ${n} deleted`);
  } catch (err) {
    announce(`Delete failed: ${err.message}`);
  }
}

async function confirmDeleteSession(sess) {
  const plateCount = sess.plates.length;
  const countStr = plateCount === 1 ? '1 capture' : `${plateCount} captures`;
  const msg = `Delete experiment "${sess.name}" and all ${countStr}?\nThis can be undone manually from .trash on the Pi.`;
  if (!confirm(msg)) return;
  const btn = document.querySelector(`.sess-del-btn[aria-label="Delete experiment"]`);
  if (btn) btn.disabled = true;
  try {
    await api(`/sessions/${sess.id}`, { method: 'DELETE' });
    S.sessions = S.sessions.filter(s => s.id !== sess.id);
    if (S.activeSessionId === sess.id) {
      S.activeSessionId = null;
      S.activePlateId = null;
      renderSessionCapture();
    }
    renderSessionSidebar();
    announce(`Experiment "${sess.name}" deleted`);
  } catch (err) {
    if (btn) btn.disabled = false;
    announce(`Delete failed: ${err.message}`);
  }
}

async function confirmDeleteCondition(sess, cond) {
  const itemLabel = sess.assay_mode === 'motility' ? 'video' : 'plate';
  const n = cond.plates.length;
  const countStr = n === 1 ? `1 ${itemLabel}` : `${n} ${itemLabel}s`;
  const msg = `Delete condition "${cond.name} / ${cond.condition_id}" and all ${countStr}?\nThis can be undone manually from .trash on the Pi.`;
  if (!confirm(msg)) return;
  try {
    const updated = await apiJson(`/sessions/${sess.id}/conditions/${encodeURIComponent(cond.condition_id)}?name=${encodeURIComponent(cond.name)}`, { method: 'DELETE' });
    const idx = S.sessions.findIndex(s => s.id === sess.id);
    if (idx >= 0) S.sessions[idx] = updated;
    const conditionHadActivePlate = cond.plates.some(p => p.id === S.activePlateId) && S.activeSessionId === sess.id;
    if (conditionHadActivePlate) {
      S.activePlateId = null;
      renderSessionCapture();
    }
    renderSessionSidebar();
    announce(`Condition "${cond.name} / ${cond.condition_id}" deleted`);
  } catch (err) {
    announce(`Delete failed: ${err.message}`);
  }
}

async function moveCondition(sess, conditions, idx, delta) {
  const target = idx + delta;
  if (target < 0 || target >= conditions.length) return;
  // Build the full current order, then swap this condition with its neighbour.
  const order = conditions.map(c => ({ condition_id: c.condition_id, name: c.name }));
  [order[idx], order[target]] = [order[target], order[idx]];
  try {
    const updated = await apiJson(`/sessions/${sess.id}/conditions/reorder`, {
      method: 'POST', body: { order },
    });
    const i = S.sessions.findIndex(s => s.id === sess.id);
    if (i >= 0) S.sessions[i] = updated;
    renderSessionSidebar();
  } catch (err) {
    announce(`Reorder failed: ${err.message}`);
  }
}

async function submitRenameCondition(sessionId, cond, form) {
  const strain_label = form.querySelector('.rc-strain').value.trim();
  const treatment_label = form.querySelector('.rc-treatment').value.trim();
  try {
    const updated = await apiJson(
      `/sessions/${sessionId}/conditions/${encodeURIComponent(cond.condition_id)}?name=${encodeURIComponent(cond.name)}`,
      { method: 'PATCH', body: { strain_label, treatment_label } },
    );
    const idx = S.sessions.findIndex(s => s.id === sessionId);
    if (idx >= 0) S.sessions[idx] = updated;
    S.editingConditionFor = null;
    renderSessionSidebar();
    announce('Renamed');
  } catch (err) {
    announce(`Rename failed: ${err.message}`);
  }
}

// ── Session capture panel ──────────────────────────────────────────────────────
function renderSessionCapture() {
  const body = document.getElementById('session-capture-body');
  const sess = getActiveSession();
  const plate = getActivePlate();

  if (!sess || !plate) {
    body.innerHTML = '<p class="panel-hint">Select a plate in the sidebar to start capturing.</p>';
    return;
  }

  if (sess.assay_mode === 'motility') {
    const dur = sess.assay_config.duration_s ?? 30;
    body.innerHTML = `
      <div class="plate-info">
        <div class="plate-info-name">${esc(plate.folder_name)}</div>
        <div class="plate-info-mode">Motility · ${dur}s per clip</div>
      </div>
      <label class="field-label">Duration
        <div class="input-affix">
          <input id="mot-dur" type="number" value="${dur}" min="1" max="600" class="mono">
          <span class="affix-unit">s</span>
        </div>
      </label>
      <button id="mot-btn" class="btn btn-primary btn-capture">Record ${dur}s <span class="kbd-hint">[Space]</span></button>
      <div id="mot-progress" hidden>
        <div class="progress-track"><div id="mot-bar" class="progress-fill"></div></div>
        <span id="mot-ctr" class="progress-label mono"></span>
      </div>`;

    const durEl = body.querySelector('#mot-dur');
    const motBtn = body.querySelector('#mot-btn');
    durEl.addEventListener('input', () => { motBtn.innerHTML = `Record ${parseInt(durEl.value)||30}s <span class="kbd-hint">[Space]</span>`; });
    motBtn.addEventListener('click', () => captureMotility(sess.id, plate.id));

  } else if (isColony(sess)) {
    body.innerHTML = `
      <div class="plate-info">
        <div class="plate-info-name">${esc(plate.folder_name)}</div>
        <div class="plate-info-mode">Colony Survival · Single frame</div>
      </div>
      <button id="guided-start-btn" class="btn btn-primary btn-capture">Start Guided Mode</button>
      <p class="panel-hint" style="margin:8px 0 0">Walks every plate in this experiment; press Enter to capture and advance.</p>
      <button id="surv-btn" class="btn btn-capture" style="margin-top:10px">Capture this plate only <span class="kbd-hint">[Space]</span></button>`;

    body.querySelector('#guided-start-btn').addEventListener('click', () => startGuidedMode(sess.id));
    body.querySelector('#surv-btn').addEventListener('click', () => captureSurvival(sess.id, plate.id));

  } else if (sess.assay_config.quadrants) {
    body.innerHTML = `
      <div class="plate-info">
        <div class="plate-info-name">${esc(plate.folder_name)}</div>
        <div class="plate-info-mode">Survival · Quadrant</div>
      </div>
      <button id="guided-start-btn" class="btn btn-primary btn-capture">Start Guided Mode</button>
      <p class="panel-hint" style="margin:8px 0 0">Walks every quadrant of every plate; press Enter to capture and advance.</p>
      <div class="quadrant-grid">
        <button class="btn btn-quadrant" data-q="NW">NW <span class="kbd-hint">[1]</span></button>
        <button class="btn btn-quadrant" data-q="NE">NE <span class="kbd-hint">[2]</span></button>
        <button class="btn btn-quadrant" data-q="SW">SW <span class="kbd-hint">[3]</span></button>
        <button class="btn btn-quadrant" data-q="SE">SE <span class="kbd-hint">[4]</span></button>
      </div>`;

    body.querySelector('#guided-start-btn').addEventListener('click', () => startGuidedMode(sess.id));
    body.querySelectorAll('.btn-quadrant').forEach(btn => {
      btn.addEventListener('click', () => captureQuadrant(sess.id, plate.id, btn.dataset.q, btn));
    });
    markCapturedQuadrants(sess.id, plate.id);

  } else {
    body.innerHTML = `
      <div class="plate-info">
        <div class="plate-info-name">${esc(plate.folder_name)}</div>
        <div class="plate-info-mode">Survival · Single frame</div>
      </div>
      <button id="surv-btn" class="btn btn-primary btn-capture">Capture Still <span class="kbd-hint">[Space]</span></button>`;

    body.querySelector('#surv-btn').addEventListener('click', () => captureSurvival(sess.id, plate.id));
  }
}

async function captureMotility(sessionId, plateId) {
  const durEl = document.getElementById('mot-dur');
  const btn = document.getElementById('mot-btn');
  const wrap = document.getElementById('mot-progress');
  const bar = document.getElementById('mot-bar');
  const ctr = document.getElementById('mot-ctr');
  const dur = parseInt(durEl.value) || 30;

  btn.disabled = true; durEl.disabled = true; wrap.hidden = false;
  const t0 = Date.now();
  const tickId = setInterval(() => {
    const pct = Math.min(100, ((Date.now()-t0)/(dur*1000))*100);
    const rem = Math.max(0, dur-(Date.now()-t0)/1000);
    bar.style.width = `${pct}%`; ctr.textContent = `${rem.toFixed(1)}s remaining`;
  }, 100);

  try {
    await apiJson(`/sessions/${sessionId}/plates/${plateId}/capture`,
      { method: 'POST', body: { duration_s: dur } });
    announce('Motility clip recorded');
    await refreshThumbnails();
  } catch (err) {
    announce(`Recording failed: ${err.message}`);
  } finally {
    clearInterval(tickId);
    btn.disabled = false; durEl.disabled = false; wrap.hidden = true; bar.style.width = '0%';
  }
}

async function captureSurvival(sessionId, plateId) {
  const btn = document.getElementById('surv-btn');
  const origHtml = btn.innerHTML;
  btn.disabled = true;
  btn.textContent = 'Capturing…';
  try {
    await apiJson(`/sessions/${sessionId}/plates/${plateId}/capture`,
      { method: 'POST', body: {} });
    announce('Still captured');
    await refreshThumbnails();
  } catch (err) { announce(`Capture failed: ${err.message}`); }
  finally {
    btn.innerHTML = origHtml;
    btn.disabled = false;
  }
}

async function captureQuadrant(sessionId, plateId, quadrant, btn) {
  const origHtml = btn.innerHTML;
  btn.disabled = true;
  btn.textContent = `${quadrant}…`;
  try {
    await apiJson(`/sessions/${sessionId}/plates/${plateId}/capture`,
      { method: 'POST', body: { quadrant } });
    btn.innerHTML = origHtml;
    btn.classList.add('captured');
    announce(`${quadrant} captured`);
    await refreshThumbnails();
  } catch (err) {
    announce(`Capture failed: ${err.message}`);
    btn.innerHTML = origHtml;
    btn.disabled = false;
  }
}

async function markCapturedQuadrants(sessionId, plateId) {
  try {
    const files = await apiJson(`/sessions/${sessionId}/plates/${plateId}/files`);
    const captured = new Set(
      files.map(f => { const m = f.filename.match(/_([A-Z]{2})\.(tif|tiff|jpg|jpeg)$/i); return m ? m[1].toUpperCase() : null; })
           .filter(Boolean)
    );
    document.querySelectorAll('.btn-quadrant').forEach(btn => {
      btn.classList.toggle('captured', captured.has(btn.dataset.q));
    });
  } catch {}
}

// ── Guided (Colony Survival) capture ───────────────────────────────────────────
// Condition-major queue: all plates of one condition before the next condition,
// conditions in their defined (insertion) order, plates by ascending plate_number.
// To change the ordering, reorder/flatten differently here — nothing else depends
// on the traversal order.
// Worm survival with quadrants walks four quadrants per plate; colony and
// plain-still survival walk one shot per plate (quadrant: null).
function buildGuidedQueue(sess) {
  const quadMode = sess.assay_mode === 'survival' && !isColony(sess) && !!sess.assay_config?.quadrants;
  const queue = [];
  for (const cond of groupByCondition(sess.plates)) {
    const d = condDisplay(cond);
    const sorted = [...cond.plates].sort((a, b) => a.plate_number - b.plate_number);
    for (const plate of sorted) {
      const base = {
        plateId: plate.id,
        conditionLabel: `${d.strain} / ${d.treatment}`,
        plateNumber: plate.plate_number,
        folderName: plate.folder_name,
      };
      if (quadMode) {
        for (let q = 0; q < 4; q++) queue.push({ ...base, quadrant: q, capturedFilename: null });
      } else {
        queue.push({ ...base, quadrant: null, capturedFilename: null });
      }
    }
  }
  return queue;
}

function startGuidedMode(sessionId) {
  const sess = S.sessions.find(s => s.id === sessionId);
  if (!sess) return;
  const queue = buildGuidedQueue(sess);
  if (queue.length === 0) { announce('No plates to capture in this experiment'); return; }
  G.active = true;
  G.sessionId = sessionId;
  G.queue = queue;
  G.index = 0;
  G.inFlight = false;

  const ov = document.getElementById('guided-overlay');
  ov.hidden = false;
  document.getElementById('guided-preview').src =
    `/preview.mjpg?token=${encodeURIComponent(S.token)}`;
  updateGuidedBanner();
  // Draw once the overlay has laid out (and again when the first frame arrives).
  requestAnimationFrame(drawGuidedOverlay);
  const unit = queue.some(i => i.quadrant != null) ? 'quadrants' : 'plates';
  announce(`Guided mode: ${queue.length} ${unit}`);
}

function exitGuidedMode() {
  if (!G.active) return;
  G.active = false;
  const ov = document.getElementById('guided-overlay');
  ov.hidden = true;
  document.getElementById('guided-preview').src = '';  // drop the MJPEG connection
  refreshThumbnails();
  announce('Guided mode exited');
}

function updateGuidedBanner() {
  const condEl = document.getElementById('guided-cond');
  const plateEl = document.getElementById('guided-plate');
  const progEl = document.getElementById('guided-progress');
  const stateEl = document.getElementById('guided-state');
  const captureBtn = document.getElementById('guided-capture-btn');
  const redoBtn = document.getElementById('guided-redo-btn');
  const N = G.queue.length;
  const complete = G.index >= N;

  // Redo re-does the plate just captured; unavailable when nothing is captured yet.
  redoBtn.disabled = G.index === 0 || G.inFlight;

  if (complete) {
    condEl.textContent = 'Complete';
    plateEl.textContent = `${N} captured`;
    progEl.textContent = `${N} / ${N}`;
    stateEl.textContent = '';
    captureBtn.disabled = true;
    return;
  }

  const item = G.queue[G.index];
  condEl.textContent = item.conditionLabel;
  const plateText = `Plate ${String(item.plateNumber).padStart(2, '0')}`;
  plateEl.textContent = item.quadrant != null
    ? `${plateText} · quadrant ${item.quadrant + 1} of 4 (${_QUAD_ORDER[item.quadrant]})`
    : plateText;
  progEl.textContent = `${G.index + 1} / ${N}`;
  stateEl.textContent = G.inFlight ? 'Capturing…' : 'Aim, then press Enter';
  captureBtn.disabled = G.inFlight;
}

function drawGuidedOverlay() {
  const img = document.getElementById('guided-preview');
  const canvas = document.getElementById('guided-canvas');
  const w = img.clientWidth, h = img.clientHeight;
  if (!w || !h) return;
  // Track the image's rendered content box (object-fit: contain letterboxes it).
  canvas.style.left = `${img.offsetLeft}px`;
  canvas.style.top = `${img.offsetTop}px`;
  canvas.width = w;
  canvas.height = h;

  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, w, h);

  // Full-width + full-height crosshair through center — also delineates the
  // four quadrants for worm-survival guided capture.
  ctx.lineWidth = 1.5;
  ctx.strokeStyle = 'rgba(255,90,90,0.85)';
  ctx.beginPath();
  ctx.moveTo(w / 2, 0); ctx.lineTo(w / 2, h);
  ctx.moveTo(0, h / 2); ctx.lineTo(w, h / 2);
  ctx.stroke();

  // Aim circle: centered for plate/colony items; offset into the active
  // quadrant (and shrunk to fit it) for worm-survival quadrant items.
  const item = G.index < G.queue.length ? G.queue[G.index] : null;
  const quad = item && item.quadrant != null ? item.quadrant : null;
  let cx = w / 2, cy = h / 2, r = GUIDED_CIRCLE_FRAC * Math.min(w, h);
  if (quad != null) {
    cx = (quad === 1 || quad === 3) ? w * 0.75 : w * 0.25;   // NE/SE right, else left
    cy = (quad >= 2) ? h * 0.75 : h * 0.25;                  // SW/SE bottom, else top
    r = GUIDED_CIRCLE_FRAC * Math.min(w, h) / 2;
  }
  ctx.lineWidth = 2;
  ctx.strokeStyle = 'rgba(107,156,245,0.95)';
  ctx.beginPath();
  ctx.arc(cx, cy, r, 0, Math.PI * 2);
  ctx.stroke();
}

async function guidedCapture() {
  if (!G.active || G.inFlight) return;
  if (G.index >= G.queue.length) return;          // complete → no-op
  if (!S.cameraReady) { announce('Camera not ready'); return; }
  const item = G.queue[G.index];
  G.inFlight = true;
  updateGuidedBanner();
  try {
    const body = item.quadrant != null ? { quadrant: _QUAD_ORDER[item.quadrant] } : {};
    const d = await apiJson(`/sessions/${G.sessionId}/plates/${item.plateId}/capture`,
      { method: 'POST', body });
    item.capturedFilename = d.filename;           // remembered so Redo can trash it
    const where = item.quadrant != null ? ` ${_QUAD_ORDER[item.quadrant]}` : '';
    announce(`Captured ${item.conditionLabel} plate ${item.plateNumber}${where}`);
    G.index += 1;
  } catch (err) {
    announce(`Capture failed: ${err.message}`);
  } finally {
    G.inFlight = false;
    updateGuidedBanner();
    drawGuidedOverlay();                           // move the aim circle to the next quadrant
  }
}

async function guidedRedo() {
  if (!G.active || G.inFlight) return;
  if (G.index === 0) return;                       // nothing captured yet → no-op
  // Step back to the plate just captured and re-enter its aim state (works from
  // the Complete state too). Enter will recapture and advance again.
  G.index -= 1;
  const item = G.queue[G.index];
  updateGuidedBanner();
  drawGuidedOverlay();                             // aim circle back to the redone quadrant
  // Trash its recorded shot so the operator ends with one image per plate.
  const fname = item.capturedFilename;
  item.capturedFilename = null;
  if (fname) {
    try {
      await api(`/sessions/${G.sessionId}/plates/${item.plateId}/files/${encodeURIComponent(fname)}`,
        { method: 'DELETE' });
    } catch (err) {
      // A leftover duplicate is recoverable from .trash — don't strand the operator.
      announce(`Couldn't remove previous shot (${err.message}); recapture to overwrite`);
    }
  }
}

// ── Thumbnail strip ────────────────────────────────────────────────────────────
async function refreshThumbnails() {
  let files = [];
  if (S.mode === 'session' && S.activeSessionId && S.activePlateId) {
    try {
      const raw = await apiJson(`/sessions/${S.activeSessionId}/plates/${S.activePlateId}/files`);
      const base = `/sessions/${S.activeSessionId}/plates/${S.activePlateId}/files`;
      files = raw.map(f => ({
        ...f,
        thumbUrl: `${base}/${encodeURIComponent(f.filename)}?thumb=1&token=${encodeURIComponent(S.token)}`,
        fullUrl:  `${base}/${encodeURIComponent(f.filename)}?token=${encodeURIComponent(S.token)}`,
      }));
    } catch {}
  } else {
    const d = today();
    try {
      const [rawPics, rawVids] = await Promise.all([
        apiJson(`/capture/free/files?date=${d}`).catch(() => []),
        apiJson(`/capture/free/videos?date=${d}`).catch(() => []),
      ]);
      const picBase = `/capture/free/files/${d}`;
      const vidBase = `/capture/free/videos/${d}`;
      const pics = rawPics.map(f => ({
        ...f, kind: 'picture',
        thumbUrl: `${picBase}/${encodeURIComponent(f.filename)}?thumb=1&token=${encodeURIComponent(S.token)}`,
        fullUrl:  `${picBase}/${encodeURIComponent(f.filename)}?token=${encodeURIComponent(S.token)}`,
      }));
      const vids = rawVids.map(f => ({
        ...f, kind: 'video',
        thumbUrl: `${vidBase}/${encodeURIComponent(f.filename)}?thumb=1&token=${encodeURIComponent(S.token)}`,
        fullUrl:  `${vidBase}/${encodeURIComponent(f.filename)}?token=${encodeURIComponent(S.token)}`,
      }));
      files = [...pics, ...vids].sort((a, b) => a.mtime < b.mtime ? -1 : 1);
    } catch {}
  }
  S.thumbnails = files.slice(-8);
  renderThumbnailStrip();
}

function renderThumbnailStrip() {
  const list = document.getElementById('thumb-list');
  const empty = document.getElementById('thumb-empty');
  list.innerHTML = '';
  if (S.thumbnails.length === 0) { empty.hidden = false; return; }
  empty.hidden = true;

  for (const f of S.thumbnails) {
    const isVideo = /\.(mp4|h264|mkv)$/i.test(f.filename);

    const tile = document.createElement('div');
    tile.className = 'thumb-tile';
    tile.dataset.filename = f.filename;
    if (isVideo) {
      // Videos open and play in the modal — keep them as buttons.
      tile.setAttribute('tabindex', '0');
      tile.setAttribute('role', 'button');
      tile.setAttribute('aria-label', `View ${f.filename}`);
    } else {
      // Stills are TIFF (browsers can't render them) — thumbnail is display-only.
      tile.setAttribute('aria-label', `Captured image ${f.filename}`);
    }

    const label = document.createElement('div');
    label.className = 'thumb-tile__label';
    label.textContent = f.filename;

    const img = new Image();
    img.src = f.thumbUrl;
    img.alt = f.filename;
    img.loading = 'lazy';
    img.onerror = () => img.replaceWith(createVideoIcon());
    tile.appendChild(img);

    if (isVideo) {
      const play = document.createElement('div');
      play.className = 'thumb-tile__play';
      play.innerHTML = `<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10" fill="rgba(0,0,0,.55)" stroke="none"/><polygon points="10,8 17,12 10,16" fill="#fff" stroke="none"/></svg>`;
      tile.appendChild(play);
    }

    // × delete button (both kinds)
    const delBtn = document.createElement('button');
    delBtn.className = 'thumb-tile__del';
    delBtn.setAttribute('aria-label', `Delete ${f.filename}`);
    delBtn.textContent = '×';
    delBtn.addEventListener('click', e => { e.stopPropagation(); deleteCapture(f); });
    tile.appendChild(delBtn);

    tile.appendChild(label);

    if (isVideo) {
      tile.addEventListener('click', () => openModal(f.fullUrl, f.filename, f));
      tile.addEventListener('keydown', e => { if (e.key === 'Enter') openModal(f.fullUrl, f.filename, f); });
    }
    list.appendChild(tile);
  }
}

function createVideoIcon() {
  const d = document.createElement('div');
  d.className = 'thumb-tile__icon';
  d.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
    <rect x="3" y="6" width="15" height="12" rx="2"/>
    <polyline points="18,9 21,7 21,17 18,15"/>
  </svg>`;
  return d;
}

// ── Modal ──────────────────────────────────────────────────────────────────────
let _modalFile = null;

function openModal(url, alt, fileObj) {
  _modalFile = fileObj || null;
  const overlay = document.getElementById('modal-overlay');
  const img = document.getElementById('modal-img');
  const vid = document.getElementById('modal-video');
  const delBtn = document.getElementById('modal-del-btn');
  const isVideo = /\.(mp4|h264|mkv)(\?|$)/i.test(url);
  if (isVideo) {
    img.hidden = true;
    vid.hidden = false;
    vid.src = url;
  } else {
    vid.hidden = true;
    vid.src = '';
    img.hidden = false;
    img.src = url;
    img.alt = alt;
  }
  delBtn.hidden = !fileObj;
  overlay.hidden = false;
  document.getElementById('modal-close').focus();
}

function closeModal() {
  document.getElementById('modal-overlay').hidden = true;
  const vid = document.getElementById('modal-video');
  vid.pause();
  vid.src = '';
  document.getElementById('modal-img').src = '';
  _modalFile = null;
}

async function deleteCapture(fileObj) {
  if (!fileObj) return;
  let deleteUrl;
  if (fileObj.date) {
    const ns = fileObj.kind === 'video' ? 'videos' : 'files';
    deleteUrl = `/capture/free/${ns}/${fileObj.date}/${encodeURIComponent(fileObj.filename)}`;
  } else {
    deleteUrl = `/sessions/${S.activeSessionId}/plates/${S.activePlateId}/files/${encodeURIComponent(fileObj.filename)}`;
  }
  try {
    await api(deleteUrl, { method: 'DELETE' });
    closeModal();
    // Revert quadrant button if applicable
    const m = fileObj.filename.match(/_([A-Z]{2})\.(tif|tiff|jpg|jpeg)$/i);
    if (m) {
      document.querySelectorAll(`.btn-quadrant[data-q="${m[1].toUpperCase()}"]`)
        .forEach(btn => btn.classList.remove('captured'));
    }
    // Fade the tile out, then remove it from DOM and state
    const tile = document.querySelector(`.thumb-list .thumb-tile[data-filename="${CSS.escape(fileObj.filename)}"]`);
    if (tile) {
      tile.classList.add('thumb-tile--fade-out');
      tile.addEventListener('transitionend', () => {
        tile.remove();
        S.thumbnails = S.thumbnails.filter(f => f.filename !== fileObj.filename);
        if (S.thumbnails.length === 0) {
          document.getElementById('thumb-empty').hidden = false;
        }
      }, { once: true });
    }
    announce(`Deleted: ${fileObj.filename}`);
  } catch (err) {
    announce(`Delete failed: ${err.message}`);
  }
}

// ── Event binding ──────────────────────────────────────────────────────────────
function bindEvents() {
  document.getElementById('token-form').addEventListener('submit', handleTokenSubmit);

  document.querySelectorAll('.tab').forEach(tab =>
    tab.addEventListener('click', () => switchMode(tab.dataset.mode)));

  document.getElementById('ae-lock-btn').addEventListener('click', toggleAELock);

  document.getElementById('ev-slider').addEventListener('input', onEvInput);
  document.getElementById('ev-slider').addEventListener('change', onEvChange);

  document.getElementById('calibrate-btn').addEventListener('click', toggleCalibration);
  document.getElementById('calib-save').addEventListener('click', saveCalibration);
  document.getElementById('calib-done').addEventListener('click', closeCalibration);
  document.getElementById('calib-rect').addEventListener('pointerdown', onCalibMoveDown);
  document.getElementById('calib-handle').addEventListener('pointerdown', onCalibResizeDown);
  document.getElementById('calib-handle').addEventListener('keydown', onCalibHandleKey);
  document.getElementById('calib-label').addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); saveCalibration(); }
  });
  window.addEventListener('resize', () => { if (isCalibrating()) renderCalibRect(); });

  document.addEventListener('keydown', e => {
    // Guided mode owns the keyboard while active.
    if (G.active) {
      if (e.key === 'Escape') { exitGuidedMode(); return; }
      if (isTyping()) return;
      if (e.repeat) return;                                  // debounce a held key
      if (e.key === 'Enter') { e.preventDefault(); guidedCapture(); return; }
      if (e.key === 'r' || e.key === 'R') { e.preventDefault(); guidedRedo(); return; }
      return;
    }
    if (e.key === 'Escape') {
      if (!document.getElementById('modal-overlay').hidden) { closeModal(); return; }
      if (!document.getElementById('shortcuts-overlay').hidden) { toggleShortcutsOverlay(); return; }
      if (isCalibrating()) { closeCalibration(); return; }
      return;
    }
    if (isTyping()) return;
    if (e.code === 'Space') { e.preventDefault(); primaryCapture(); return; }
    if (e.key === '1') captureQuadrantByIndex(0);
    if (e.key === '2') captureQuadrantByIndex(1);
    if (e.key === '3') captureQuadrantByIndex(2);
    if (e.key === '4') captureQuadrantByIndex(3);
    if (e.code === 'KeyN') { e.preventDefault(); openAddConditionForm(); }
    if (e.code === 'KeyL') { e.preventDefault(); toggleAELock(); }
    if (e.key === '?') { e.preventDefault(); toggleShortcutsOverlay(); }
  });

  document.getElementById('still-btn').addEventListener('click', captureFreeStill);
  document.getElementById('video-btn').addEventListener('click', captureFreeVideo);
  initVideoDuration();

  document.getElementById('new-session-btn').addEventListener('click', () => {
    S.showNewSession = !S.showNewSession;
    renderNewSessionForm();
  });
  document.querySelectorAll('input[name="ns-mode"]').forEach(r =>
    r.addEventListener('change', renderNewSessionForm));
  document.getElementById('ns-submit').addEventListener('click', submitCreateSession);
  document.getElementById('ns-cancel').addEventListener('click', () => {
    S.showNewSession = false; renderNewSessionForm();
  });

  document.getElementById('guided-capture-btn').addEventListener('click', guidedCapture);
  document.getElementById('guided-redo-btn').addEventListener('click', guidedRedo);
  document.getElementById('guided-exit-btn').addEventListener('click', exitGuidedMode);
  document.getElementById('guided-preview').addEventListener('load', () => {
    if (G.active) drawGuidedOverlay();
  });
  if (window.ResizeObserver) {
    new ResizeObserver(() => { if (G.active) drawGuidedOverlay(); })
      .observe(document.getElementById('guided-preview'));
  }
  window.addEventListener('resize', () => { if (G.active) drawGuidedOverlay(); });

  document.getElementById('modal-close').addEventListener('click', closeModal);
  document.getElementById('modal-del-btn').addEventListener('click', () => deleteCapture(_modalFile));
  document.getElementById('modal-overlay').addEventListener('click', e => {
    if (e.target === e.currentTarget) closeModal();
  });
  document.getElementById('shortcuts-btn').addEventListener('click', toggleShortcutsOverlay);
  document.getElementById('shortcuts-close').addEventListener('click', toggleShortcutsOverlay);
  document.getElementById('shortcuts-overlay').addEventListener('click', e => {
    if (e.target === e.currentTarget) toggleShortcutsOverlay();
  });
}

// ── App entry ──────────────────────────────────────────────────────────────────
function startApp() {
  initExpandedIds();
  initExpandedConditions();
  initPreview();
  initEvBias();
  startPolling();
  loadSessions().then(() => refreshThumbnails());
}

window.addEventListener('DOMContentLoaded', () => {
  bindEvents();
  initToken();
  if (S.token) {
    hideTokenPrompt();
    startApp();
  } else {
    showTokenPrompt();
  }
});
