"""Generate an interactive HTML viewer for WormScan still-image folders.

The multi-day evolution of the original make_viewer.py. Pass one or more
experiment/day folders:

  - ONE folder  -> a single strain x dose grid (identical to the original
                   viewer: loupe + click-to-pin side-by-side compare).
  - MANY folders -> the same grid per day, with arrows (and left/right keys)
                    to switch days, ordered by the YYMMDD date prefix in each
                    folder name (e.g. 260530_..._day1).

Each folder contains '<strain> <dose>J' condition subfolders, each with a
'plateNN' subfolder holding one image (or an image directly inside). Folders
whose name starts with '_' are ignored.

480px JPEG thumbnails are cached in .viewer_cache/ per folder so the grid stays
responsive even with 12MP sources; loupe and pinned-compare use the full-res
originals.

Usage:
    python make_image_viewer.py "C:\\path\\day1"
    python make_image_viewer.py "C:\\path\\day0" "C:\\path\\day1" "C:\\path\\day2"

Options:
    --out PATH   Output viewer.html path (default: parent of first folder).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}
THUMB_MAX_EDGE = 480
THUMB_QUALITY = 80
CACHE_DIRNAME = ".viewer_cache"

WT_NAMES = {"n2", "wt", "wildtype", "wild-type", "wild_type", "control", "ctrl"}

COND_RE = re.compile(r"^(?P<strain>.+?)\s+(?P<dose>\d+)\s*[Jj]$")
DATE_RE = re.compile(r"^(?P<date>\d{6})")
DAYLABEL_RE = re.compile(r"(day\s*\d+)", re.IGNORECASE)


def strain_sort_key(name: str):
    lower = name.lower().strip()
    if lower in WT_NAMES:
        return (0, lower)
    if not any(c.isdigit() for c in name):
        return (1, lower)
    return (2, lower)


def parse_condition(folder_name: str):
    m = COND_RE.match(folder_name.strip())
    if not m:
        return None
    return m.group("strain"), int(m.group("dose"))


def day_sort_key(folder: Path):
    m = DATE_RE.match(folder.name)
    if m:
        return (0, m.group("date"), folder.name.lower())
    return (1, "", folder.name.lower())


def day_label(folder: Path) -> str:
    m = DAYLABEL_RE.search(folder.name)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip().lower()
    d = DATE_RE.match(folder.name)
    if d:
        s = d.group("date")
        return f"{s[0:2]}-{s[2:4]}-{s[4:6]}"
    return folder.name


def _load_pil():
    try:
        from PIL import Image
        return Image
    except ImportError:
        return None


def find_image(condition_dir: Path) -> Path | None:
    """Highest-numbered plate folder's image; fall back to a direct image."""
    plate_dirs = [p for p in condition_dir.iterdir()
                  if p.is_dir() and p.name.lower().startswith("plate")]

    def plate_num(p: Path) -> int:
        m = re.search(r"(\d+)", p.name)
        return int(m.group(1)) if m else -1

    plate_dirs.sort(key=plate_num)
    for plate_dir in reversed(plate_dirs):
        for f in sorted(plate_dir.iterdir()):
            if f.is_file() and f.suffix.lower() in IMAGE_EXTS:
                return f
    for f in sorted(condition_dir.iterdir()):
        if f.is_file() and f.suffix.lower() in IMAGE_EXTS:
            return f
    return None


def make_thumb(src: Path, cache_root: Path, root: Path, out_dir: Path, Image):
    """Return (thumb_rel, src_rel, aspect_ratio), all relative to out_dir."""
    src_rel = _rel(src, out_dir)
    if Image is None:
        return src_rel, src_rel, 1.0
    rel = src.relative_to(root)
    key = hashlib.sha1(str(rel.as_posix()).encode("utf-8")).hexdigest()[:16]
    thumb_path = cache_root / f"{key}.jpg"
    if thumb_path.exists() and thumb_path.stat().st_mtime >= src.stat().st_mtime:
        try:
            with Image.open(thumb_path) as im:
                ar = im.size[0] / im.size[1]
        except Exception:
            ar = 1.0
        return _rel(thumb_path, out_dir), src_rel, ar
    cache_root.mkdir(parents=True, exist_ok=True)
    try:
        with Image.open(src) as im:
            ar = im.size[0] / im.size[1]
            im.thumbnail((THUMB_MAX_EDGE, THUMB_MAX_EDGE), Image.LANCZOS)
            if im.mode in ("RGBA", "P", "LA"):
                im = im.convert("RGB")
            im.save(thumb_path, "JPEG", quality=THUMB_QUALITY, optimize=True)
        return _rel(thumb_path, out_dir), src_rel, ar
    except Exception as e:
        print(f"    ! thumb failed for {rel}: {e}", file=sys.stderr)
        return src_rel, src_rel, 1.0


def _rel(p: Path, base: Path) -> str:
    """Path of p relative to base, as posix; works even across siblings."""
    import os
    return Path(os.path.relpath(p, base)).as_posix()


def build_day(day_dir: Path, out_dir: Path, Image):
    cells: dict[str, dict[int, dict]] = {}
    strain_order: list[str] = []
    dose_set: set[int] = set()
    cache_root = day_dir / CACHE_DIRNAME

    subs = sorted(p for p in day_dir.iterdir()
                  if p.is_dir() and not p.name.startswith("_")
                  and p.name != CACHE_DIRNAME)
    for sub in subs:
        parsed = parse_condition(sub.name)
        if parsed is None:
            continue
        strain, dose = parsed
        img = find_image(sub)
        if img is None:
            continue
        print(f"    {sub.name}: {img.relative_to(day_dir)}")
        thumb_rel, src_rel, ar = make_thumb(img, cache_root, day_dir, out_dir, Image)
        if strain not in cells:
            cells[strain] = {}
            strain_order.append(strain)
        cells[strain][dose] = {"src": src_rel, "thumb": thumb_rel, "ar": round(ar, 4)}
        dose_set.add(dose)

    return cells, strain_order, dose_set


def build_manifest(day_dirs: list[Path], out_dir: Path):
    Image = _load_pil()
    if Image is None:
        print("  note: Pillow not installed — using full-res images everywhere "
              "(may feel laggy). Install with:  pip install pillow")
    days_meta = []
    all_strains: list[str] = []
    all_doses: set[int] = set()

    for day_dir in day_dirs:
        print(f"  folder: {day_dir.name}")
        cells, strains, doses = build_day(day_dir, out_dir, Image)
        if not cells:
            print("    (no matching conditions — skipped)")
            continue
        for s in strains:
            if s not in all_strains:
                all_strains.append(s)
        all_doses |= doses
        days_meta.append({
            "label": day_label(day_dir),
            "folder": day_dir.name,
            "cells": cells,
        })

    all_strains.sort(key=strain_sort_key)
    return {
        "title": day_dirs[0].name if len(day_dirs) == 1 else f"{len(days_meta)} days",
        "strains": all_strains,
        "doses": sorted(all_doses),
        "days": days_meta,
    }


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>__TITLE__ — WormScan viewer</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Instrument+Serif:ital@0;1&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #0b0c0d;
  --bg-elev: #131516;
  --bg-elev-2: #1b1e20;
  --line: #2a2e31;
  --line-strong: #3a3f43;
  --text: #e7e9ea;
  --text-dim: #8a8f93;
  --text-faint: #5a5f63;
  --accent-a: #d4a574;
  --accent-b: #74c4a8;
  --bad: #c96565;
  --thumb: 240px;
  --gap: 10px;
  --loupe: 220px;
  --loupe-zoom: 3.5;
}
* { box-sizing: border-box; }
html, body {
  margin: 0; padding: 0; background: var(--bg); color: var(--text);
  font-family: 'JetBrains Mono', ui-monospace, monospace; font-size: 13px;
}
body { height: 100vh; overflow: hidden; display: flex; flex-direction: column; }

header {
  flex: 0 0 auto;
  display: flex; align-items: center; gap: 16px;
  padding: 18px 24px; border-bottom: 1px solid var(--line);
  background: var(--bg-elev);
}
header .name { font-family: 'Instrument Serif', serif; font-size: 26px; letter-spacing: 0.5px; }
header .meta { color: var(--text-dim); font-size: 12px; }
header .meta b { color: var(--text); font-weight: 500; }
header .spacer { flex: 1; }
header .hint { color: var(--text-faint); font-size: 11px; }
header .hint kbd {
  border: 1px solid var(--line-strong); padding: 1px 5px; border-radius: 3px;
  color: var(--text-dim); margin: 0 2px; font-family: inherit; font-size: 10px;
}

.btn, .seg button {
  background: transparent; border: 1px solid var(--line-strong);
  color: var(--text-dim); font-family: inherit; font-size: 11px;
  padding: 5px 10px; cursor: pointer;
  transition: color .12s, border-color .12s, background .12s;
  letter-spacing: 0.3px;
}
.btn { border-radius: 3px; display: inline-flex; align-items: center; gap: 6px; }
.btn:hover { color: var(--text); border-color: var(--text-dim); }
.btn:disabled { opacity: .35; cursor: default; }
.btn:disabled:hover { color: var(--text-dim); border-color: var(--line-strong); }
.btn svg { width: 13px; height: 13px; }
.seg { display: inline-flex; border-radius: 3px; overflow: hidden; }
.seg button { border-radius: 0; border-right-width: 0; }
.seg button:last-child { border-right-width: 1px; }
.seg button.active { color: #1a1a1a; background: var(--text-dim); border-color: var(--text-dim); }
.seg button:hover:not(.active) { color: var(--text); }

/* Day switcher */
.dayswitch { display: inline-flex; align-items: center; gap: 10px; }
.dayswitch .arrow {
  width: 30px; height: 30px; padding: 0; justify-content: center;
  font-size: 14px; line-height: 1;
}
.dayswitch .daylabel {
  font-family: 'Instrument Serif', serif; font-size: 20px;
  min-width: 110px; text-align: center; color: var(--accent-b);
  font-style: italic;
}
.dayswitch .daycount { color: var(--text-faint); font-size: 11px; }

#compare {
  flex: 0 0 auto;
  display: none; gap: 12px;
  padding: 14px 24px; border-bottom: 1px solid var(--line);
  background: var(--bg-elev-2); align-items: stretch;
}
#compare.active { display: flex; }
#compare .slot {
  flex: 1; min-width: 0;
  background: var(--bg); border: 1px solid var(--line);
  border-radius: 4px; overflow: hidden;
  display: flex; flex-direction: column;
}
#compare .slot.A { border-color: var(--accent-a); }
#compare .slot.B { border-color: var(--accent-b); }
#compare .slot-head {
  display: flex; align-items: center; gap: 10px;
  padding: 8px 12px; border-bottom: 1px solid var(--line);
  font-size: 12px; color: var(--text-dim);
}
#compare .slot.A .slot-head { border-bottom-color: rgba(212,165,116,.2); }
#compare .slot.B .slot-head { border-bottom-color: rgba(116,196,168,.2); }
#compare .slot.A .label { color: var(--accent-a); }
#compare .slot.B .label { color: var(--accent-b); }
#compare .slot-head .label { font-weight: 700; font-size: 13px; }
#compare .slot-head .x {
  margin-left: auto; cursor: pointer; color: var(--text-faint);
  padding: 2px 6px; border-radius: 3px;
  transition: color .12s, background .12s;
}
#compare .slot-head .x:hover { color: var(--bad); background: rgba(201,101,101,.08); }
#compare .slot-body {
  flex: 1; position: relative; overflow: hidden;
  display: flex; align-items: center; justify-content: center;
  min-height: 320px; background: #000;
}
#compare .slot-body img {
  max-width: 100%; max-height: 55vh; display: block;
}

main {
  flex: 1 1 auto;
  overflow: auto;
  padding: 24px;
}

.grid {
  display: grid;
  gap: var(--gap);
  align-items: stretch;
  width: max-content;
  margin: 0 auto;
}
.grid .col-head, .grid .row-head {
  letter-spacing: 0.3px;
  display: flex; align-items: center;
}
.grid .col-head {
  font-size: 14px;
  justify-content: center; padding: 8px 4px;
  border-bottom: 1px solid var(--line);
  color: var(--text); font-weight: 500;
  white-space: nowrap;
  position: sticky; top: 0;
  background: var(--bg);
  z-index: 15;
}
.grid .col-head .unit, .grid .row-head .unit {
  color: var(--text-dim); font-weight: 400; font-size: 12px; margin-left: 4px;
}
.grid .row-head {
  font-size: 14px;
  padding: 0 14px 0 12px; justify-content: flex-end; text-align: right;
  color: var(--text); font-weight: 500;
  white-space: nowrap;
  position: sticky; left: 0;
  background: var(--bg);
  z-index: 14;
}
.grid .corner {
  position: sticky;
  top: 0; left: 0;
  background: var(--bg);
  z-index: 16;
}

.cell {
  width: var(--thumb); height: var(--thumb);
  background: #000; border: 1px solid var(--line);
  position: relative; overflow: hidden;
  cursor: zoom-in;
  transition: border-color .12s;
}
.cell:hover { border-color: var(--line-strong); }
.cell.pin-a { border-color: var(--accent-a); box-shadow: 0 0 0 1px var(--accent-a) inset; }
.cell.pin-b { border-color: var(--accent-b); box-shadow: 0 0 0 1px var(--accent-b) inset; }
.cell.empty {
  cursor: default; border-style: dashed; border-color: var(--line);
  background: transparent;
  display: flex; align-items: center; justify-content: center;
  color: var(--text-faint); font-size: 13px;
}
.cell.empty:hover { border-color: var(--line); }
.cell img {
  width: 100%; height: 100%; object-fit: cover; display: block;
  pointer-events: none;
}
.cell .badge {
  position: absolute; top: 4px; right: 4px;
  color: #1a1a1a;
  font-size: 10px; font-weight: 700;
  padding: 1px 6px; border-radius: 2px;
  font-family: inherit;
}
.cell .badge.A { background: var(--accent-a); }
.cell .badge.B { background: var(--accent-b); }

.loupe {
  position: fixed; pointer-events: none;
  width: var(--loupe); height: var(--loupe);
  border: 2px solid var(--accent-a);
  border-radius: 50%;
  box-shadow: 0 0 0 1px rgba(0,0,0,.8), 0 8px 24px rgba(0,0,0,.6);
  background-repeat: no-repeat;
  background-color: #000;
  display: none; z-index: 100;
}
.loupe.active { display: block; }
</style>
</head>
<body>

<header>
  <div class="name">__TITLE__</div>
  <div class="meta"><b id="m-strains">0</b> strains · <b id="m-doses">0</b> doses · <b id="m-cells">0</b> plates</div>

  <div class="dayswitch" id="dayswitch">
    <button class="btn arrow" id="dayPrev" title="Previous day">‹</button>
    <span class="daylabel" id="dayLabel">—</span>
    <button class="btn arrow" id="dayNext" title="Next day">›</button>
    <span class="daycount" id="dayCount"></span>
  </div>

  <button class="btn" id="rotate" title="Swap rows and columns">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="16 3 20 7 16 11"/><line x1="4" y1="7" x2="20" y2="7"/><polyline points="8 21 4 17 8 13"/><line x1="20" y1="17" x2="4" y2="17"/></svg>
    <span>rotate</span>
  </button>
  <div class="seg" id="sizeSeg" title="thumbnail size">
    <button data-size="fit" class="active">fit</button>
    <button data-size="large">large</button>
  </div>
  <div class="spacer"></div>
  <div class="hint">hover <kbd>magnify</kbd> · click <kbd>pin</kbd> · <kbd>←</kbd> <kbd>→</kbd> day</div>
</header>

<div id="compare"></div>

<main>
  <div class="grid" id="grid"></div>
</main>

<div class="loupe" id="loupe"></div>

<script>
const DATA = __MANIFEST__;
const LOUPE_ZOOM = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--loupe-zoom')) || 3.5;
const LOUPE_SIZE = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--loupe')) || 220;

let orientation = (DATA.strains.length > DATA.doses.length) ? 'strains-cols' : 'strains-rows';
let sizeMode = 'fit';
const FULL_THUMB = 240;
let dayIndex = 0;

const pinned = [];
const grid = document.getElementById('grid');
const compare = document.getElementById('compare');
const loupe = document.getElementById('loupe');
const mainEl = document.querySelector('main');

function currentDay() { return DATA.days[dayIndex]; }
function cellFor(strain, dose) {
  const c = currentDay().cells;
  return c[strain] && c[strain][dose];
}

function setStats() {
  document.getElementById('m-strains').textContent = DATA.strains.length;
  document.getElementById('m-doses').textContent = DATA.doses.length;
  let n = 0;
  for (const s of DATA.strains)
    for (const d of DATA.doses)
      if (cellFor(s, d)) n++;
  document.getElementById('m-cells').textContent = n;
}

function updateDayUI() {
  const total = DATA.days.length;
  document.getElementById('dayLabel').textContent = currentDay().label;
  document.getElementById('dayCount').textContent = total > 1 ? `(${dayIndex + 1}/${total})` : '';
  document.getElementById('dayPrev').disabled = dayIndex <= 0;
  document.getElementById('dayNext').disabled = dayIndex >= total - 1;
  document.getElementById('dayswitch').style.display = total > 1 ? 'inline-flex' : 'none';
}

function gotoDay(i) {
  const total = DATA.days.length;
  const next = Math.max(0, Math.min(total - 1, i));
  if (next === dayIndex) return;
  dayIndex = next;
  pinned.length = 0;          // pins are per-day; clear on switch
  hideLoupe(); loupeCurrentCell = null;
  updateDayUI();
  setStats();
  buildGrid();
}

function fitThumbSize(nCols, nRows) {
  if (sizeMode === 'large') return FULL_THUMB;
  const mainW = mainEl.clientWidth;
  const mainH = mainEl.clientHeight;
  const pad = 48, rowHead = 120, colHead = 46, gap = 10;
  const wFit = (mainW - pad - rowHead - gap * nCols) / nCols;
  const hFit = (mainH - pad - colHead - gap * nRows) / nRows;
  return Math.max(60, Math.min(240, Math.floor(Math.min(wFit, hFit))));
}

function formatLabel(value, kind) {
  if (kind === 'strain') return escapeHtml(String(value));
  return `${value}<span class="unit">J/m²</span>`;
}

function buildGrid() {
  const colItems = orientation === 'strains-cols' ? DATA.strains : DATA.doses;
  const rowItems = orientation === 'strains-cols' ? DATA.doses : DATA.strains;
  const colKind  = orientation === 'strains-cols' ? 'strain' : 'dose';
  const rowKind  = orientation === 'strains-cols' ? 'dose'   : 'strain';

  document.documentElement.style.setProperty('--thumb', fitThumbSize(colItems.length, rowItems.length) + 'px');

  grid.innerHTML = '';
  grid.style.gridTemplateColumns = `auto repeat(${colItems.length}, var(--thumb))`;
  grid.appendChild(Object.assign(document.createElement('div'), {className: 'corner'}));

  for (const c of colItems) {
    const h = document.createElement('div');
    h.className = 'col-head';
    h.innerHTML = formatLabel(c, colKind);
    grid.appendChild(h);
  }

  for (const r of rowItems) {
    const rh = document.createElement('div');
    rh.className = 'row-head';
    rh.innerHTML = formatLabel(r, rowKind);
    grid.appendChild(rh);

    for (const c of colItems) {
      const strain = colKind === 'strain' ? c : r;
      const dose   = colKind === 'dose'   ? c : r;
      const info = cellFor(strain, dose);
      const cell = document.createElement('div');
      if (!info) {
        cell.className = 'cell empty';
        cell.textContent = '—';
      } else {
        cell.className = 'cell';
        cell.dataset.strain = strain;
        cell.dataset.dose = dose;
        cell.dataset.src = info.src;
        cell.dataset.ar = info.ar || 1;
        const img = document.createElement('img');
        img.src = info.thumb;
        img.alt = `${strain} ${dose}J`;
        img.loading = 'lazy';
        img.decoding = 'async';
        cell.appendChild(img);
        cell.addEventListener('click', () => togglePin(strain, dose, info.src, info.thumb, info.ar));
      }
      grid.appendChild(cell);
    }
  }
  renderPinned();
}

// --- Loupe ---
let loupeCurrentCell = null;

function showLoupe(src) {
  loupe.style.backgroundImage = `url("${cssEscapeUrl(src)}")`;
  loupe.classList.add('active');
}

function hideLoupe() {
  loupe.classList.remove('active');
}

function positionLoupe(ev, refCell, sourceAR) {
  const rect = refCell.getBoundingClientRect();
  const refAR = rect.width / rect.height;
  const ar = sourceAR || refAR;
  let renderedW, renderedH, offsetX, offsetY;
  if (Math.abs(refAR - ar) < 0.01) {
    renderedW = rect.width;  renderedH = rect.height;
    offsetX = 0;             offsetY = 0;
  } else if (ar > refAR) {
    renderedH = rect.height;
    renderedW = rect.height * ar;
    offsetX = (rect.width - renderedW) / 2;
    offsetY = 0;
  } else {
    renderedW = rect.width;
    renderedH = rect.width / ar;
    offsetX = 0;
    offsetY = (rect.height - renderedH) / 2;
  }
  const mx = Math.max(0, Math.min(rect.width,  ev.clientX - rect.left));
  const my = Math.max(0, Math.min(rect.height, ev.clientY - rect.top));
  const ix = mx - offsetX;
  const iy = my - offsetY;
  loupe.style.backgroundSize = `${renderedW * LOUPE_ZOOM}px ${renderedH * LOUPE_ZOOM}px`;
  loupe.style.backgroundPosition = `${-(ix * LOUPE_ZOOM - LOUPE_SIZE / 2)}px ${-(iy * LOUPE_ZOOM - LOUPE_SIZE / 2)}px`;
  loupe.style.left = (ev.clientX - LOUPE_SIZE / 2) + 'px';
  loupe.style.top  = (ev.clientY - LOUPE_SIZE / 2) + 'px';
}

function setupGridLoupe() {
  let raf = null;
  let lastEv = null;
  grid.addEventListener('mousemove', (e) => {
    lastEv = e;
    if (raf) return;
    raf = requestAnimationFrame(() => {
      raf = null;
      const ev = lastEv;
      if (!ev) return;
      const el = document.elementFromPoint(ev.clientX, ev.clientY);
      const cell = el && el.closest ? el.closest('.cell') : null;
      if (cell) {
        if (cell.classList.contains('empty') || !cell.dataset.src) {
          loupeCurrentCell = null;
          hideLoupe();
          return;
        }
        if (cell !== loupeCurrentCell) {
          loupeCurrentCell = cell;
          showLoupe(cell.dataset.src);
        }
        positionLoupe(ev, cell, parseFloat(cell.dataset.ar) || 1);
        return;
      }
      if (el === grid) {
        if (loupeCurrentCell) positionLoupe(ev, loupeCurrentCell, parseFloat(loupeCurrentCell.dataset.ar) || 1);
        return;
      }
      loupeCurrentCell = null;
      hideLoupe();
    });
  });
  grid.addEventListener('mouseleave', () => {
    loupeCurrentCell = null;
    hideLoupe();
  });
}

function attachSlotLoupe(target, fullSrc, ar) {
  let raf = null;
  let lastEv = null;
  target.addEventListener('mouseenter', () => showLoupe(fullSrc));
  target.addEventListener('mouseleave', () => {
    hideLoupe();
    if (raf) { cancelAnimationFrame(raf); raf = null; }
  });
  target.addEventListener('mousemove', (e) => {
    lastEv = e;
    if (raf) return;
    raf = requestAnimationFrame(() => {
      raf = null;
      if (!lastEv) return;
      const img = target.querySelector('img');
      positionLoupe(lastEv, img || target, ar || 1);
    });
  });
}

function cssEscapeUrl(s) { return s.replace(/"/g, '\\"'); }

function togglePin(strain, dose, src, thumb, ar) {
  const idx = pinned.findIndex(p => p.strain === strain && p.dose === dose);
  if (idx >= 0) {
    pinned.splice(idx, 1);
  } else {
    if (pinned.length >= 2) pinned.shift();
    pinned.push({strain, dose, src, thumb, ar});
  }
  renderPinned();
}

function renderPinned() {
  document.querySelectorAll('.cell').forEach(c => {
    c.classList.remove('pin-a', 'pin-b');
    const b = c.querySelector('.badge'); if (b) b.remove();
  });
  pinned.forEach((p, i) => {
    const letter = i === 0 ? 'A' : 'B';
    const cell = document.querySelector(`.cell[data-strain="${cssAttr(p.strain)}"][data-dose="${p.dose}"]`);
    if (cell) {
      cell.classList.add(i === 0 ? 'pin-a' : 'pin-b');
      const b = document.createElement('div');
      b.className = 'badge ' + letter;
      b.textContent = letter;
      cell.appendChild(b);
    }
  });
  compare.innerHTML = '';
  if (pinned.length > 0) {
    compare.classList.add('active');
    pinned.forEach((p, i) => {
      const letter = i === 0 ? 'A' : 'B';
      const slot = document.createElement('div');
      slot.className = 'slot ' + letter;
      slot.innerHTML = `
        <div class="slot-head">
          <span class="label">${letter}</span>
          <span>${escapeHtml(p.strain)} · ${p.dose} J/m²</span>
          <span class="x" title="unpin">✕</span>
        </div>
        <div class="slot-body"><img src="${escapeAttr(p.src)}" alt=""></div>
      `;
      slot.querySelector('.x').addEventListener('click', () => {
        const idx = pinned.findIndex(q => q.strain === p.strain && q.dose === p.dose);
        if (idx >= 0) pinned.splice(idx, 1);
        renderPinned();
      });
      const bigImg = slot.querySelector('.slot-body img');
      bigImg.style.pointerEvents = 'none';
      attachSlotLoupe(slot.querySelector('.slot-body'), p.src, p.ar);
      compare.appendChild(slot);
    });
  } else {
    compare.classList.remove('active');
  }
}

function cssAttr(s) { return s.replace(/"/g, '\\"'); }
function escapeHtml(s) { return s.replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function escapeAttr(s) { return s.replace(/"/g, '&quot;'); }

document.getElementById('rotate').addEventListener('click', () => {
  orientation = (orientation === 'strains-rows') ? 'strains-cols' : 'strains-rows';
  buildGrid();
});
document.querySelectorAll('#sizeSeg button').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('#sizeSeg button').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    sizeMode = btn.dataset.size;
    buildGrid();
  });
});
document.getElementById('dayPrev').addEventListener('click', () => gotoDay(dayIndex - 1));
document.getElementById('dayNext').addEventListener('click', () => gotoDay(dayIndex + 1));
window.addEventListener('keydown', (e) => {
  if (e.key === 'ArrowLeft') gotoDay(dayIndex - 1);
  else if (e.key === 'ArrowRight') gotoDay(dayIndex + 1);
});

let resizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(buildGrid, 120);
});

updateDayUI();
setStats();
setupGridLoupe();
buildGrid();
</script>
</body>
</html>
"""


def write_viewer(day_dirs: list[Path], out_path: Path) -> Path:
    out_dir = out_path.parent
    manifest = build_manifest(day_dirs, out_dir)
    if not manifest["days"]:
        raise SystemExit(
            "No matching '<strain> <dose>J' conditions found in any folder.\n"
            "Expected e.g. 'CSB 0J/plate 01/image.jpg'."
        )
    html = (HTML_TEMPLATE
            .replace("__TITLE__", manifest["title"])
            .replace("__MANIFEST__", json.dumps(manifest, ensure_ascii=False)))
    out_path.write_text(html, encoding="utf-8")
    return out_path


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Build a WormScan still-image viewer.")
    ap.add_argument("folders", nargs="+", help="One or more experiment/day folders.")
    ap.add_argument("--out", default=None, help="Output viewer.html path.")
    args = ap.parse_args(argv[1:])

    day_dirs = [Path(p).resolve() for p in args.folders]
    for d in day_dirs:
        if not d.is_dir():
            print(f"Not a directory: {d}", file=sys.stderr)
            return 1
    day_dirs.sort(key=day_sort_key)

    if args.out:
        out_path = Path(args.out).resolve()
    else:
        parent = day_dirs[0].parent
        stem = (day_dirs[0].name if len(day_dirs) == 1
                else f"{day_dirs[0].name}__{len(day_dirs)}days")
        out_path = parent / f"{stem}_viewer.html"

    print(f"Building image viewer from {len(day_dirs)} folder(s)...")
    out = write_viewer(day_dirs, out_path)
    print(f"\nWrote {out}")
    print("  Open by double-clicking (Chrome/Firefox; Safari needs Develop > "
          "Disable Local File Restrictions).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
