"""Generate an interactive HTML viewer for WormScan crawling-video folders.

The video analog of make_viewer.py. Instead of one experiment folder of still
images, you pass one or more *day* folders. Each day folder contains
'<strain> <dose>J' condition subfolders, each with a 'plateNN' subfolder
holding one .mp4. Folders whose name starts with '_' (e.g.
'_crawling_analysis_...') are ignored.

For every condition the generator picks the highest-numbered plate, then
pre-transcodes its 3-minute video into a short (~3 s) fast loop clip cached in
.viewer_cache/. The result is a single self-contained viewer.html showing a
strain x dose grid of looping clips, with arrows to switch between days
(ordered by the YYMMDD date prefix in each day-folder name).

Usage:
    python make_video_viewer.py "C:\\path\\day1" "C:\\path\\day2" ...
    python make_video_viewer.py "C:\\path\\day1"        # single day, no arrows
    # The viewer.html is written next to the FIRST day folder's parent
    # (or pass --out to choose).

Options:
    --target-seconds N   Approx length of each sped-up loop clip (default 3.0).
    --out PATH           Where to write viewer.html (default: parent of first
                         day folder, named '<...>_video_viewer.html').
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".m4v"}
CACHE_DIRNAME = ".viewer_cache"
DEFAULT_TARGET_SECONDS = 3.0
CLIP_MAX_EDGE = 480          # downscale long edge of the loop clip for grid use
CRF = 26                     # loop clips are tiny + looping; 26 is plenty
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# Same condition grammar as the still viewer: "<strain> <dose>J"
COND_RE = re.compile(r"^(?P<strain>.+?)\s+(?P<dose>\d+)\s*[Jj]$")

# Leading YYMMDD (6 digits) used to order day folders, e.g. 260530_Crawling_day1
DATE_RE = re.compile(r"^(?P<date>\d{6})")
# A friendly day label: prefer a trailing 'dayN', else the date itself.
DAYLABEL_RE = re.compile(r"(day\s*\d+)", re.IGNORECASE)

WT_NAMES = {"n2", "wt", "wildtype", "wild-type", "wild_type", "control", "ctrl"}


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
    """Order days by leading YYMMDD; folders without one sort last by name."""
    m = DATE_RE.match(folder.name)
    if m:
        return (0, m.group("date"), folder.name.lower())
    return (1, "", folder.name.lower())


def day_label(folder: Path) -> str:
    m = DAYLABEL_RE.search(folder.name)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip().lower().replace("day ", "day ")
    d = DATE_RE.match(folder.name)
    if d:
        s = d.group("date")
        return f"{s[0:2]}-{s[2:4]}-{s[4:6]}"
    return folder.name


def pick_plate_video(condition_dir: Path) -> Path | None:
    """Highest-numbered plate folder's video; fall back to a direct video."""
    plate_dirs = [p for p in condition_dir.iterdir()
                  if p.is_dir() and p.name.lower().startswith("plate")]

    def plate_num(p: Path) -> int:
        m = re.search(r"(\d+)", p.name)
        return int(m.group(1)) if m else -1

    plate_dirs.sort(key=plate_num)
    for plate_dir in reversed(plate_dirs):   # highest plate first
        vids = sorted(f for f in plate_dir.iterdir()
                      if f.is_file() and f.suffix.lower() in VIDEO_EXTS)
        if vids:
            return vids[0]
    # No plate subfolder — accept a video sitting directly in the condition dir
    vids = sorted(f for f in condition_dir.iterdir()
                  if f.is_file() and f.suffix.lower() in VIDEO_EXTS)
    return vids[0] if vids else None


def ffprobe_duration(src: Path) -> float | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nokey=1:noprint_wrappers=1", str(src)],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
        )
        return float(out.stdout.strip())
    except Exception:
        return None


def make_loop_clip(src: Path, cache_root: Path, target_seconds: float) -> Path | None:
    """Transcode src into a short fast-playback loop clip. Cached by src mtime."""
    key = hashlib.sha1(str(src.resolve()).encode("utf-8")).hexdigest()[:16]
    clip_path = cache_root / f"{key}.mp4"
    if clip_path.exists() and clip_path.stat().st_mtime >= src.stat().st_mtime:
        return clip_path

    dur = ffprobe_duration(src)
    if not dur or dur <= 0:
        print(f"  ! could not probe duration: {src.name}", file=sys.stderr)
        return None
    # setpts factor: new_duration = dur * factor  ->  factor = target/dur
    factor = max(target_seconds / dur, 1e-4)

    cache_root.mkdir(parents=True, exist_ok=True)
    # Scale long edge to CLIP_MAX_EDGE, force even dims (libx264 + yuv420p).
    vf = (f"setpts={factor:.6f}*PTS,"
          f"scale='if(gt(iw,ih),{CLIP_MAX_EDGE},-2)':'if(gt(iw,ih),-2,{CLIP_MAX_EDGE})',"
          f"scale=trunc(iw/2)*2:trunc(ih/2)*2")
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-an",                       # drop audio
        "-vf", vf,
        "-r", "30",                  # normalise output fps
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(CRF),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(clip_path),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           creationflags=CREATE_NO_WINDOW)
        if r.returncode != 0:
            print(f"  ! ffmpeg failed for {src.name}: {r.stderr[-300:]}",
                  file=sys.stderr)
            return None
    except Exception as e:
        print(f"  ! ffmpeg error for {src.name}: {e}", file=sys.stderr)
        return None
    return clip_path


def build_day(day_dir: Path, cache_root: Path, out_dir: Path,
              target_seconds: float):
    """Return (cells, strains, doses) for a single day folder."""
    cells: dict[str, dict[int, dict]] = {}
    strain_order: list[str] = []
    dose_set: set[int] = set()

    subs = sorted(p for p in day_dir.iterdir()
                  if p.is_dir() and not p.name.startswith("_")
                  and p.name != CACHE_DIRNAME)
    for sub in subs:
        parsed = parse_condition(sub.name)
        if parsed is None:
            continue
        strain, dose = parsed
        src = pick_plate_video(sub)
        if src is None:
            continue
        print(f"    {sub.name}: {src.relative_to(day_dir)}")
        clip = make_loop_clip(src, cache_root, target_seconds)
        if clip is None:
            continue
        clip_rel = clip.relative_to(out_dir).as_posix()
        if strain not in cells:
            cells[strain] = {}
            strain_order.append(strain)
        cells[strain][dose] = {"clip": clip_rel}
        dose_set.add(dose)

    return cells, strain_order, dose_set


def build_manifest(day_dirs: list[Path], out_dir: Path, target_seconds: float):
    days_meta = []
    all_strains: list[str] = []
    all_doses: set[int] = set()

    for day_dir in day_dirs:
        cache_root = day_dir / CACHE_DIRNAME
        print(f"  day: {day_dir.name}")
        cells, strains, doses = build_day(day_dir, cache_root, out_dir,
                                          target_seconds)
        if not cells:
            print(f"    (no matching conditions — skipped)")
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


# ----------------------------------------------------------------------------
# HTML — matches make_viewer.py's look (dark, JetBrains Mono / Instrument Serif,
# sticky strain x dose grid, fit/large size toggle, rotate, live loupe). The
# pin/compare machinery is removed; day-switching arrows are added.
# ----------------------------------------------------------------------------

HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>__TITLE__ — WormScan video viewer</title>
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
  --loupe: 260px;
  --loupe-zoom: 3.0;
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
.cell.empty {
  cursor: default; border-style: dashed; border-color: var(--line);
  background: transparent;
  display: flex; align-items: center; justify-content: center;
  color: var(--text-faint); font-size: 13px;
}
.cell.empty:hover { border-color: var(--line); }
.cell video {
  width: 100%; height: 100%; object-fit: cover; display: block;
  pointer-events: none;
}

/* Live loupe: a magnified video that tracks the cursor. */
.loupe {
  position: fixed; pointer-events: none;
  width: var(--loupe); height: var(--loupe);
  border: 2px solid var(--accent-a);
  border-radius: 50%;
  box-shadow: 0 0 0 1px rgba(0,0,0,.8), 0 8px 24px rgba(0,0,0,.6);
  overflow: hidden;
  background: #000;
  display: none; z-index: 100;
}
.loupe.active { display: block; }
.loupe canvas {
  position: absolute; left: 0; top: 0;
  width: 100%; height: 100%;
  display: block;
}
</style>
</head>
<body>

<header>
  <div class="name">__TITLE__</div>
  <div class="meta"><b id="m-strains">0</b> strains · <b id="m-doses">0</b> doses · <b id="m-cells">0</b> clips</div>

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
  <div class="hint">hover <kbd>magnify</kbd> · <kbd>←</kbd> <kbd>→</kbd> switch day</div>
</header>

<main>
  <div class="grid" id="grid"></div>
</main>

<div class="loupe" id="loupe"><canvas id="loupeCanvas"></canvas></div>

<script>
const DATA = __MANIFEST__;
const LOUPE_ZOOM = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--loupe-zoom')) || 3.0;
const LOUPE_SIZE = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--loupe')) || 260;

let orientation = (DATA.strains.length > DATA.doses.length) ? 'strains-cols' : 'strains-rows';
let sizeMode = 'fit';
const FULL_THUMB = 240;
let dayIndex = 0;

const grid = document.getElementById('grid');
const loupe = document.getElementById('loupe');
const loupeCanvas = document.getElementById('loupeCanvas');
const loupeCtx = loupeCanvas.getContext('2d');
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
  dayIndex = Math.max(0, Math.min(total - 1, i));
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
        cell.dataset.clip = info.clip;
        const v = document.createElement('video');
        v.src = info.clip;
        v.muted = true; v.loop = true; v.autoplay = true;
        v.playsInline = true; v.setAttribute('playsinline', '');
        v.preload = 'auto';
        cell.appendChild(v);
        // best-effort autoplay kick (some browsers need the explicit call)
        v.play && v.play().catch(() => {});
      }
      grid.appendChild(cell);
    }
  }
}

// --- Live loupe ---
// A circular canvas that, each animation frame, draws a magnified crop from the
// cell's already-playing <video> (which is guaranteed to be decoding because
// it's visible). No second decode, no seek-sync, never goes black.

let loupeCurrentCell = null;
let loupeRaf = null;
let loupeLastEv = null;

const DPR = window.devicePixelRatio || 1;
loupeCanvas.width = LOUPE_SIZE * DPR;
loupeCanvas.height = LOUPE_SIZE * DPR;

function showLoupe() {
  loupe.classList.add('active');
  if (!loupeRaf) loupeRaf = requestAnimationFrame(drawLoupe);
}

function hideLoupe() {
  loupe.classList.remove('active');
  if (loupeRaf) { cancelAnimationFrame(loupeRaf); loupeRaf = null; }
}

function positionLoupe(ev, cell) {
  loupeLastEv = { ev, cell };
  loupe.style.left = (ev.clientX - LOUPE_SIZE / 2) + 'px';
  loupe.style.top  = (ev.clientY - LOUPE_SIZE / 2) + 'px';
}

function drawLoupe() {
  loupeRaf = loupe.classList.contains('active') ? requestAnimationFrame(drawLoupe) : null;
  if (!loupeLastEv) return;
  const { ev, cell } = loupeLastEv;
  const cv = cell.querySelector('video');
  if (!cv || !cv.videoWidth) return;

  const rect = cell.getBoundingClientRect();
  const vw = cv.videoWidth, vh = cv.videoHeight;
  // object-fit:cover mapping of the source video into the square cell
  const coverScale = Math.max(rect.width / vw, rect.height / vh);
  const covOffX = (rect.width - vw * coverScale) / 2;
  const covOffY = (rect.height - vh * coverScale) / 2;

  // cursor position within the cell
  const mx = Math.max(0, Math.min(rect.width,  ev.clientX - rect.left));
  const my = Math.max(0, Math.min(rect.height, ev.clientY - rect.top));
  // corresponding point in source-video pixels
  const srcX = (mx - covOffX) / coverScale;
  const srcY = (my - covOffY) / coverScale;

  // crop a window of the source whose displayed size = LOUPE_SIZE at ZOOM
  const cropW = LOUPE_SIZE / (coverScale * LOUPE_ZOOM);
  const cropH = LOUPE_SIZE / (coverScale * LOUPE_ZOOM);
  let sx = srcX - cropW / 2;
  let sy = srcY - cropH / 2;
  // clamp the crop to source bounds so we never sample outside the frame
  sx = Math.max(0, Math.min(vw - cropW, sx));
  sy = Math.max(0, Math.min(vh - cropH, sy));

  const C = loupeCanvas.width, H = loupeCanvas.height;
  loupeCtx.clearRect(0, 0, C, H);
  try {
    loupeCtx.drawImage(cv, sx, sy, cropW, cropH, 0, 0, C, H);
  } catch (e) { /* frame not ready yet */ }
}


function setupGridLoupe() {
  let raf = null, lastEv = null;
  grid.addEventListener('mousemove', (e) => {
    lastEv = e;
    if (raf) return;
    raf = requestAnimationFrame(() => {
      raf = null;
      const ev = lastEv; if (!ev) return;
      const el = document.elementFromPoint(ev.clientX, ev.clientY);
      const cell = el && el.closest ? el.closest('.cell') : null;
      if (cell && !cell.classList.contains('empty') && cell.dataset.clip) {
        if (cell !== loupeCurrentCell) {
          loupeCurrentCell = cell;
          showLoupe();
        }
        positionLoupe(ev, cell);
        return;
      }
      if (el === grid && loupeCurrentCell) {
        positionLoupe(ev, loupeCurrentCell);
        return;
      }
      loupeCurrentCell = null;
      hideLoupe();
    });
  });
  grid.addEventListener('mouseleave', () => { loupeCurrentCell = null; hideLoupe(); });
}

function escapeHtml(s) { return s.replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

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


def write_viewer(day_dirs: list[Path], out_path: Path, target_seconds: float) -> Path:
    out_dir = out_path.parent
    manifest = build_manifest(day_dirs, out_dir, target_seconds)
    if not manifest["days"]:
        raise SystemExit(
            "No matching '<strain> <dose>J' conditions found in any day folder.\n"
            "Expected e.g. '260530_Crawling_day1/N2 0J/plate01/clip.mp4'."
        )
    html = (HTML_TEMPLATE
            .replace("__TITLE__", manifest["title"])
            .replace("__MANIFEST__", json.dumps(manifest, ensure_ascii=False)))
    out_path.write_text(html, encoding="utf-8")
    return out_path


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Build a WormScan crawling-video viewer.")
    ap.add_argument("day_folders", nargs="+", help="One or more day folders.")
    ap.add_argument("--target-seconds", type=float, default=DEFAULT_TARGET_SECONDS,
                    help=f"Approx loop length (default {DEFAULT_TARGET_SECONDS}).")
    ap.add_argument("--out", default=None, help="Output viewer.html path.")
    args = ap.parse_args(argv[1:])

    day_dirs = [Path(p).resolve() for p in args.day_folders]
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
        out_path = parent / f"{stem}_video_viewer.html"

    print(f"Building viewer from {len(day_dirs)} day folder(s)...")
    out = write_viewer(day_dirs, out_path, args.target_seconds)
    print(f"\nWrote {out}")
    print("  Open it by double-clicking (Chrome/Firefox; Safari needs Develop "
          "> Disable Local File Restrictions).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
