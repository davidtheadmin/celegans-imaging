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
  activeSessionId: null,
  activePlateId: null,
  addingPlateFor: null,
  showNewSession: false,
  thumbnails: [],
  deletedFiles: new Set(),
  magnifierActive: false,
  _magnifierTimer: null,
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
    throw new Error(detail);
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
  return el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable);
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

function openAddPlateForm() {
  if (!S.activeSessionId) return;
  S.expandedIds.add(S.activeSessionId);
  saveExpandedIds();
  S.addingPlateFor = S.activeSessionId;
  renderSessionSidebar();
  setTimeout(() => document.querySelector('.ap-cond')?.focus(), 0);
}

function toggleShortcutsOverlay() {
  const ov = document.getElementById('shortcuts-overlay');
  ov.hidden = !ov.hidden;
  if (!ov.hidden) document.getElementById('shortcuts-close').focus();
}

// ── Magnifier ─────────────────────────────────────────────────────────────────
function startMagnifier(e) {
  if (e?.preventDefault) e.preventDefault();
  if (S.magnifierActive) return;
  S.magnifierActive = true;
  document.getElementById('magnifier-overlay').hidden = false;
  magnifierLoop();
}

function stopMagnifier() {
  if (!S.magnifierActive) return;
  S.magnifierActive = false;
  document.getElementById('magnifier-overlay').hidden = true;
  clearTimeout(S._magnifierTimer);
  const img = document.getElementById('magnifier-img');
  if (img.src.startsWith('blob:')) URL.revokeObjectURL(img.src);
  img.src = '';
}

async function magnifierLoop() {
  if (!S.magnifierActive) return;
  try {
    const resp = await api('/magnifier.jpg');
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const img = document.getElementById('magnifier-img');
    const old = img.src;
    img.src = url;
    if (old.startsWith('blob:')) URL.revokeObjectURL(old);
  } catch {}
  if (S.magnifierActive) S._magnifierTimer = setTimeout(magnifierLoop, 100);
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
  btn.disabled = true;
  msg.textContent = 'Capturing…'; msg.hidden = false;
  try {
    const d = await apiJson('/capture/free/still', { method: 'POST', body: {} });
    announce(`Captured: ${d.filename}`);
    msg.textContent = d.filename;
    await refreshThumbnails();
  } catch (err) {
    announce(`Capture failed: ${err.message}`);
    msg.textContent = `Error: ${err.message}`;
  } finally {
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

// ── Session expand state (sessionStorage) ────────────────────────────────────
function saveExpandedIds() {
  try { sessionStorage.setItem('expandedIds', JSON.stringify([...S.expandedIds])); } catch {}
}

function initExpandedIds() {
  try {
    const saved = JSON.parse(sessionStorage.getItem('expandedIds') || '[]');
    S.expandedIds = new Set(saved);
  } catch { S.expandedIds = new Set(); }
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
      '<p style="padding:12px 14px;font-size:11px;color:var(--text-dim);margin:0">No sessions yet.</p>';
    return;
  }

  for (const sess of [...S.sessions].reverse()) {
    const isExpanded = S.expandedIds.has(sess.id);
    const item = document.createElement('div');
    item.className = 'session-item' + (isExpanded ? ' open' : '');
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
      <span class="mode-badge ${esc(sess.assay_mode)}">${sess.assay_mode === 'motility' ? 'MOT' : 'SRV'}</span>
      <span class="s-name" title="${esc(sess.name)}">${esc(sess.name)}</span>
      <span class="s-date">${esc(fmtDate(sess.created_at))}</span>
    `;
    hdr.addEventListener('click', () => toggleSession(sess.id));
    hdr.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleSession(sess.id); }
    });
    item.appendChild(hdr);

    if (isExpanded) {
      const sec = document.createElement('div');
      sec.className = 'plates-section';

      if (sess.plates.length === 0) {
        sec.innerHTML =
          '<p style="font-size:11px;color:var(--text-dim);margin:4px 0 6px">No plates yet.</p>';
      }

      for (const plate of sess.plates) {
        const isActive = plate.id === S.activePlateId && sess.id === S.activeSessionId;
        const el = document.createElement('div');
        el.className = 'plate-item' + (isActive ? ' active' : '');
        el.setAttribute('tabindex', '0');
        el.innerHTML = `
          <span class="plate-dot"></span>
          <span class="plate-name">${esc(plate.folder_name)}</span>
          <button class="plate-del-btn" aria-label="Delete ${esc(plate.folder_name)}" title="Delete plate">×</button>`;
        el.addEventListener('click', e => {
          if (e.target.classList.contains('plate-del-btn')) return;
          selectPlate(sess.id, plate.id);
        });
        el.addEventListener('keydown', e => { if (e.key === 'Enter') selectPlate(sess.id, plate.id); });
        el.querySelector('.plate-del-btn').addEventListener('click', e => {
          e.stopPropagation();
          confirmDeletePlate(sess.id, plate);
        });
        sec.appendChild(el);
      }

      const isAdding = S.addingPlateFor === sess.id;
      const addBtn = document.createElement('button');
      addBtn.className = 'btn btn-sm add-plate-btn';
      addBtn.innerHTML = `
        <svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.5">
          <line x1="6" y1="2" x2="6" y2="10"/><line x1="2" y1="6" x2="10" y2="6"/>
        </svg> Add plate`;
      addBtn.hidden = isAdding;
      addBtn.addEventListener('click', () => { S.addingPlateFor = sess.id; renderSessionSidebar(); });
      sec.appendChild(addBtn);

      if (isAdding) {
        const form = document.createElement('div');
        form.className = 'add-plate-form';
        const last = sess.plates.length > 0 ? sess.plates[sess.plates.length - 1] : null;
        const nextNum = last ? last.plate_number + 1 : 1;
        const prevCond = last?.condition_id ?? '';
        const prevName = last?.name ?? '';
        form.innerHTML = `
          <input class="ap-cond" type="text" placeholder="Condition ID" value="${esc(prevCond)}" required>
          <input class="ap-name" type="text" placeholder="Name" value="${esc(prevName)}" required>
          <div style="display:flex;gap:6px;align-items:center">
            <label class="field-label" style="flex-direction:row;align-items:center;gap:6px;text-transform:none;letter-spacing:0;flex:1">
              Start #<input class="ap-num mono" type="number" value="${nextNum}" min="1" style="width:52px">
            </label>
            <label class="field-label" style="flex-direction:row;align-items:center;gap:6px;text-transform:none;letter-spacing:0;flex:1">
              ×<input class="ap-rep mono" type="number" value="1" min="1" max="50" style="width:44px">
            </label>
          </div>
          <div class="ap-preview" style="font-size:10px;color:var(--text-dim);font-family:var(--mono);min-height:13px"></div>
          <div class="form-actions">
            <button class="btn btn-sm btn-primary ap-submit">Add</button>
            <button class="btn btn-sm ap-cancel">Cancel</button>
          </div>`;

        const updatePreview = () => {
          const n = parseInt(form.querySelector('.ap-num').value) || nextNum;
          const r = Math.max(1, parseInt(form.querySelector('.ap-rep').value) || 1);
          const el = form.querySelector('.ap-preview');
          el.textContent = r > 1 ? `Plates ${n} – ${n + r - 1}` : `Plate ${n}`;
        };
        form.querySelector('.ap-num').addEventListener('input', updatePreview);
        form.querySelector('.ap-rep').addEventListener('input', updatePreview);
        updatePreview();

        form.querySelector('.ap-submit').addEventListener('click', () => submitAddPlate(sess.id, form));
        form.querySelector('.ap-cancel').addEventListener('click', () => {
          S.addingPlateFor = null; renderSessionSidebar();
        });
        // Focus plate number if pre-filled, otherwise condition field
        setTimeout(() => {
          const target = prevCond ? form.querySelector('.ap-num') : form.querySelector('.ap-cond');
          target?.focus();
        }, 0);
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
}

async function submitCreateSession() {
  const name = document.getElementById('ns-name').value.trim();
  if (!name) { document.getElementById('ns-name').focus(); return; }
  const mode = document.querySelector('input[name="ns-mode"]:checked').value;
  const assay_config = mode === 'motility'
    ? { duration_s: parseInt(document.getElementById('ns-duration').value) || 30 }
    : { quadrants: document.getElementById('ns-quadrants').checked };
  try {
    const sess = await apiJson('/sessions', {
      method: 'POST', body: { name, assay_mode: mode, assay_config },
    });
    S.sessions.push(sess);
    S.expandedIds.add(sess.id);
    saveExpandedIds();
    S.showNewSession = false;
    document.getElementById('ns-name').value = '';
    renderNewSessionForm();
    renderSessionSidebar();
    announce(`Session created: ${sess.name}`);
  } catch (err) {
    announce(`Failed: ${err.message}`);
  }
}

async function submitAddPlate(sessionId, form) {
  const cond = form.querySelector('.ap-cond').value.trim();
  const name = form.querySelector('.ap-name').value.trim();
  const num = parseInt(form.querySelector('.ap-num').value) || 1;
  const replicates = Math.max(1, parseInt(form.querySelector('.ap-rep').value) || 1);
  if (!cond || !name) return;
  try {
    const updated = await apiJson(`/sessions/${sessionId}/plates`, {
      method: 'POST', body: { condition_id: cond, name, plate_number: num, replicates },
    });
    const idx = S.sessions.findIndex(s => s.id === sessionId);
    if (idx >= 0) S.sessions[idx] = updated;
    S.addingPlateFor = null;
    renderSessionSidebar();
    announce('Plate added');
  } catch (err) {
    announce(`Failed: ${err.message}`);
  }
}

async function confirmDeletePlate(sessionId, plate) {
  const n = plate.folder_name;
  const count = 0; // skip file count fetch for simplicity
  const msg = `Delete plate ${n} and all its captures?\nThis can be undone manually from .trash on the Pi.`;
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
    announce(`Plate ${n} deleted`);
  } catch (err) {
    announce(`Delete failed: ${err.message}`);
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

  } else if (sess.assay_config.quadrants) {
    body.innerHTML = `
      <div class="plate-info">
        <div class="plate-info-name">${esc(plate.folder_name)}</div>
        <div class="plate-info-mode">Survival · Quadrant</div>
      </div>
      <div class="quadrant-grid">
        <button class="btn btn-quadrant" data-q="NW">NW <span class="kbd-hint">[1]</span></button>
        <button class="btn btn-quadrant" data-q="NE">NE <span class="kbd-hint">[2]</span></button>
        <button class="btn btn-quadrant" data-q="SW">SW <span class="kbd-hint">[3]</span></button>
        <button class="btn btn-quadrant" data-q="SE">SE <span class="kbd-hint">[4]</span></button>
      </div>`;

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
  btn.disabled = true;
  try {
    await apiJson(`/sessions/${sessionId}/plates/${plateId}/capture`,
      { method: 'POST', body: {} });
    announce('Still captured');
    await refreshThumbnails();
  } catch (err) { announce(`Capture failed: ${err.message}`); }
  finally { btn.disabled = false; }
}

async function captureQuadrant(sessionId, plateId, quadrant, btn) {
  btn.disabled = true;
  try {
    await apiJson(`/sessions/${sessionId}/plates/${plateId}/capture`,
      { method: 'POST', body: { quadrant } });
    btn.classList.add('captured');
    announce(`${quadrant} captured`);
    await refreshThumbnails();
  } catch (err) {
    announce(`Capture failed: ${err.message}`);
    btn.disabled = false;
  }
}

async function markCapturedQuadrants(sessionId, plateId) {
  try {
    const files = await apiJson(`/sessions/${sessionId}/plates/${plateId}/files`);
    const captured = new Set(
      files.map(f => { const m = f.filename.match(/_([A-Z]{2})\.jpg$/i); return m ? m[1].toUpperCase() : null; })
           .filter(Boolean)
    );
    document.querySelectorAll('.btn-quadrant').forEach(btn => {
      btn.classList.toggle('captured', captured.has(btn.dataset.q));
    });
  } catch {}
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
      const raw = await apiJson(`/capture/free/files?date=${d}`);
      const base = `/capture/free/files/${d}`;
      files = raw.map(f => ({
        ...f,
        thumbUrl: `${base}/${encodeURIComponent(f.filename)}?thumb=1&token=${encodeURIComponent(S.token)}`,
        fullUrl:  `${base}/${encodeURIComponent(f.filename)}?token=${encodeURIComponent(S.token)}`,
      }));
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
    const deleted = S.deletedFiles.has(f.filename);
    const isVideo = /\.(mp4|h264|mkv)$/i.test(f.filename);

    if (deleted) {
      const tile = document.createElement('div');
      tile.className = 'thumb-tile thumb-tile--deleted';
      tile.setAttribute('aria-label', `Deleted: ${f.filename}`);
      tile.innerHTML = `<div class="thumb-tile__tomb">Deleted</div>`;
      const label = document.createElement('div');
      label.className = 'thumb-tile__label';
      label.textContent = f.filename;
      tile.appendChild(label);
      list.appendChild(tile);
      continue;
    }

    const tile = document.createElement('div');
    tile.className = 'thumb-tile';
    tile.setAttribute('tabindex', '0');
    tile.setAttribute('role', 'button');
    tile.setAttribute('aria-label', `View ${f.filename}`);

    const label = document.createElement('div');
    label.className = 'thumb-tile__label';
    label.textContent = f.filename;

    if (isVideo) {
      const img = new Image();
      img.src = f.thumbUrl;
      img.alt = f.filename;
      img.loading = 'lazy';
      img.onerror = () => img.replaceWith(createVideoIcon());
      tile.appendChild(img);
      const play = document.createElement('div');
      play.className = 'thumb-tile__play';
      play.innerHTML = `<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10" fill="rgba(0,0,0,.55)" stroke="none"/><polygon points="10,8 17,12 10,16" fill="#fff" stroke="none"/></svg>`;
      tile.appendChild(play);
    } else {
      const img = new Image();
      img.src = f.thumbUrl;
      img.alt = f.filename;
      img.loading = 'lazy';
      img.onerror = () => img.replaceWith(createVideoIcon());
      tile.appendChild(img);
    }

    // × delete button
    const delBtn = document.createElement('button');
    delBtn.className = 'thumb-tile__del';
    delBtn.setAttribute('aria-label', `Delete ${f.filename}`);
    delBtn.textContent = '×';
    delBtn.addEventListener('click', e => { e.stopPropagation(); deleteCapture(f); });
    tile.appendChild(delBtn);

    tile.appendChild(label);
    tile.addEventListener('click', () => openModal(f.fullUrl, f.filename, f));
    tile.addEventListener('keydown', e => { if (e.key === 'Enter') openModal(f.fullUrl, f.filename, f); });
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
    deleteUrl = `/capture/free/files/${fileObj.date}/${encodeURIComponent(fileObj.filename)}`;
  } else {
    deleteUrl = `/sessions/${S.activeSessionId}/plates/${S.activePlateId}/files/${encodeURIComponent(fileObj.filename)}`;
  }
  try {
    await api(deleteUrl, { method: 'DELETE' });
    S.deletedFiles.add(fileObj.filename);
    closeModal();
    renderThumbnailStrip();
    // Revert quadrant button if applicable
    const m = fileObj.filename.match(/_([A-Z]{2})\.jpg$/i);
    if (m) {
      document.querySelectorAll(`.btn-quadrant[data-q="${m[1].toUpperCase()}"]`)
        .forEach(btn => btn.classList.remove('captured'));
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

  const magBtn = document.getElementById('magnifier-btn');
  magBtn.addEventListener('mousedown', startMagnifier);
  magBtn.addEventListener('touchstart', startMagnifier, { passive: false });
  document.addEventListener('mouseup', stopMagnifier);
  document.addEventListener('touchend', stopMagnifier);

  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      if (S.magnifierActive) { stopMagnifier(); return; }
      if (!document.getElementById('modal-overlay').hidden) { closeModal(); return; }
      if (!document.getElementById('shortcuts-overlay').hidden) { toggleShortcutsOverlay(); return; }
      return;
    }
    if (isTyping()) return;
    if (e.code === 'KeyM' && !e.repeat) { e.preventDefault(); startMagnifier(); return; }
    if (e.code === 'Space') { e.preventDefault(); primaryCapture(); return; }
    if (e.key === '1') captureQuadrantByIndex(0);
    if (e.key === '2') captureQuadrantByIndex(1);
    if (e.key === '3') captureQuadrantByIndex(2);
    if (e.key === '4') captureQuadrantByIndex(3);
    if (e.code === 'KeyN') { e.preventDefault(); openAddPlateForm(); }
    if (e.code === 'KeyL') { e.preventDefault(); toggleAELock(); }
    if (e.key === '?') { e.preventDefault(); toggleShortcutsOverlay(); }
  });
  document.addEventListener('keyup', e => { if (e.code === 'KeyM') stopMagnifier(); });

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
  initPreview();
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
