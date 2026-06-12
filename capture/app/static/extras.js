'use strict';
/* Prototype affordances layered beside the untouched app.js:
   1. A small "Display" control to taste-test the accent colour.
   2. A clock-sync chip added to the header status cluster (the launcher's
      clock warning, given a home in the web UI's status cluster).            */
(function () {

  const ACCENTS = [
    { id: 'blue',   color: '#0a84ff' },
    { id: 'indigo', color: '#5e5ce6' },
    { id: 'teal',   color: '#19c3b2' },
    { id: 'violet', color: '#bf5af0' },
  ];
  const K_ACC = 'wormscan.accent';
  const K_MODE = 'wormscan.mode';

  function getAccent() { return localStorage.getItem(K_ACC) || 'blue'; }
  function apply(accent) { document.body.setAttribute('data-accent', accent); }

  function getMode() { return localStorage.getItem(K_MODE) || 'light'; }
  function applyMode(mode) { document.body.setAttribute('data-mode', mode); }

  apply(getAccent());
  applyMode(getMode());

  // Accordion: only one experiment expanded at a time. Wraps app.js's global
  // toggleSession without modifying it — when opening a new experiment, collapse
  // the others first.
  if (typeof window.toggleSession === 'function') {
    const _origToggle = window.toggleSession;
    window.toggleSession = function (id) {
      try {
        if (typeof S !== 'undefined' && S.expandedIds && !S.expandedIds.has(id)) {
          S.expandedIds.clear();
        }
      } catch (e) {}
      return _origToggle(id);
    };
  }

  const SUN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="4.2"/><path d="M12 2.5v2.2M12 19.3v2.2M4.6 4.6l1.6 1.6M17.8 17.8l1.6 1.6M2.5 12h2.2M19.3 12h2.2M4.6 19.4l1.6-1.6M17.8 6.2l1.6-1.6"/></svg>';
  const MOON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20 14.5A8 8 0 1 1 9.5 4a6.3 6.3 0 0 0 10.5 10.5z"/></svg>';

  function buildSwitcher() {
    const isProto = !!window.__mockReset;
    const wrap = document.createElement('div');
    wrap.className = 'disp';
    wrap.innerHTML = `
      <button class="disp-toggle" id="disp-toggle" aria-expanded="false">
        <span class="disp-dot"></span><span>Display</span>
      </button>
      <div class="disp-panel" id="disp-panel" hidden>
        <div class="disp-row">
          <span class="disp-row__label">Accent</span>
          <div class="disp-swatches" id="disp-acc"></div>
        </div>
        ${isProto ? '<p class="disp-note">Live prototype \u2014 everything works except the camera feed and file writes.</p><button class="disp-reset" id="disp-reset">Reset experiment data</button>' : ''}
      </div>`;
    document.body.appendChild(wrap);

    const toggle = wrap.querySelector('#disp-toggle');
    const panel = wrap.querySelector('#disp-panel');
    const accRow = wrap.querySelector('#disp-acc');

    function renderAcc() {
      const acc = getAccent();
      accRow.innerHTML = '';
      for (const a of ACCENTS) {
        const b = document.createElement('button');
        b.style.background = a.color;
        b.style.boxShadow = `0 0 12px ${a.color}66`;
        b.title = a.id;
        b.classList.toggle('on', a.id === acc);
        b.addEventListener('click', () => {
          localStorage.setItem(K_ACC, a.id);
          apply(a.id);
          renderAcc();
        });
        accRow.appendChild(b);
      }
    }

    toggle.addEventListener('click', () => {
      const open = panel.hasAttribute('hidden');
      if (open) { panel.removeAttribute('hidden'); toggle.setAttribute('aria-expanded', 'true'); }
      else { panel.setAttribute('hidden', ''); toggle.setAttribute('aria-expanded', 'false'); }
    });
    document.addEventListener('click', (e) => {
      if (!wrap.contains(e.target)) { panel.setAttribute('hidden', ''); toggle.setAttribute('aria-expanded', 'false'); }
    });
    const resetBtn = wrap.querySelector('#disp-reset');
    if (resetBtn) resetBtn.addEventListener('click', () => {
      if (window.__mockReset) window.__mockReset();
      location.reload();
    });

    renderAcc();
  }

  function buildClockChip() {
    const cluster = document.querySelector('.hdr-status');
    if (!cluster || document.getElementById('clock-chip')) return null;
    const chip = document.createElement('span');
    chip.id = 'clock-chip';
    chip.className = 'status-chip mono';
    chip.textContent = 'clock';
    const shortcuts = document.getElementById('shortcuts-btn');
    if (shortcuts) cluster.insertBefore(chip, shortcuts);
    else cluster.appendChild(chip);
    return chip;
  }

  async function pollClock(chip) {
    try {
      const r = await fetch('/status', { headers: { 'X-Auth-Token': sessionStorage.getItem('token') || '' } });
      if (!r.ok) return;
      const st = await r.json();
      // If the backend's /status doesn't expose clock state, hide the chip rather
      // than show a false "stale" warning.
      if (!('clock_synced' in st)) { chip.style.display = 'none'; return; }
      chip.style.display = '';
      if (st.clock_synced) {
        const d = new Date(Date.now() - (st.clock_offset_min || 0) * 60000);
        const hh = String(d.getHours()).padStart(2, '0');
        const mm = String(d.getMinutes()).padStart(2, '0');
        chip.textContent = `clock ${hh}:${mm}`;
        chip.className = 'status-chip mono';
        chip.title = 'Pi clock synced (NTP) · drift within tolerance';
      } else {
        chip.textContent = 'CLOCK STALE';
        chip.className = 'status-chip mono err';
        chip.title = 'Raspberry Pi has no battery clock — time has not synced. Captured timestamps may be wrong.';
      }
    } catch (e) {}
  }

  function buildModeToggle() {
    const cluster = document.querySelector('.hdr-status');
    if (!cluster || document.getElementById('mode-toggle')) return;
    const btn = document.createElement('button');
    btn.id = 'mode-toggle';
    btn.type = 'button';
    const render = () => {
      const m = getMode();
      btn.innerHTML = m === 'light' ? MOON : SUN;
      btn.setAttribute('aria-label', m === 'light' ? 'Switch to dark mode' : 'Switch to light mode');
      btn.title = m === 'light' ? 'Switch to dark mode' : 'Switch to light mode';
    };
    btn.addEventListener('click', () => {
      const next = getMode() === 'light' ? 'dark' : 'light';
      localStorage.setItem(K_MODE, next);
      applyMode(next);
      render();
    });
    render();
    const shortcuts = document.getElementById('shortcuts-btn');
    if (shortcuts) cluster.insertBefore(btn, shortcuts);
    else cluster.appendChild(btn);
  }

  // On load, enforce a single open experiment (collapse any extras from a prior session).
  function enforceSingleOpen() {
    try {
      if (typeof S !== 'undefined' && S.expandedIds && S.expandedIds.size > 1) {
        const keep = S.expandedIds.has(S.activeSessionId) ? S.activeSessionId : [...S.expandedIds][0];
        S.expandedIds.clear(); S.expandedIds.add(keep);
        if (typeof saveExpandedIds === 'function') saveExpandedIds();
        if (typeof renderSessionSidebar === 'function') renderSessionSidebar();
      }
    } catch (e) {}
  }

  function init() {
    enforceSingleOpen();
    buildSwitcher();
    buildModeToggle();
    const chip = buildClockChip();
    if (chip) { pollClock(chip); setInterval(() => pollClock(chip), 10000); }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
