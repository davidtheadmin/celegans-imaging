"""
Development analysis (3.13 side).

Runs the YOLO staging model over one or more folders of plate images and turns
the developmental-stage calls into a DEVELOPMENT readout — mean stage index,
stage composition and body size — with per-image / per-plate / per-condition
statistics, an Excel report, four figures and a self-contained explorer.

Naming note (deliberate)
------------------------
The user-facing name of this mode is "Development". The module name, the agent
class, the mode string "survival" and the ``survival_*`` config fields keep
their original spelling on purpose: renaming them would churn config
persistence and every call site for no behavioural gain. User-visible strings —
UI labels, the output folder, the workbook filename, log wording — all say
"Development".

Why the readout changed
-----------------------
Survival % = (L3+L4+adult)/all was the old headline and it does not hold up:

  * The denominator collapses. In a full dose experiment one strain lost 95% of
    its animals by 20 J/m², so survival % was computed over ~25 surviving worms
    per plate and ROSE with dose — an inverted dose response that is an artefact
    of the shrinking denominator, not biology.
  * The survivor cutoff sits exactly on the L2/L3 boundary, which is where the
    model is weakest: about 26% of L3 calls are L2-sized.

Body size, stage composition and adult-keyed counts did hold up. Survival % is
therefore still computed and still written to the workbook — people ask for it
and dropping it silently would be worse — but it appears in NO figure.

Multi-folder
------------
One run takes a LIST of folders, each with a timepoint in hours (typed by the
user, or derived from the capture stamps in the image filenames). One folder is
a list of one. Every row carries its folder's timepoint, so the figures can put
time on an axis.

Two-venv boundary (hard constraint)
-----------------------------------
This module orchestrates, aggregates and writes Excel on the launcher's 3.13
venv. It NEVER imports ultralytics / torch / tiled_infer. ALL inference goes
through a subprocess to the vision venv:

    vision/.venv-vision/Scripts/python.exe  vision/infer_stage.py --batch --stdin

The subprocess loads the model once, tiles every frame at 676x608 (staging reads
absolute worm size — whole frames are never resized), and streams JSON Lines
back. We parse those and do all the statistics here.

Thread boundary mirrors CountingAgent/CountingStatus exactly:
    Write contract — worker thread ONLY: status.update() / status.mark_completed()
    Read contract  — UI thread ONLY:     status.snapshot() / status.pop_completed()
Never touch Tk widgets from the worker thread.

Heavy deps (pandas, numpy, openpyxl) are imported lazily inside the run so
importing this module at launcher start-up stays cheap.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import threading
import traceback
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import paths
from typing import Callable, Optional

log = logging.getLogger(__name__)

_ANALYSIS_PREFIX = "_development"
# Always written. Not optional any more: the body-size figure and the
# size_histogram / size_summary sheets are built from this file, so a run
# without it would be missing an agreed output rather than merely a diagnostic.
_SOFT_SCORES_NAME = "soft_stage_scores.csv"
_RESULTS_NAME = "development_results.xlsx"
_EXPLORER_NAME = "explorer.html"

# Discovery mirrors analysis/counting.py so both sides agree on the image set.
_IMAGE_EXTS = (".tif", ".tiff", ".png", ".jpg", ".jpeg")
_MAX_DEPTH = 3

# --- vision venv locations (inference lives ONLY here) ----------------------
# CREATE_NO_WINDOW keeps a spawned console from appearing when the launcher is
# running under pythonw.exe. 0 elsewhere, where the flag does not exist.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

_VISION_DIR = Path(__file__).parent / "vision"
# Resolved rather than hardcoded: an installed copy keeps this venv directly
# under the install root, because nesting it here overruns Windows MAX_PATH
# while pip is unpacking torch. See launcher/paths.py.
_VISION_PY = paths.vision_python()
_INFER_SCRIPT = _VISION_DIR / "infer_stage.py"

# The model and the threshold file are TUNABLES: %APPDATA%\\WormScan wins
# over the copy this install shipped, so a retrained model or a
# recalibrated stage_conf.json can be handed over as a file instead of a
# new installer. Nothing is seeded into that folder automatically, so a
# stock install always reads what it shipped. See launcher/paths.py.
#
# Resolved once at import, deliberately: a run must not change model
# half-way through because someone dropped a file in mid-analysis.
_MODEL_PATH = paths.staging_model()
_STAGE_CONF = paths.stage_conf()


# ---------------------------------------------------------------------------
# Shared staging defaults (vision/stage_conf.json)
#
# The same file infer_stage.py reads when nothing is passed on the command
# line. Reading it here — plain JSON, no vision-venv import — is what lets the
# analysis dialog seed its per-class sliders, and the "Analyze on laptop"
# button run, with numbers identical to a batch survival run. Do NOT duplicate
# these values in this module: if the file is unreadable we deliberately return
# nothing and let infer_stage.py apply its own fallback, so there is never a
# second set of defaults to drift.
# ---------------------------------------------------------------------------

_DEFAULT_CLASS_CONF_FALLBACK = 0.25


def load_stage_defaults() -> dict:
    """Read vision/stage_conf.json. Returns {} if missing/unreadable."""
    try:
        raw = json.loads(_STAGE_CONF.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("could not read %s: %s", _STAGE_CONF, exc)
        return {}
    return raw if isinstance(raw, dict) else {}


def default_exclude_classes() -> list[str]:
    """Classes stage_conf.json drops by default (ships as ["egg"])."""
    return [str(c) for c in (load_stage_defaults().get("exclude_classes") or [])]


def default_class_conf() -> dict[str, float]:
    """Per-class confidence defaults, in the order the sliders should appear.

    Drops the "_default" catch-all: it is a fallback for classes the file does
    not name, not something to put a slider on. Class names come from this file
    rather than from model.names because the 3.13 side cannot load the model —
    if a retrain renames a class, update stage_conf.json. A name that no longer
    exists is harmless (infer_stage matches by name and ignores strangers), and
    a *new* class the file misses still runs, on "_default".
    """
    conf = (load_stage_defaults().get("class_conf") or {})
    return {k: float(v) for k, v in conf.items() if not k.startswith("_")}

# ---------------------------------------------------------------------------
# Survivor mapping — THE ONLY place stage names are referenced. Stage strings
# come from model.names at run time (never hardcoded elsewhere); this dict maps
# them to survival categories and must stay editable in one spot. Any stage the
# model reports that is not listed here is counted, reported in an `unmapped`
# column, excluded from the survival denominator, and logged loudly — so a
# retrain that renames/adds a class fails visibly in the report instead of
# silently miscounting. Comparison is case-insensitive and whitespace-tolerant.
# ---------------------------------------------------------------------------
SURVIVAL_CONFIG: dict[str, list[str]] = {
    "survivors":     ["L3", "L4", "young adult", "adult"],
    "non_survivors": ["L1", "L2"],
    "excluded":      ["egg"],   # counted + reported, NOT in survival denom
}
# survival % = survivors / (survivors + non_survivors) * 100
# Kept for the workbook only. No figure draws it — see the module docstring.


# ---------------------------------------------------------------------------
# Stage index — THE development readout
#
# L1=1 … adult=5, eggs excluded (an egg has no developmental stage on this
# scale, and including it as a 0 would drag the mean by however many eggs
# happened to be laid). Mean stage index is computed per plate first, then
# averaged across plates, so a plate with 600 animals does not outvote one with
# 25. "young adult" is not a class this checkpoint emits — the entry is here
# only so a retrain that reintroduces it lands between L4 and adult instead of
# silently becoming unmapped. Comparison is case-insensitive.
# ---------------------------------------------------------------------------
STAGE_INDEX: dict[str, float] = {
    "l1": 1.0, "l2": 2.0, "l3": 3.0, "l4": 4.0,
    "young adult": 4.5, "adult": 5.0,
}


def stage_index_of(stage: str) -> Optional[float]:
    """Developmental index for a stage name, or None if it has none (eggs)."""
    return STAGE_INDEX.get(_norm(stage))


def _mean_stage_index(counts: dict[str, int]) -> tuple[float, int]:
    """Return (mean stage index, n animals it was computed over).

    Animals whose stage carries no index (eggs, unmapped classes) are excluded
    from BOTH the sum and the denominator — so the mean is over staged animals
    only, and the returned n says how many that was.
    """
    total = 0.0
    n = 0
    for stage, c in counts.items():
        idx = stage_index_of(stage)
        if idx is None or not c:
            continue
        total += idx * c
        n += c
    return ((total / n) if n else float("nan")), n


# --- condition grammar ------------------------------------------------------
# "<strain> <dose><unit>", e.g. "601 20J" / "N2 100 uM". <unit> is J (rendered
# J/m²) or uM/µM (rendered µM).
#
# The definition lives in assay_common so all four assays share ONE grammar —
# motility, crawling and counting parse condition folders with the same rule, and
# a change here cannot reach one assay and miss another. Re-exported under the
# names this module has always used, so every call site is unchanged.
from assay_common import canon_unit as _canon_unit    # noqa: E402
from assay_common import parse_condition             # noqa: E402,F401


# --- filename-encoded metadata (flat-folder capture naming) -----------------
# Real capture data is a FLAT folder; metadata lives in the STEM, e.g.
#   601_Train_survival_0J_p01_0001.png
#    ^strain  ^session tag   ^dose ^plate ^frame
# We split on '_' and match tokens by shape (NOT fixed position — the session
# tag varies in length): a dose token, a plate token, the first token as strain,
# a trailing pure-digit frame token (ignored). The condition label we build must
# stay COND_RE-parseable so dose_response works unchanged, so the unit is the
# label form ('J' / 'µM'), which _canon_unit later maps to J/m² / µM.
_DOSE_TOKEN_RE = re.compile(r"^(\d+)(J|uM|µM)$", re.IGNORECASE)
_PLATE_TOKEN_RE = re.compile(r"^p(\d+)$", re.IGNORECASE)
_ENCODED_FRACTION = 0.80
_UNPARSED_CONDITION = "__unparsed__"


def _label_unit(token: str) -> str:
    """Condition-label unit form that COND_RE re-accepts: 'J' or 'µM'."""
    return "J" if token.lower() == "j" else "µM"


def parse_filename_tokens(stem: str) -> Optional[dict]:
    """Token-parse a capture-style stem. Returns {'strain','dose','unit','plate'}
    (unit is the COND_RE-compatible label form 'J'/'µM'), or None if either a
    dose or a plate token is absent (the two keys grouping needs)."""
    tokens = stem.split("_")
    if not tokens:
        return None
    strain = tokens[0]
    dose = unit = plate = None
    for t in tokens[1:]:  # strain is token 0; never consume it as dose/plate
        md = _DOSE_TOKEN_RE.match(t)
        if md and dose is None:
            dose = int(md.group(1))
            unit = _label_unit(md.group(2))
            continue
        mp = _PLATE_TOKEN_RE.match(t)
        if mp and plate is None:
            plate = int(mp.group(1))
    if dose is None or plate is None:
        return None
    return {"strain": strain, "dose": dose, "unit": unit, "plate": plate}


def decide_grouping_mode(images: list[Path]) -> tuple[str, float]:
    """Return (mode, encoded_fraction). 'filename' if >=80% of stems carry BOTH
    a dose and a plate token; otherwise 'directory' (the counting-style rule)."""
    if not images:
        return "directory", 0.0
    n_encoded = sum(1 for p in images if parse_filename_tokens(p.stem) is not None)
    frac = n_encoded / len(images)
    return ("filename" if frac >= _ENCODED_FRACTION else "directory"), frac


def resolve_record(path: Path, root: Path, mode: str) -> tuple[str, str]:
    """Return (condition, plate) for one image under the chosen grouping mode.

    filename mode: parse the stem; unparsable stems go to the visible
    '__unparsed__' condition (plate=stem) so they are never silently dropped.
    The 4 frames of a plate share (condition, plate) and thus SUM into one plate
    row. directory mode: the existing depth-based rule, unchanged.
    """
    if mode == "filename":
        parsed = parse_filename_tokens(path.stem)
        if parsed is None:
            return _UNPARSED_CONDITION, path.stem
        condition = f"{parsed['strain']} {parsed['dose']}{parsed['unit']}"
        return condition, f"p{parsed['plate']:02d}"
    return resolve_image_path(path, root)


# ---------------------------------------------------------------------------
# Quadrant identity
#
# A plate is imaged as four quadrant frames, named ..._NE / _NW / _SE / _SW.
# When a condition has only one plate, those four frames are the only
# replication available, so the figures fall back to them — which means we have
# to be able to name them. A frame with no quadrant suffix keeps its stem, so
# the fallback still has distinct units rather than four identical labels.
# ---------------------------------------------------------------------------
_QUADRANT_RE = re.compile(r"_([NS][EW])$", re.IGNORECASE)


def quadrant_of(image_name: str) -> str:
    """'…_NW.tif' -> 'NW'. Anything else -> the stem, so units stay distinct."""
    stem = Path(image_name).stem
    m = _QUADRANT_RE.search(stem)
    return m.group(1).upper() if m else stem


# ---------------------------------------------------------------------------
# Timepoints
#
# Each folder in a Development run is one timepoint. The user may type the
# elapsed hours beside the folder; when the box is left blank we derive it from
# the capture stamp in the image filenames (20260805T075438_NW.tif), taking the
# folder's MEDIAN capture time — median, not min, because one stray file copied
# in from another session would otherwise define the folder's clock.
#
# What we deliberately do NOT do is guess. A folder with no typed value and no
# parseable stamp stops the run and is named in the error. Falling back to file
# mtimes would be wrong after any copy or re-sync and would fail silently; a
# folder quietly landing at 0 h puts a wrong x-axis on every figure. This was
# confirmed with David.
# ---------------------------------------------------------------------------
_ISO_STAMP_RE = re.compile(r"(?<!\d)(\d{8})T(\d{6})(?!\d)")


def parse_capture_time(name: str) -> Optional[datetime]:
    """Capture datetime from an image filename, or None if it carries no stamp."""
    m = _ISO_STAMP_RE.search(Path(name).stem)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def folder_capture_time(images: list[Path]) -> tuple[Optional[datetime], int, int]:
    """Return (median capture time | None, n_stamped, n_images) for one folder."""
    stamps = sorted(
        t for t in (parse_capture_time(p.name) for p in images) if t is not None
    )
    if not stamps:
        return None, 0, len(images)
    return stamps[len(stamps) // 2], len(stamps), len(images)


@dataclass
class FolderPlan:
    """One folder of a Development run, with its resolved timepoint.

    ``hours`` is None only when ``error`` is set — the run must not start.
    ``method`` is one of "typed", "filenames", "single folder"; ``detail`` is
    the human-readable line written to the log and to run_info.
    """
    folder: Path
    hours: Optional[float] = None
    method: str = ""
    detail: str = ""
    error: str = ""
    n_images: int = 0
    n_stamped: int = 0
    capture_time: Optional[datetime] = None


def _fmt_hours(h: float) -> str:
    return f"{h:g} h"


def resolve_timepoints(entries: list[tuple[Path, str]]) -> list[FolderPlan]:
    """Resolve each (folder, typed_text) pair to a timepoint in hours.

    Rules, in order:

    * A non-empty typed value always wins, and is taken literally.
    * A single folder with nothing typed is timepoint 0 — there is no second
      folder for it to be relative to, and the time-axis panels collapse to one
      column. This is the ordinary single-folder case and must not error.
    * Otherwise the folder's median capture stamp is used, expressed as hours
      relative to the EARLIEST folder in the run.
    * Mixed runs (some typed, some derived) need the two clocks tied together.
      We anchor on folders that are both typed AND stamped: offset = mean(typed
      − raw-derived) over those. With no such folder there is nothing to relate
      the clocks to, so every derived folder errors rather than inventing one.
    * A folder with neither a typed value nor a stamp errors, naming itself.

    Never raises. The caller reports ``error`` on any returned plan.
    """
    plans: list[FolderPlan] = []
    for folder, typed in entries:
        images = find_images(folder)
        cap, n_stamped, n_images = folder_capture_time(images)
        plan = FolderPlan(folder=folder, n_images=n_images,
                          n_stamped=n_stamped, capture_time=cap)
        text = (typed or "").strip()
        if text:
            try:
                plan.hours = float(text.replace(",", "."))
            except ValueError:
                plan.error = (
                    f"{folder.name}: timepoint \"{text}\" is not a number. "
                    "Enter elapsed hours (e.g. 0, 24, 48.5) or clear the box to "
                    "derive it from the image capture times."
                )
                plans.append(plan)
                continue
            plan.method = "typed"
            plan.detail = f"typed by the user ({_fmt_hours(plan.hours)})"
        plans.append(plan)

    typed_plans = [p for p in plans if p.method == "typed"]
    todo = [p for p in plans if p.method != "typed" and not p.error]

    # Single folder, nothing typed: timepoint 0, no derivation needed.
    if len(plans) == 1 and todo:
        p = todo[0]
        p.hours = 0.0
        p.method = "single folder"
        p.detail = ("single folder with no timepoint given — treated as 0 h; "
                    "the time-axis panels collapse to one column")
        return plans

    stamped = [p for p in plans if p.capture_time is not None]
    if todo and not stamped:
        for p in todo:
            p.error = (
                f"{p.folder.name}: no timepoint typed and no capture time in the "
                f"image filenames ({p.n_images} image(s) checked; expecting a "
                "stamp like 20260805T075438_NW.tif). Type the elapsed hours for "
                "this folder."
            )
        return plans

    if todo:
        base = min(p.capture_time for p in stamped)

        def raw_hours(p: FolderPlan) -> float:
            return (p.capture_time - base).total_seconds() / 3600.0

        offset = 0.0
        anchors = [p for p in typed_plans if p.capture_time is not None]
        if typed_plans and not anchors:
            for p in todo:
                p.error = (
                    f"{p.folder.name}: its timepoint would be derived from the "
                    "capture times, but the folders you typed a timepoint for "
                    "carry no capture stamp, so there is nothing to line the two "
                    "clocks up against. Type a timepoint for this folder too, or "
                    "clear the typed ones and let all of them be derived."
                )
            return plans
        if anchors:
            offset = sum(p.hours - raw_hours(p) for p in anchors) / len(anchors)

        for p in todo:
            if p.capture_time is None:
                p.error = (
                    f"{p.folder.name}: no timepoint typed and no capture time in "
                    f"the image filenames ({p.n_images} image(s) checked; "
                    "expecting a stamp like 20260805T075438_NW.tif). Type the "
                    "elapsed hours for this folder."
                )
                continue
            # Rounded to 0.1 h: the median capture times of two sessions are
            # never exactly 24.0000 h apart, and a "24.0106 h" on every axis
            # label is noise dressed up as precision. Six minutes is well
            # inside the resolution of a developmental timecourse.
            p.hours = round(raw_hours(p) + offset, 1)
            p.method = "filenames"
            p.detail = (
                f"derived from image capture times "
                f"({p.n_stamped}/{p.n_images} filenames stamped, median "
                f"{p.capture_time.isoformat(sep=' ', timespec='minutes')}) "
                f"→ {_fmt_hours(p.hours)}"
                + (f", anchored on the typed folder(s) (offset "
                   f"{offset:+.2f} h)" if anchors else "")
            )

    return plans


def log_timepoint_plan(plans: list[FolderPlan],
                       write_log: Callable[[str], None]) -> None:
    """Say per folder how its timepoint was decided — loudly, like GROUPING."""
    write_log("=" * 60)
    write_log(f"TIMEPOINTS: {len(plans)} folder(s)")
    for p in sorted(plans, key=lambda q: (q.hours if q.hours is not None else 0)):
        write_log(f"  {_fmt_hours(p.hours or 0.0):>8}  {p.folder}")
        write_log(f"            {p.detail}")
    methods = Counter(p.method for p in plans)
    if methods.get("filenames") and methods.get("typed"):
        write_log(
            "  NOTE: this run mixes typed and derived timepoints. The derived "
            "ones were shifted onto the typed clock; check the hours above "
            "read the way you expect before trusting the time axis."
        )
    write_log("=" * 60)


# ---------------------------------------------------------------------------
# Discovery + path resolution (image-native rule, matches analysis/counting.py)
# ---------------------------------------------------------------------------

def find_images(folder: Path, max_depth: int = _MAX_DEPTH) -> list[Path]:
    """Recursively find image files up to ``max_depth`` levels deep, skipping
    dirs starting with '_' (pipeline output) or '.' (hidden caches)."""
    results: list[Path] = []

    def _recurse(path: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            for child in sorted(path.iterdir()):
                if child.is_file() and child.suffix.lower() in _IMAGE_EXTS:
                    results.append(child)
                elif child.is_dir() and not (
                    child.name.startswith("_") or child.name.startswith(".")
                ):
                    _recurse(child, depth + 1)
        except PermissionError:
            pass

    _recurse(folder, 1)
    return results


def resolve_image_path(image: Path, root: Path) -> tuple[str, str]:
    """Return (condition, plate) for an image relative to ``root``.

    Depth 0  root/img                   -> condition="default", plate=img.stem
    Depth 1  root/plate/img             -> condition="default", plate=parent.name
    Depth 2+ root/condition/plate/img   -> condition=grandparent.name, plate=parent.name
    """
    try:
        rel = image.relative_to(root)
    except ValueError:
        return "default", image.stem
    depth = len(rel.parts) - 1
    if depth == 0:
        return "default", image.stem
    if depth == 1:
        return "default", image.parent.name
    return image.parent.parent.name, image.parent.name


# ---------------------------------------------------------------------------
# Stage classification
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    return str(s).strip().lower()


def build_stage_categories(
    stage_names: list[str], config: dict[str, list[str]]
) -> tuple[dict[str, str], list[str]]:
    """Map each model stage to survivor|non_survivor|excluded|unmapped.

    Returns (category_by_stage, unmapped_stage_list). Unmapped stages are the
    'fail loud' signal — the caller reports and logs them.
    """
    surv = {_norm(x) for x in config["survivors"]}
    non = {_norm(x) for x in config["non_survivors"]}
    exc = {_norm(x) for x in config["excluded"]}
    cats: dict[str, str] = {}
    unmapped: list[str] = []
    for stage in stage_names:
        n = _norm(stage)
        if n in surv:
            cats[stage] = "survivor"
        elif n in non:
            cats[stage] = "non_survivor"
        elif n in exc:
            cats[stage] = "excluded"
        else:
            cats[stage] = "unmapped"
            unmapped.append(stage)
    return cats, unmapped


def _tally(counts: dict[str, int], cats: dict[str, str]) -> tuple[int, int, int, int]:
    """Return (n_survivors, n_non_survivors, n_excluded, n_unmapped)."""
    surv = non = exc = unm = 0
    for stage, c in counts.items():
        cat = cats.get(stage, "unmapped")
        if cat == "survivor":
            surv += c
        elif cat == "non_survivor":
            non += c
        elif cat == "excluded":
            exc += c
        else:
            unm += c
    return surv, non, exc, unm


def _survival_pct(n_surv: int, n_non: int) -> float:
    denom = n_surv + n_non
    if denom == 0:
        return float("nan")
    return n_surv / denom * 100.0


# ---------------------------------------------------------------------------
# Inference subprocess (vision venv)
# ---------------------------------------------------------------------------

def run_inference(
    images: list[Path],
    class_conf: dict[str, float],
    *,
    exclude_classes: Optional[list[str]] = None,
    preview_dir: Optional[Path],
    soft_csv: Optional[Path] = None,
    rescore: bool = True,
    write_log: Callable[[str], None],
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> tuple[list[str], list[dict], dict]:
    """Call vision/infer_stage.py once for the whole image list via stdin.

    Returns (stage_names, records, meta). stage_names is the authoritative model
    class list from the meta line. records are the per-image JSON objects (each
    has "path" and either "counts"/"w"/"h" or "error"). meta is the whole meta
    line, which carries the thresholds and tiling params actually used — those
    go straight into run_info so a saved report says how it was produced.

    `class_conf` is passed through as inline JSON. Anything it does not name
    falls back to vision/stage_conf.json, so an empty dict means "use the shared
    defaults" and the run still matches the Analyze-on-laptop button.

    `soft_csv`, when given, asks the vision side to additionally write one row
    per detection carrying its full per-class score vector. It changes nothing
    about the detections themselves — see tiled_infer.collect_scores.

    `rescore` is a SWITCH, not a value. True passes no alpha at all, so
    vision/stage_conf.json's rescore.alpha applies (ships at 2.0) and stays the
    single tunable source of truth. False passes an explicit 0, which is a
    bit-identical no-op. Nothing here hardcodes an alpha — that is deliberate:
    retuning must be an edit to stage_conf.json, not a rebuild.

    Raises RuntimeError if the vision venv/model is missing or the subprocess
    fails before emitting any records.
    """
    if not _VISION_PY.exists():
        raise RuntimeError(f"Vision venv python not found: {_VISION_PY}")
    if not _INFER_SCRIPT.exists():
        raise RuntimeError(f"infer_stage.py not found: {_INFER_SCRIPT}")

    cmd = [
        str(_VISION_PY), str(_INFER_SCRIPT),
        "--batch", "--stdin",
        "--model", str(_MODEL_PATH),
        # Passed explicitly so both ends read the SAME file. Without it
        # infer_stage.py resolves stage_conf.json relative to itself and
        # would silently ignore an override in %APPDATA%\\WormScan.
        "--stage-conf", str(_STAGE_CONF),
        "--no-boxes",  # stats never need per-box lists; previews drawn in-proc
    ]
    if class_conf:
        # Not shell-quoted anywhere: Popen gets an argv list, so the JSON goes
        # across verbatim regardless of braces/quotes on Windows.
        cmd += ["--class-conf", json.dumps(class_conf)]
    if exclude_classes is not None:
        # Always passed explicitly (even as an empty string) so the UI checkbox
        # is authoritative and cannot be silently overridden by the file default.
        cmd += ["--exclude-classes", ",".join(exclude_classes)]
    if preview_dir is not None:
        cmd += ["--preview-dir", str(preview_dir)]
    if soft_csv is not None:
        # Side output only. infer_stage.py feeds it from the detections the
        # normal merge already kept, so counts and Excel are unaffected.
        cmd += ["--soft-csv", str(soft_csv)]
    if not rescore:
        # Only ever passed to turn the pass OFF. Ticked = say nothing and let
        # stage_conf.json's alpha stand.
        cmd += ["--rescore-alpha", "0"]

    write_log(f"Inference: {' '.join(cmd)}")
    write_log(f"Feeding {len(images)} image path(s) to the vision venv…")

    proc = subprocess.Popen(
        cmd, cwd=str(_VISION_DIR),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", bufsize=1,
        # The launcher runs under pythonw.exe, which has no console. Spawning
        # python.exe without this makes Windows allocate one — an empty black
        # terminal that pops up beside the progress window for the length of
        # the run. Every pipe here is already captured, so the console never
        # had anything to show.
        creationflags=_NO_WINDOW,
    )

    # Drain stderr on a side thread into a buffer to avoid a pipe-fill deadlock;
    # flushed to the log after the process ends (and always on failure).
    stderr_lines: list[str] = []

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_lines.append(line.rstrip("\n"))

    err_thread = threading.Thread(target=_drain_stderr, daemon=True)
    err_thread.start()

    # Feed the file list, then close stdin so the child can start.
    assert proc.stdin is not None
    for img in images:
        proc.stdin.write(str(img) + "\n")
    proc.stdin.close()

    stage_names: list[str] = []
    records: list[dict] = []
    meta: dict = {}
    total = len(images)
    cancelled = False

    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            write_log(f"[infer] unparseable stdout line: {line[:200]}")
            continue
        if "path" not in obj:  # meta line
            meta = obj
            stage_names = list(obj.get("names", []))
            write_log(f"Model classes: {stage_names}")
            eff = obj.get("class_conf") or {}
            if eff:
                write_log("Per-class confidence: " + ", ".join(
                    f"{k}={float(v):.2f}" for k, v in eff.items()))
            write_log(
                f"Tiling overlap: {obj.get('overlap')}   "
                f"seam suppression: {obj.get('seam')}"
            )
            excl = obj.get("exclude_classes") or []
            write_log("Excluded classes (NOT counted, not zero): "
                      + (", ".join(excl) if excl else "(none)"))
            # Echo what the vision side actually resolved, not what we asked
            # for — this is the line that proves which alpha ran.
            rs = obj.get("rescore") or {}
            alpha = float(rs.get("alpha") or 0.0)
            if alpha:
                refs = ", ".join(f"{k}={float(v):.4f}"
                                 for k, v in (rs.get("refs") or {}).items())
                write_log(
                    f"Class-confidence correction: ON, alpha {alpha:g} — "
                    "relabels only; detection count and box geometry are "
                    f"unchanged. Reference scores: {refs}"
                )
            else:
                write_log(
                    "Class-confidence correction: OFF (alpha 0 — arg-max on "
                    "raw scores, bit-identical to a build without the pass)"
                )
            continue
        records.append(obj)
        if progress_cb is not None:
            progress_cb(len(records), total, Path(obj["path"]).name)
        if cancel_check is not None and cancel_check():
            cancelled = True
            write_log("Cancel requested — terminating inference subprocess.")
            proc.terminate()
            break

    proc.wait()
    err_thread.join(timeout=2)
    if stderr_lines:
        write_log("--- vision venv stderr ---")
        for ln in stderr_lines:
            write_log(ln)
        write_log("--- end vision venv stderr ---")

    if cancelled:
        return stage_names, records, meta

    if proc.returncode not in (0, None) and not records:
        raise RuntimeError(
            f"Inference subprocess exited {proc.returncode} with no results "
            f"(see log for vision-venv stderr)."
        )

    return stage_names, records, meta


# ---------------------------------------------------------------------------
# Aggregation (across every folder in the run)
# ---------------------------------------------------------------------------

def _cond_parts(condition: str) -> tuple[str, Optional[int], str]:
    """(strain, dose|None, unit) for a condition label; strain=label if unparsed."""
    parsed = parse_condition(condition)
    if parsed is None:
        return condition, None, ""
    return parsed


def _dose_key(dose: Optional[int]) -> tuple[int, int]:
    """Sort key putting an unparsed dose (None) after every real one."""
    return (1, 0) if dose is None else (0, dose)


def _sd(values: list[float]) -> float:
    """Sample SD of the non-NaN values; NaN when fewer than two remain."""
    import numpy as np
    arr = np.array([v for v in values if v == v], dtype=float)
    return float(np.std(arr, ddof=1)) if arr.size > 1 else float("nan")


def _mean(values: list[float]) -> float:
    import numpy as np
    arr = np.array([v for v in values if v == v], dtype=float)
    return float(np.mean(arr)) if arr.size else float("nan")


def aggregate(stage_names: list[str], folder_runs: list[dict],
              cats: dict[str, str]):
    """Build per_image / per_plate / per_condition / qc rows across all folders.

    ``folder_runs`` is one dict per folder: {"plan": FolderPlan, "records":
    [...], "mode": str, "encoded_fraction": float}. Every row carries the
    folder's timepoint, so downstream code never has to re-derive it.

    Conditions are matched across folders by their label. A condition present in
    one folder and absent in another is a GAP, not an error: it is reported in
    ``gaps`` and logged, and the figures leave that cell empty.

    Returns a dict of lists-of-dicts keyed by sheet name, plus the grouping and
    error counts. Nothing here computes body size — that comes from the soft
    score CSV, which is joined on (timepoint, image) later.
    """
    stage_cols = list(stage_names)

    per_image: list[dict] = []
    n_error = 0
    n_unparsed = 0
    # (timepoint, condition, plate) -> stage Counter, plus image count
    plate_counts: dict[tuple[float, str, str], Counter] = {}
    plate_nimg: dict[tuple[float, str, str], int] = {}
    plate_folder: dict[tuple[float, str, str], str] = {}
    # errors resolved to the condition they belong to, so a short run is
    # attributable rather than just a total
    err_by_cond: Counter = Counter()
    unparsed_by_folder: Counter = Counter()

    for run in folder_runs:
        plan: FolderPlan = run["plan"]
        root = plan.folder
        tp = float(plan.hours or 0.0)
        mode = run["mode"]
        for rec in run["records"]:
            path = Path(rec["path"])
            condition, plate = resolve_record(path, root, mode)
            if "error" in rec:
                n_error += 1
                err_by_cond[(tp, condition)] += 1
                continue
            if condition == _UNPARSED_CONDITION:
                n_unparsed += 1
                unparsed_by_folder[root.name] += 1
            counts = {k: int(v) for k, v in rec.get("counts", {}).items()}
            surv, non, exc, unm = _tally(counts, cats)
            total = surv + non + exc + unm
            si, n_staged = _mean_stage_index(counts)
            strain, dose, unit = _cond_parts(condition)
            row = {
                "folder": root.name,
                "timepoint_h": tp,
                "condition": condition,
                "strain": strain,
                "dose": dose,
                "unit": unit,
                "plate": plate,
                "quadrant": quadrant_of(path.name),
                "image": path.name,
            }
            for st in stage_cols:
                row[st] = counts.get(st, 0)
            row["unmapped"] = unm
            row["total"] = total
            row["n_staged"] = n_staged
            row["stage_index"] = si
            row["n_survivors"] = surv
            row["n_non_survivors"] = non
            row["survival_pct"] = _survival_pct(surv, non)
            per_image.append(row)

            key = (tp, condition, plate)
            agg = plate_counts.setdefault(key, Counter())
            for st, c in counts.items():
                agg[st] += c
            plate_nimg[key] = plate_nimg.get(key, 0) + 1
            plate_folder[key] = root.name

    # --- per plate ---------------------------------------------------------
    per_plate: list[dict] = []
    for (tp, condition, plate), agg in plate_counts.items():
        counts = dict(agg)
        surv, non, exc, unm = _tally(counts, cats)
        si, n_staged = _mean_stage_index(counts)
        strain, dose, unit = _cond_parts(condition)
        row = {
            "folder": plate_folder[(tp, condition, plate)],
            "timepoint_h": tp,
            "condition": condition,
            "strain": strain,
            "dose": dose,
            "unit": unit,
            "plate": plate,
        }
        for st in stage_cols:
            row[st] = counts.get(st, 0)
        row["unmapped"] = unm
        row["total"] = surv + non + exc + unm
        row["n_staged"] = n_staged
        row["stage_index"] = si
        row["n_survivors"] = surv
        row["n_non_survivors"] = non
        row["survival_pct"] = _survival_pct(surv, non)
        row["n_images"] = plate_nimg[(tp, condition, plate)]
        per_plate.append(row)

    # Sort order is load-bearing for the workbook: survival_excel builds its
    # formulas as plain SUM/AVERAGE over contiguous row ranges, which is only
    # valid because the rows of one plate — and the plates of one condition —
    # sit together. `condition` is in the key so two conditions that share a
    # strain and dose but differ in unit cannot interleave.
    _order = lambda r: (r["timepoint_h"], str(r["strain"]), _dose_key(r["dose"]),
                        str(r["condition"]), str(r["plate"]))
    per_image.sort(key=lambda r: _order(r) + (str(r["image"]),))
    per_plate.sort(key=_order)

    # --- per condition x timepoint ----------------------------------------
    #
    # Replication unit: plates when there is more than one, otherwise the
    # quadrant images of the single plate. Which one was used is carried in the
    # row (replicate_unit) and printed in the figure captions — a mean ± SD
    # whose n means two different things in two panels is worse than no SD.
    plates_by_cond: dict[tuple[float, str], list[dict]] = {}
    for r in per_plate:
        plates_by_cond.setdefault((r["timepoint_h"], r["condition"]), []).append(r)
    images_by_cond: dict[tuple[float, str], list[dict]] = {}
    for r in per_image:
        images_by_cond.setdefault((r["timepoint_h"], r["condition"]), []).append(r)

    per_condition: list[dict] = []
    for key in sorted(plates_by_cond,
                      key=lambda k: (k[0], str(_cond_parts(k[1])[0]),
                                     _dose_key(_cond_parts(k[1])[1]))):
        tp, condition = key
        plates = plates_by_cond[key]
        imgs = images_by_cond.get(key, [])
        strain, dose, unit = _cond_parts(condition)
        pooled: Counter = Counter()
        for r in plates:
            for st in stage_cols:
                pooled[st] += r[st]
        p_surv, p_non, p_exc, p_unm = _tally(dict(pooled), cats)
        p_total = p_surv + p_non + p_exc + p_unm

        reps = plates if len(plates) > 1 else imgs
        unit_name = "plate" if len(plates) > 1 else "quadrant image"
        si_vals = [r["stage_index"] for r in reps]
        surv_vals = [r["survival_pct"] for r in reps]
        n_vals = [r["total"] for r in reps]

        pooled_si, pooled_staged = _mean_stage_index(dict(pooled))
        row = {
            "timepoint_h": tp,
            "condition": condition,
            "strain": strain,
            "dose": dose,
            "unit": unit,
            "n_plates": len(plates),
            "n_images": len(imgs),
            "replicate_unit": unit_name,
            "n_replicates": len(reps),
            "stage_index_mean": _mean(si_vals),
            "stage_index_sd": _sd(si_vals),
            "n_animals_mean": _mean([float(v) for v in n_vals]),
            "n_animals_sd": _sd([float(v) for v in n_vals]),
        }
        for st in stage_cols:
            row[f"n_{st}"] = pooled[st]
        for st in stage_cols:
            row[f"pct_{st}"] = (100.0 * pooled[st] / p_total) if p_total else float("nan")
        row["unmapped"] = p_unm
        row["pooled_total"] = p_total
        row["pooled_staged"] = pooled_staged
        row["pooled_stage_index"] = pooled_si
        row["survival_pct_mean"] = _mean(surv_vals)
        row["survival_pct_sd"] = _sd(surv_vals)
        row["pooled_survival_pct"] = _survival_pct(p_surv, p_non)
        per_condition.append(row)

    # --- gaps: a condition seen somewhere but missing at some timepoint ----
    all_conditions = sorted({r["condition"] for r in per_condition})
    all_tps = sorted({r["timepoint_h"] for r in per_condition})
    have = {(r["timepoint_h"], r["condition"]) for r in per_condition}
    gaps = [
        {"timepoint_h": t, "condition": c}
        for c in all_conditions for t in all_tps if (t, c) not in have
    ]

    # --- quality control ---------------------------------------------------
    #
    # Panel 4 and the qc sheet read from here. The control for a condition is
    # the LOWEST dose of the same strain at the same timepoint — its own
    # control, never another strain's and never another day's.
    # QC is PLATE-based throughout, whatever replication unit the stage-index
    # panel used. "Animals per plate" has to mean animals per plate even for a
    # condition whose SD came from quadrants, or the % of control compares a
    # plate count with a quadrant count and lands 4x out. (It did, once — the
    # workbook cross-check is what caught it.)
    plate_mean_of: dict[tuple[float, str], float] = {}
    for key, plates in plates_by_cond.items():
        plate_mean_of[key] = _mean([float(p["total"]) for p in plates])

    control_n: dict[tuple[float, str], tuple] = {}
    for r in per_condition:
        k = (r["timepoint_h"], str(r["strain"]))
        if r["dose"] is None:
            continue
        cur = control_n.get(k)
        if cur is None or _dose_key(r["dose"]) < cur[0]:
            control_n[k] = (_dose_key(r["dose"]),
                            plate_mean_of[(r["timepoint_h"], r["condition"])])
    control_mean = {k: v[1] for k, v in control_n.items()}

    qc: list[dict] = []
    for r in per_condition:
        key = (r["timepoint_h"], r["condition"])
        plates = plates_by_cond[key]
        imgs = images_by_cond.get(key, [])
        plate_totals = [float(p["total"]) for p in plates]
        # quadrant-to-quadrant spread: within each plate, the SD of the per-image
        # animal counts as a % of that plate's mean; reported as the mean and the
        # worst plate. This is the number that says "one quadrant of this plate
        # is not like the others" — a bubble, a shadow, a mis-set focus.
        cvs: list[float] = []
        for p in plates:
            per_img = [i["total"] for i in imgs if i["plate"] == p["plate"]]
            if len(per_img) < 2:
                continue
            m = _mean([float(x) for x in per_img])
            s = _sd([float(x) for x in per_img])
            if m and m == m and s == s:
                cvs.append(100.0 * s / m)
        ctrl = control_mean.get((r["timepoint_h"], str(r["strain"])))
        qc.append({
            "timepoint_h": r["timepoint_h"],
            "condition": r["condition"],
            "strain": r["strain"],
            "dose": r["dose"],
            "unit": r["unit"],
            "n_plates": r["n_plates"],
            "n_images": r["n_images"],
            "n_animals_total": r["pooled_total"],
            "animals_per_plate_mean": _mean(plate_totals),
            "animals_per_plate_sd": _sd(plate_totals),
            "animals_per_plate_min": min([p["total"] for p in plates], default=0),
            "animals_per_plate_max": max([p["total"] for p in plates], default=0),
            "pct_of_control": (100.0 * plate_mean_of[key] / ctrl)
                              if (ctrl and ctrl == ctrl) else float("nan"),
            "quadrant_cv_pct_mean": _mean(cvs),
            "quadrant_cv_pct_max": max(cvs) if cvs else float("nan"),
            "n_image_errors": err_by_cond.get(key, 0),
        })

    return {
        "per_image": per_image,
        "per_plate": per_plate,
        "per_condition": per_condition,
        "qc": qc,
        "gaps": gaps,
        "stage_cols": stage_cols,
        "n_error": n_error,
        "n_unparsed": n_unparsed,
        "unparsed_by_folder": dict(unparsed_by_folder),
        "n_conditions": len({r["condition"] for r in per_condition}),
        "n_plates": len(per_plate),
        "n_images_ok": len(per_image),
        "timepoints": all_tps,
    }


# ---------------------------------------------------------------------------
# Console summary
# ---------------------------------------------------------------------------

def _console_summary(agg: dict, write_log: Callable[[str], None]) -> None:
    write_log("Per condition — mean stage index (the readout) and, for the "
              "record only, survival %:")
    for row in agg["per_condition"]:
        si, sd = row["stage_index_mean"], row["stage_index_sd"]
        sv = row["survival_pct_mean"]
        si_s = "nan" if si != si else f"{si:.2f}"
        sd_s = " ± n/a" if sd != sd else f" ± {sd:.2f}"
        sv_s = "nan" if sv != sv else f"{sv:.1f}"
        write_log(
            f"  {row['timepoint_h']:g} h  {row['condition']}: "
            f"stage index {si_s}{sd_s} "
            f"(n={row['n_replicates']} {row['replicate_unit']}"
            f"{'' if row['n_replicates'] == 1 else 's'}, "
            f"{row['pooled_total']} animals) | survival {sv_s}%"
        )
    if agg["gaps"]:
        write_log(
            f"NOTE: {len(agg['gaps'])} condition × timepoint cell(s) have no "
            "data. This is a gap, not an error — the condition simply was not "
            "imaged then. The figures leave those cells empty:"
        )
        for g in agg["gaps"]:
            write_log(f"  {g['condition']} at {g['timepoint_h']:g} h")


# ---------------------------------------------------------------------------
# Orchestrator (called by the agent worker thread)
# ---------------------------------------------------------------------------

@dataclass
class ReusePlan:
    """What a run would reuse, worked out without running anything.

    The dialog asks for this before it starts so it can say "these folders have
    already been analysed" up front, instead of the run finishing in four
    seconds and leaving the user wondering whether it did anything. analyze()
    uses the SAME function, so the preview cannot drift from what happens.
    """
    digest: str = ""
    manifests: list = field(default_factory=list)
    images: dict = field(default_factory=dict)      # folder -> [Path]
    caches: dict = field(default_factory=dict)      # folder -> FolderCache
    n_images: int = 0
    n_reused: int = 0
    n_fresh: int = 0

    @property
    def all_cached(self) -> bool:
        return bool(self.n_images) and self.n_fresh == 0

    @property
    def any_cached(self) -> bool:
        return self.n_reused > 0

    def folder_lines(self) -> list[str]:
        out = []
        for folder, cache in self.caches.items():
            total = len(self.images.get(folder, []))
            if cache.n_reused == total and total:
                out.append(f"  {Path(folder).name}: all {total} already done")
            elif cache.n_reused:
                out.append(f"  {Path(folder).name}: {cache.n_reused} of {total} "
                           f"already done, {total - cache.n_reused} to analyse")
            else:
                out.append(f"  {Path(folder).name}: {total} to analyse")
        return out


def plan_reuse(
    plans: list[FolderPlan],
    class_conf: dict[str, float],
    *,
    exclude_classes: Optional[list[str]] = None,
    save_previews: bool = False,
    force_reanalyze: bool = False,
    write_log: Optional[Callable[[str], None]] = None,
) -> ReusePlan:
    """Work out, per folder and per image, what a run would reuse.

    Cheap: a directory walk, a few JSON reads and a stat() per image. No model,
    no subprocess. Safe to call from the UI thread before a run starts.
    """
    import survival_cache

    stage_conf = load_stage_defaults()
    digest = survival_cache.settings_digest(
        stage_conf, class_conf or default_class_conf(), exclude_classes,
        _MODEL_PATH)
    manifests = ([] if force_reanalyze
                 else survival_cache.discover([p.folder for p in plans],
                                              write_log))
    out = ReusePlan(digest=digest, manifests=manifests)
    for plan in plans:
        images = find_images(plan.folder)
        out.images[plan.folder] = images
        if force_reanalyze:
            cache = survival_cache.FolderCache(
                folder=plan.folder, to_infer=list(images),
                reason="re-analyse was requested")
        else:
            cache = survival_cache.plan_folder(plan.folder, images, manifests,
                                               digest, save_previews)
        out.caches[plan.folder] = cache
        out.n_images += len(images)
        out.n_reused += cache.n_reused
        out.n_fresh += len(cache.to_infer)
    return out


def analyze(
    plans: list[FolderPlan],
    class_conf: dict[str, float],
    save_previews: bool,
    out_dir: Path,
    *,
    exclude_classes: Optional[list[str]] = None,
    rescore: bool = True,
    force_reanalyze: bool = False,
    write_log: Callable[[str], None],
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> dict:
    """End-to-end Development run over every folder in ``plans``.

    Each folder is handled separately — one subprocess per folder, so a bad
    folder cannot poison the others — and every resulting row is tagged with
    that folder's timepoint before aggregation. Outputs all land in ``out_dir``,
    which the agent also writes its log.txt into.

    Images already analysed by a previous run are REUSED rather than re-run,
    image by image (see survival_cache). That is what makes "analyse each
    timepoint as it comes in, then combine them at the end" cheap: the combining
    run does no inference at all. ``force_reanalyze`` turns the cache off.

    Returns {'n_plates', 'n_error', 'out_dir', 'out_xlsx', 'outputs',
    'reuse_note'}.
    """
    import survival_cache
    import survival_excel
    import survival_explorer
    import survival_figures
    import survival_scale
    import survival_size

    log_timepoint_plan(plans, write_log)

    # The alpha we are targeting. Read from stage_conf.json rather than passed
    # in, so cached rows are relabelled against exactly the number the vision
    # side would have used — one source of truth, both paths.
    stage_conf = load_stage_defaults()
    rescore_block = stage_conf.get("rescore") or {}
    target_alpha = float(rescore_block.get("alpha") or 0.0) if rescore else 0.0
    target_refs = rescore_block.get("refs") or {}

    if force_reanalyze:
        write_log("Re-analyse requested: previous results are ignored and every "
                  "image goes through the model again.")
    reuse = plan_reuse(plans, class_conf, exclude_classes=exclude_classes,
                       save_previews=save_previews,
                       force_reanalyze=force_reanalyze, write_log=write_log)
    digest = reuse.digest

    all_images: list[Path] = []
    folder_runs: list[dict] = []
    soft_parts: list[dict] = []
    manifest_folders: list[dict] = []
    reuse_rows: list[tuple[str, int, int, str]] = []
    stage_names: list[str] = []
    meta: dict = {}
    cache_meta: dict = {}
    modes: list[str] = []
    encoded: list[float] = []
    n_relabelled = 0
    n_no_vector = 0

    # Progress spans the whole run, not one folder, so the dialog's bar does not
    # restart at zero four times.
    per_folder_images = []
    for plan in plans:
        imgs = reuse.images.get(plan.folder, [])
        per_folder_images.append(imgs)
        all_images.extend(imgs)
    grand_total = len(all_images)
    done_before = 0

    write_log(f"Folders: {len(plans)}   images: {grand_total}")
    write_log(
        "Conf (per class): "
        + (", ".join(f"{k}={float(v):.2f}" for k, v in class_conf.items())
           if class_conf else "(vision/stage_conf.json defaults)")
    )
    write_log(f"Save previews: {save_previews}")
    write_log(
        "Class-confidence correction requested: "
        + ("ON — alpha comes from vision/stage_conf.json (nothing here "
           "hardcodes it)" if rescore else "OFF — alpha forced to 0")
    )
    write_log(
        f"Soft per-class scores: {_SOFT_SCORES_NAME} — always written. One row "
        "per detection carrying every class score for the box the pipeline "
        "kept, plus its size_px (always pixels here; the size outputs convert "
        "to µm when every image is calibrated). The body-size figure and the "
        "size sheets are "
        "built from it, and it is what lets a later run reuse this one. Scores "
        "are per-class sigmoids (they do NOT sum to 1) and are UNCALIBRATED: "
        "do not report them as percentages without calibrating against manual "
        "counts first."
    )

    for i, (plan, images) in enumerate(zip(plans, per_folder_images)):
        write_log("-" * 60)
        write_log(f"Folder {i + 1}/{len(plans)}: {plan.folder}  "
                  f"({plan.hours:g} h, {len(images)} image(s))")
        if not images:
            write_log("  no images here — skipped, and it contributes nothing "
                      "to any figure.")
            continue

        # --- what can come from a previous run? --------------------------
        fc = reuse.caches.get(plan.folder) or survival_cache.FolderCache(
            folder=plan.folder, to_infer=list(images))

        cached_rows = None
        records: list[dict] = []
        if fc.hit:
            man_names = list((fc.manifest or {}).get("stage_names") or [])
            cached_rows = survival_cache.load_cached_rows(
                fc, {p.name for p in fc.reused}, man_names,
                target_alpha, target_refs, exclude_classes)
            if cached_rows is None:
                write_log("  previous results could not be read — analysing "
                          "these images again.")
                fc = survival_cache.FolderCache(
                    folder=plan.folder, to_infer=list(images),
                    reason="the previous run's CSV could not be read")
            else:
                records.extend(survival_cache.records_from_counts(
                    fc.reused, cached_rows.counts_by_image, set()))
                if not stage_names and man_names:
                    stage_names = man_names
                if not cache_meta:
                    cache_meta = dict((fc.manifest or {}).get("meta") or {})
                n_relabelled += cached_rows.n_relabelled
                n_no_vector += cached_rows.n_no_vector
                src = Path((fc.manifest or {})["_run_dir"]).name
                write_log(
                    f"  REUSING {fc.n_reused} of {len(images)} image(s) from "
                    f"{src} — no model run for them."
                )
                if cached_rows.n_relabelled:
                    write_log(
                        f"  relabelled {cached_rows.n_relabelled} cached "
                        f"detection(s) at alpha {target_alpha:g}. This is not "
                        "an approximation: rescoring is an arg-max over the "
                        "per-class scores in the CSV, so these are the labels "
                        "a fresh run would have produced."
                    )
                if cached_rows.n_no_vector:
                    write_log(
                        f"  {cached_rows.n_no_vector} cached detection(s) had "
                        "no score vector and kept their original label."
                    )
                done_before += fc.n_reused
                if progress_cb is not None:
                    progress_cb(done_before, grand_total,
                                f"reused {plan.folder.name}")
        elif fc.reason:
            write_log(f"  analysing all {len(images)} image(s): {fc.reason}.")

        # --- infer whatever is left --------------------------------------
        soft_csv = None
        if fc.to_infer:
            if fc.hit:
                write_log(f"  analysing the remaining {len(fc.to_infer)} "
                          "image(s).")
            preview_dir = ((out_dir / "previews" / plan.folder.name)
                           if save_previews else None)
            soft_csv = out_dir / f"_soft_{i:02d}.csv"

            def folder_progress(done: int, total: int, name: str,
                                _base=done_before) -> None:
                if progress_cb is not None:
                    progress_cb(_base + done, grand_total, name)

            names, fresh_records, m = run_inference(
                fc.to_infer, class_conf,
                exclude_classes=exclude_classes,
                preview_dir=preview_dir,
                soft_csv=soft_csv,
                rescore=rescore,
                write_log=write_log,
                progress_cb=folder_progress,
                cancel_check=cancel_check,
            )
            records.extend(fresh_records)
            done_before += len(fresh_records)
            if names and not stage_names:
                stage_names = names
            if m and not meta:
                meta = m

        mode, frac = decide_grouping_mode(images)
        modes.append(mode)
        encoded.append(frac)
        write_log(f"  grouping: {mode} (encoded fraction {frac:.0%})")
        folder_runs.append({"plan": plan, "records": records, "mode": mode,
                            "encoded_fraction": frac})
        soft_parts.append({"plan": plan, "cached": cached_rows,
                           "fresh_csv": soft_csv})
        # `covered` is what this folder actually produced a record for —
        # cached rows plus fresh inference. It is NOT len(images): a run that
        # is cancelled, or whose subprocess dies part-way, returns fewer.
        # write_manifest refuses to mark the folder reusable when the two
        # disagree, because a missing row is indistinguishable from a real
        # zero once the run is over.
        covered = sorted({Path(r["path"]).name for r in records})
        manifest_folders.append({
            "folder": plan.folder,
            "timepoint_h": plan.hours or 0.0,
            "images": images,
            "errors": [Path(r["path"]).name for r in records if "error" in r],
            "n_rows": len(records),
            "covered": covered,
        })
        if len(covered) < len(images):
            write_log(
                f"  WARNING: {len(images) - len(covered)} of {len(images)} "
                "image(s) in this folder were never analysed. They are absent "
                "from this run's numbers, and the folder will be re-analysed "
                "next time rather than reused."
            )
        reuse_rows.append((plan.folder.name, fc.n_reused, len(fc.to_infer),
                           fc.reason))
        if cancel_check is not None and cancel_check():
            write_log("Cancelled — aggregating what was already inferred.")
            break

    if not folder_runs:
        raise RuntimeError("No images found in any of the selected folders.")

    # A fully cached run never spoke to the vision venv, so there is no meta
    # line. Fall back to the one the cached run recorded, with the rescoring
    # block replaced by what these numbers actually reflect.
    from_cache_only = not meta
    if from_cache_only and cache_meta:
        meta = dict(cache_meta)
        meta["rescore"] = {"alpha": target_alpha, "refs": dict(target_refs)}
        write_log(
            "No images needed analysing — the model was not run at all. Every "
            "detection came from previous runs, and the settings below are the "
            "ones those runs used."
        )
    if not stage_names:
        raise RuntimeError(
            "Could not determine the model's class list — neither this run nor "
            "the cached results carry it. Re-run with \"Re-analyse images\" "
            "ticked."
        )

    total_reused = sum(r[1] for r in reuse_rows)
    total_fresh = sum(r[2] for r in reuse_rows)
    write_log("=" * 60)
    write_log(f"REUSE: {total_reused} image(s) taken from previous runs, "
              f"{total_fresh} analysed now.")
    for name, n_re, n_new, reason in reuse_rows:
        line = f"  {name}: {n_re} reused, {n_new} analysed"
        if n_re == 0 and reason:
            line += f"  ({reason})"
        write_log(line)
    write_log("=" * 60)

    # Echo the alpha the vision side actually resolved, not the one we asked
    # for. This is the number that goes into run_info and the README, and the
    # only place the two could disagree is a stage_conf.json we misread.
    _alpha = float((meta.get("rescore") or {}).get("alpha") or 0.0)
    write_log(
        "Class-confidence correction ACTUALLY APPLIED: "
        + (f"ON, alpha {_alpha:g}" if _alpha else "OFF (alpha 0)")
        + ("" if bool(_alpha) == bool(rescore) else
           "  <-- WARNING: this does not match what the checkbox asked for. "
           "Check vision/stage_conf.json.")
    )

    # Drop excluded classes from the column set entirely rather than carrying a
    # column of zeros: a zero says "none found", which is a claim we did not
    # make. run_info records the exclusion so the sheet is still self-describing.
    excluded_eff = [str(c) for c in (meta.get("exclude_classes") or [])]
    if excluded_eff:
        _skip = {_norm(c) for c in excluded_eff}
        stage_names = [s for s in stage_names if _norm(s) not in _skip]
        write_log("Excluded from the report (not counted): "
                  + ", ".join(excluded_eff))

    cats, unmapped = build_stage_categories(stage_names, SURVIVAL_CONFIG)
    if unmapped:
        write_log(
            "WARNING: model reported stage(s) with no survivor mapping: "
            + ", ".join(unmapped)
            + " — counted in the 'unmapped' column and EXCLUDED from the "
              "survival denominator. They also carry no stage index, so they "
              "are outside the development readout too. Update "
              "SURVIVAL_CONFIG and STAGE_INDEX in survival.py."
        )
        log.warning("Unmapped staging classes: %s", unmapped)

    agg = aggregate(stage_names, folder_runs, cats)

    # LOUD grouping summary — the last bug of this kind hid because a wrong
    # plate count was never surfaced. Make it impossible to miss.
    mode = Counter(modes).most_common(1)[0][0] if modes else "directory"
    if len(set(modes)) > 1:
        write_log(
            "WARNING: the folders did not agree on a grouping mode "
            f"({', '.join(f'{p.folder.name}={m}' for p, m in zip(plans, modes))})"
            " — each folder was grouped on its own rule, which is correct, but "
            "check that the condition labels really do match across folders."
        )
    write_log("=" * 60)
    write_log(
        f"GROUPING: mode={mode} (encoded fraction "
        f"{(sum(encoded) / len(encoded) if encoded else 0):.0%}) — "
        f"{agg['n_conditions']} condition(s), {agg['n_plates']} plate(s), "
        f"{agg['n_images_ok']} image(s), {len(agg['timepoints'])} timepoint(s)"
    )
    if agg["n_unparsed"]:
        write_log(
            f"WARNING: {agg['n_unparsed']} image(s) had no dose+plate tokens in "
            f"filename mode — grouped under condition '{_UNPARSED_CONDITION}' "
            f"(visible in the sheets, not silently dropped): "
            + ", ".join(f"{k}={v}" for k, v in agg["unparsed_by_folder"].items())
        )
        log.warning("Development: %d unparsed image(s)", agg["n_unparsed"])
    if agg["n_error"]:
        write_log(f"WARNING: {agg['n_error']} image(s) errored during "
                  "inference and are in no count. See the qc sheet for which "
                  "conditions lost them.")
    write_log("=" * 60)

    summary = {
        "mode": mode,
        "encoded_fraction": (sum(encoded) / len(encoded)) if encoded else 0.0,
        "n_conditions": agg["n_conditions"],
        "n_plates": agg["n_plates"],
        "n_images": grand_total,
        "n_unparsed": agg["n_unparsed"],
        "n_reused": total_reused,
        "n_analysed": total_fresh,
        "n_relabelled": n_relabelled,
        "from_cache_only": from_cache_only,
    }

    # --- body size ---------------------------------------------------------
    soft_path = out_dir / _SOFT_SCORES_NAME
    survival_size.write_merged_soft_csv(soft_path, soft_parts, write_log)

    # Physical scale, read from each image's OWN TIFF tags. Deliberately done
    # here and not in the vision subprocess: scale is a property of the image,
    # not of the inference settings, so this keeps the detection cache key
    # unchanged AND scales detections replayed from an earlier run exactly like
    # fresh ones — which is most of the rows in a combining run. See
    # survival_scale's module docstring.
    scale_report = survival_scale.scan(all_images)
    write_log("Spatial calibration: " + scale_report.describe() + ".")
    scale_by_key: dict[tuple, Optional[float]] = {}
    for plan, images in zip(plans, per_folder_images):
        if plan.hours is None:
            continue
        tag = f"{plan.hours:g}"
        for p in images:
            k = (tag, p.name)
            v = scale_report.by_path.get(p)
            # Same basename twice in one timepoint with different scales: the
            # size join cannot tell those images apart either, so record the
            # ambiguity rather than pick one.
            scale_by_key[k] = None if (k in scale_by_key
                                       and scale_by_key[k] != v) else v
    size = survival_size.build_size_payload(soft_path, agg["per_image"],
                                            write_log,
                                            scale_by_key=scale_by_key,
                                            scale_report=scale_report)

    # --- workbook ----------------------------------------------------------
    survival_excel.index_map = dict(STAGE_INDEX)
    out_xlsx = out_dir / _RESULTS_NAME
    survival_excel.write_workbook(
        out_xlsx, agg, stage_names=stage_names, cats=cats, unmapped=unmapped,
        meta=meta, plans=plans, summary=summary, size=size, write_log=write_log,
    )

    # --- figures + explorer (bonus outputs; never fail the run) ------------
    pngs = survival_figures.write_figures(out_dir, agg, size, write_log)
    explorer = survival_explorer.write_explorer(
        out_dir / _EXPLORER_NAME, agg, size, meta, plans, summary, write_log)

    # --- manifest, so THIS run can be reused in turn -----------------------
    for entry in manifest_folders:
        entry["n_rows"] = sum(
            1 for r in agg["per_image"]
            if r["folder"] == Path(entry["folder"]).name)
    survival_cache.write_manifest(
        out_dir, digest=digest, meta=meta, stage_names=stage_names,
        previews=save_previews, folders=manifest_folders, write_log=write_log)

    _console_summary(agg, write_log)

    n_plates = len(agg["per_plate"])
    write_log(
        f"done: {n_plates} plate(s), {len(agg['per_condition'])} "
        f"condition × timepoint cell(s), {agg['n_error']} image error(s) "
        f"-> {out_dir}"
    )
    return {
        "n_plates": n_plates,
        "n_error": agg["n_error"],
        "out_dir": out_dir,
        "out_xlsx": out_xlsx,
        "outputs": [out_xlsx, soft_path] + pngs
                   + ([explorer] if explorer else []),
        "reuse_note": _reuse_note(total_reused, total_fresh, n_relabelled,
                                  len(plans)),
    }


def _reuse_note(n_reused: int, n_fresh: int, n_relabelled: int,
                n_folders: int) -> str:
    """One or two sentences for the completion dialog. Empty if nothing to say.

    The point is that a run which finishes in four seconds should say why,
    rather than leaving the user wondering whether it did anything.
    """
    if not n_reused:
        return ""
    total = n_reused + n_fresh
    where = "folder" if n_folders == 1 else "folders"
    if not n_fresh:
        note = (f"No images were analysed: all {n_reused} in "
                f"{'this' if n_folders == 1 else 'these'} {where} had already "
                "been done, so the results were rebuilt from the saved "
                "detections.")
    else:
        note = (f"Reused {n_reused} of {total} images from previous runs; "
                f"only {n_fresh} needed analysing.")
    if n_relabelled:
        note += (f"\n{n_relabelled} reused detection(s) were relabelled to "
                 "match the current class-confidence setting.")
    return note


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------

def survival_preflight(folders: list[Path]) -> list[str]:
    """Return human-readable error messages; empty = OK. Same contract as
    counting_preflight / docker_utils.run_preflight.

    Takes the folder LIST. Timepoint resolution is checked separately, by
    ``resolve_timepoints`` — the UI runs both before it will start.
    """
    errors: list[str] = []

    if not folders:
        errors.append("No folders added. Use \"Add folder…\" to add at least "
                      "one.")
    for folder in folders:
        if not folder.is_dir():
            errors.append(f"Folder not found:\n    {folder}")
        elif len(find_images(folder)) == 0:
            errors.append(
                f"No images in {folder.name} "
                "(.tif/.tiff/.png/.jpg/.jpeg, checked up to 3 levels deep):\n"
                f"    {folder}"
            )
    if not _VISION_PY.exists():
        errors.append(
            f"Vision venv not found:\n    {_VISION_PY}\n"
            "Create it and install ultralytics + torch (see launcher/vision/requirements.txt)."
        )
    if not _INFER_SCRIPT.exists():
        errors.append(f"Inference script missing:\n    {_INFER_SCRIPT}")
    if not _MODEL_PATH.exists():
        errors.append(f"Staging model missing:\n    {_MODEL_PATH}")

    missing = []
    for mod in ("pandas", "numpy", "openpyxl"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        errors.append(
            "Missing launcher packages: " + ", ".join(missing) + ".\n"
            "Install them into the launcher venv:\n"
            "    pip install -r launcher/requirements.txt"
        )

    return errors


# ---------------------------------------------------------------------------
# Shared status snapshot (read-only, passed to UI thread)
# ---------------------------------------------------------------------------

@dataclass
class SurvivalSnapshot:
    color: str
    label: str
    running: bool
    current_index: int
    total: int
    current_basename: str
    current_stage: str


class SurvivalStatus:
    """Shared state between the survival worker thread and the UI thread.

    Write contract — worker thread ONLY: call update() or mark_completed().
    Read contract  — UI thread ONLY: call snapshot() or pop_completed().
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._color = "gray"
        self._label = "Idle"
        self._running = False
        self._current_index = 0
        self._total = 0
        self._current_basename = ""
        self._current_stage = ""
        self._completed_result: Optional[dict] = None

    def update(
        self,
        *,
        color: str,
        label: str,
        running: bool = False,
        current_index: int = 0,
        total: int = 0,
        current_basename: str = "",
        current_stage: str = "",
    ) -> None:
        with self._lock:
            self._color = color
            self._label = label
            self._running = running
            self._current_index = current_index
            self._total = total
            self._current_basename = current_basename
            self._current_stage = current_stage

    def mark_failed(self, error: str, out_dir: Optional[Path] = None) -> None:
        """A run that died, surfaced through the SAME channel as a success.

        Without this a crash set the dot red, wrote a truncated label and
        stopped — no message, nothing to open — which from the outside is
        exactly what a run that quietly did nothing looks like. That is how a
        broken call signature survived several rounds of "it does not show me
        a message".
        """
        with self._lock:
            self._color = "red"
            self._label = "Analysis failed — see log"
            self._running = False
            self._current_stage = ""
            self._completed_result = {
                "failed": True,
                "error": error,
                "n_ok": 0,
                "n_fail": 0,
                "out_dir": out_dir,
                "note": "",
            }

    def mark_completed(self, n_ok: int, n_fail: int, out_dir: Path,
                       note: str = "") -> None:
        """``note`` is free text the UI shows with the result — currently what,
        if anything, was reused instead of re-analysed."""
        with self._lock:
            self._color = "green"
            self._label = f"Analysis complete: {n_ok} plate(s)"
            self._running = False
            self._current_stage = ""
            self._completed_result = {
                "failed": False,
                "n_ok": n_ok,
                "n_fail": n_fail,
                "out_dir": out_dir,
                "note": note,
            }

    def snapshot(self) -> SurvivalSnapshot:
        with self._lock:
            return SurvivalSnapshot(
                color=self._color,
                label=self._label,
                running=self._running,
                current_index=self._current_index,
                total=self._total,
                current_basename=self._current_basename,
                current_stage=self._current_stage,
            )

    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def pop_completed(self) -> Optional[dict]:
        with self._lock:
            result = self._completed_result
            self._completed_result = None
            return result


# ---------------------------------------------------------------------------
# Survival agent
# ---------------------------------------------------------------------------

class SurvivalAgent(threading.Thread):
    """Background thread for Development analysis. Idle until start_analysis().

    Class name kept (see the module docstring): the rename is user-facing only.
    """

    def __init__(self, settings: object, status: SurvivalStatus) -> None:
        super().__init__(daemon=True, name="SurvivalAgent")
        self._lock = threading.Lock()
        self._settings = settings
        self.status = status
        self._stop = threading.Event()
        self._cancel = threading.Event()
        self._wake = threading.Event()
        self._plans: Optional[list[FolderPlan]] = None
        self._class_conf: dict[str, float] = {}
        self._exclude_classes: Optional[list[str]] = None
        self._save_previews: bool = False
        self._rescore: bool = True
        self._force_reanalyze: bool = False
        self._last_out_dir: Optional[Path] = None

    def update_settings(self, settings: object) -> None:
        with self._lock:
            self._settings = settings

    def stop(self) -> None:
        self._stop.set()
        self._cancel.set()
        self._wake.set()

    def cancel(self) -> None:
        """UI thread: cancel the current run. Does not stop the thread."""
        self._cancel.set()

    def start_analysis(
        self,
        plans: list[FolderPlan],
        class_conf: Optional[dict[str, float]] = None,
        save_previews: bool = False,
        exclude_classes: Optional[list[str]] = None,
        rescore: bool = True,
        force_reanalyze: bool = False,
    ) -> None:
        """UI thread: trigger a Development run over the resolved folder plans.

        ``plans`` comes from resolve_timepoints() and must already be free of
        errors — the dialog refuses to start otherwise, because a folder whose
        timepoint could not be resolved would otherwise land silently at 0 h.

        class_conf maps stage name -> confidence floor. None or {} means "use
        vision/stage_conf.json", which is also what the Analyze-on-laptop
        button uses, so the two paths agree by construction.

        ``rescore`` is a switch: True lets stage_conf.json's alpha stand, False
        forces 0. No alpha value passes through this layer.

        ``force_reanalyze`` ignores every previous run's saved detections and
        sends every image through the model again.
        """
        with self._lock:
            self._plans = list(plans)
            self._class_conf = dict(class_conf or {})
            self._exclude_classes = (None if exclude_classes is None
                                     else list(exclude_classes))
            self._save_previews = save_previews
            self._rescore = bool(rescore)
            self._force_reanalyze = bool(force_reanalyze)
        self.status.update(
            running=True,
            total=0,
            current_basename="",
            current_stage="Starting…",
            color="yellow",
            label="Starting analysis…",
        )
        self._cancel.clear()
        self._wake.set()

    def run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait()
            self._wake.clear()
            if self._stop.is_set():
                break
            with self._lock:
                plans = self._plans
                class_conf = self._class_conf
                exclude_classes = self._exclude_classes
                save_previews = self._save_previews
                rescore = self._rescore
                force_reanalyze = self._force_reanalyze
                self._plans = None
            if plans:
                self._cancel.clear()
                try:
                    self._run_analysis(plans, class_conf, save_previews,
                                       exclude_classes, rescore,
                                       force_reanalyze)
                except Exception as exc:
                    log.exception("SurvivalAgent crashed")
                    # Report it through the SAME channel as a success. Before
                    # this, a crash set the dot red and nothing else — no
                    # dialog, nothing to open — which is indistinguishable from
                    # a run that quietly did nothing.
                    self.status.mark_failed(f"{type(exc).__name__}: {exc}",
                                            self._last_out_dir)

    def _run_analysis(
        self, plans: list[FolderPlan], class_conf: dict[str, float],
        save_previews: bool, exclude_classes: Optional[list[str]] = None,
        rescore: bool = True, force_reanalyze: bool = False,
    ) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        # Everything lands under the FIRST folder — with several folders there
        # is no neutral place to put it, and the first one is the one the user
        # picked first.
        out_dir = plans[0].folder / f"{_ANALYSIS_PREFIX}_{timestamp}"
        out_dir.mkdir(parents=True, exist_ok=True)
        # Remembered so a crash can point the user at the folder holding the log.
        self._last_out_dir = out_dir
        log_path = out_dir / "log.txt"

        with open(log_path, "w", encoding="utf-8") as lf:

            def write_log(msg: str) -> None:
                lf.write(msg + "\n")
                lf.flush()
                log.info("[development] %s", msg)

            self.status.update(
                color="yellow",
                label="Discovering images…",
                running=True,
                current_stage="Discovering images…",
            )

            def progress_cb(done: int, total: int, name: str) -> None:
                self.status.update(
                    color="yellow",
                    label=f"{name} ({done}/{total})",
                    running=True,
                    current_index=done,
                    total=total,
                    current_basename=name,
                    current_stage=f"{done}/{total} done",
                )

            write_log(f"Development run: {timestamp}")
            try:
                result = analyze(
                    plans, class_conf, save_previews, out_dir,
                    exclude_classes=exclude_classes,
                    rescore=rescore,
                    force_reanalyze=force_reanalyze,
                    write_log=write_log,
                    progress_cb=progress_cb,
                    cancel_check=self._cancel.is_set,
                )
            except Exception:
                # The traceback belongs in this run's own log.txt, next to the
                # lines that led up to it — not only in the launcher log, which
                # nobody thinks to open.
                write_log("=" * 60)
                write_log("RUN FAILED. Traceback follows — send this file.")
                write_log(traceback.format_exc())
                write_log("=" * 60)
                raise

            n_plates = result["n_plates"]
            n_error = result["n_error"]
            log.info(
                "Development analysis complete: %d plate(s), %d image "
                "error(s). Results: %s", n_plates, n_error, out_dir,
            )
            self.status.mark_completed(n_plates, n_error, out_dir,
                                       note=result.get("reuse_note", ""))
