"""
Worm-survival analysis (3.13 side).

Runs the YOLO staging model over a folder of plate images and turns the
developmental-stage calls into a survival readout with per-image / per-plate /
per-condition statistics and an Excel report.

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
import threading
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

_ANALYSIS_PREFIX = "_survival"

# Discovery mirrors analysis/counting.py so both sides agree on the image set.
_IMAGE_EXTS = (".tif", ".tiff", ".png", ".jpg", ".jpeg")
_MAX_DEPTH = 3

# --- vision venv locations (inference lives ONLY here) ----------------------
_VISION_DIR = Path(__file__).parent / "vision"
_VISION_PY = _VISION_DIR / ".venv-vision" / "Scripts" / "python.exe"
_INFER_SCRIPT = _VISION_DIR / "infer_stage.py"
_MODEL_PATH = _VISION_DIR / "models" / "staging.pt"
_STAGE_CONF = _VISION_DIR / "stage_conf.json"


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


# --- condition grammar (reused from the viewer generators) ------------------
# "<strain> <dose><unit>", e.g. "601 20J" / "N2 100 uM". <unit> is J (rendered
# J/m²) or uM/µM (rendered µM). Mirrors make_video_viewer.py COND_RE.
_COND_RE = re.compile(r"^(?P<strain>.+?)\s+(?P<dose>\d+)\s*(?P<unit>[Jj]|[uUµ][Mm])$")


def _canon_unit(token: str) -> str:
    if token in ("J", "j"):
        return "J/m²"
    return "µM"  # u/µ + M


def parse_condition(name: str):
    """Return (strain, dose:int, unit) or None if the name isn't the grammar."""
    m = _COND_RE.match(name.strip())
    if not m:
        return None
    return m.group("strain"), int(m.group("dose")), _canon_unit(m.group("unit"))


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

    write_log(f"Inference: {' '.join(cmd)}")
    write_log(f"Feeding {len(images)} image path(s) to the vision venv…")

    proc = subprocess.Popen(
        cmd, cwd=str(_VISION_DIR),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", bufsize=1,
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
# Aggregation
# ---------------------------------------------------------------------------

def aggregate(
    stage_names: list[str],
    records: list[dict],
    root: Path,
    cats: dict[str, str],
    mode: str,
):
    """Build per_image / per_plate / per_condition / dose_response rows.

    Returns a dict of lists-of-dicts keyed by sheet name, plus n_error and the
    grouping counts (n_conditions/n_plates/n_images/n_unparsed). Grouping uses
    `mode` ('filename' or 'directory'). Uses numpy for the per-condition
    mean/SD/min/max over plate survival %.
    """
    import numpy as np

    stage_cols = list(stage_names)

    per_image: list[dict] = []
    n_error = 0
    n_unparsed = 0
    # plate_counts[(condition, plate)] -> Counter of stage -> n; plus n_images
    plate_counts: dict[tuple[str, str], Counter] = {}
    plate_nimg: dict[tuple[str, str], int] = {}

    for rec in records:
        if "error" in rec:
            n_error += 1
            continue
        path = Path(rec["path"])
        condition, plate = resolve_record(path, root, mode)
        if condition == _UNPARSED_CONDITION:
            n_unparsed += 1
        counts = {k: int(v) for k, v in rec.get("counts", {}).items()}
        surv, non, exc, unm = _tally(counts, cats)
        total = surv + non + exc + unm
        row = {"condition": condition, "plate": plate, "image": path.name}
        for st in stage_cols:
            row[st] = counts.get(st, 0)
        row["unmapped"] = unm
        row["total"] = total
        row["n_survivors"] = surv
        row["n_non_survivors"] = non
        row["survival_pct"] = _survival_pct(surv, non)
        per_image.append(row)

        key = (condition, plate)
        agg = plate_counts.setdefault(key, Counter())
        for st, c in counts.items():
            agg[st] += c
        plate_nimg[key] = plate_nimg.get(key, 0) + 1

    # per_plate
    per_plate: list[dict] = []
    # survival % per (condition -> list of plate survival %) and pooled counters
    cond_plate_surv: dict[str, list[float]] = {}
    cond_pooled: dict[str, Counter] = {}
    cond_nplates: dict[str, int] = {}
    # for dose_response: (condition, plate) survival keyed for grouping later
    for (condition, plate), agg in plate_counts.items():
        counts = dict(agg)
        surv, non, exc, unm = _tally(counts, cats)
        total = surv + non + exc + unm
        pct = _survival_pct(surv, non)
        row = {"condition": condition, "plate": plate}
        for st in stage_cols:
            row[st] = counts.get(st, 0)
        row["unmapped"] = unm
        row["total"] = total
        row["n_survivors"] = surv
        row["n_non_survivors"] = non
        row["survival_pct"] = pct
        row["n_images"] = plate_nimg[(condition, plate)]
        per_plate.append(row)

        cond_plate_surv.setdefault(condition, []).append(pct)
        pooled = cond_pooled.setdefault(condition, Counter())
        for st, c in counts.items():
            pooled[st] += c
        cond_nplates[condition] = cond_nplates.get(condition, 0) + 1

    # per_condition
    per_condition: list[dict] = []
    for condition in sorted(cond_pooled):
        pcts = np.array(
            [p for p in cond_plate_surv.get(condition, []) if not np.isnan(p)],
            dtype=float,
        )
        n_plates = cond_nplates[condition]
        pooled = dict(cond_pooled[condition])
        p_surv, p_non, p_exc, p_unm = _tally(pooled, cats)
        p_total = p_surv + p_non + p_exc + p_unm
        row = {
            "condition": condition,
            "n_plates": n_plates,
            "survival_pct_mean": float(np.mean(pcts)) if pcts.size else float("nan"),
            "survival_pct_sd": float(np.std(pcts, ddof=1)) if pcts.size > 1 else float("nan"),
            "survival_pct_min": float(np.min(pcts)) if pcts.size else float("nan"),
            "survival_pct_max": float(np.max(pcts)) if pcts.size else float("nan"),
        }
        for st in stage_cols:
            row[st] = pooled.get(st, 0)
        row["unmapped"] = p_unm
        row["pooled_total"] = p_total
        row["pooled_survival_pct"] = _survival_pct(p_surv, p_non)
        per_condition.append(row)

    # dose_response — group plate survival % by (strain, dose, unit)
    dose_groups: dict[tuple[str, int, str], list[float]] = {}
    for condition, pcts in cond_plate_surv.items():
        parsed = parse_condition(condition)
        if parsed is None:
            continue
        strain, dose, unit = parsed
        dose_groups.setdefault((strain, dose, unit), []).extend(pcts)

    dose_response: list[dict] = []
    for (strain, dose, unit) in sorted(dose_groups, key=lambda k: (str(k[0]), k[1])):
        arr = np.array(
            [p for p in dose_groups[(strain, dose, unit)] if not np.isnan(p)],
            dtype=float,
        )
        dose_response.append({
            "strain": strain,
            "dose": dose,
            "unit": unit,
            "survival_pct_mean": float(np.mean(arr)) if arr.size else float("nan"),
            "survival_pct_sd": float(np.std(arr, ddof=1)) if arr.size > 1 else float("nan"),
            "n_plates": len(dose_groups[(strain, dose, unit)]),
        })

    return {
        "per_image": per_image,
        "per_plate": per_plate,
        "per_condition": per_condition,
        "dose_response": dose_response,
        "stage_cols": stage_cols,
        "n_error": n_error,
        "n_unparsed": n_unparsed,
        "n_conditions": len(per_condition),
        "n_plates": len(per_plate),
        "n_images_ok": len(per_image),
    }


# ---------------------------------------------------------------------------
# Excel + console summary
# ---------------------------------------------------------------------------

def write_excel(
    out_path: Path,
    agg: dict,
    *,
    stage_names: list[str],
    unmapped: list[str],
    meta: dict,
    n_images: int,
    summary: dict,
    write_log: Callable[[str], None],
) -> None:
    import pandas as pd

    stage_cols = agg["stage_cols"]

    per_image_cols = (
        ["condition", "plate", "image"] + stage_cols
        + ["unmapped", "total", "n_survivors", "n_non_survivors", "survival_pct"]
    )
    per_plate_cols = (
        ["condition", "plate"] + stage_cols
        + ["unmapped", "total", "n_survivors", "n_non_survivors",
           "survival_pct", "n_images"]
    )
    per_condition_cols = (
        ["condition", "n_plates", "survival_pct_mean", "survival_pct_sd",
         "survival_pct_min", "survival_pct_max"] + stage_cols
        + ["unmapped", "pooled_total", "pooled_survival_pct"]
    )
    dose_cols = ["strain", "dose", "unit", "survival_pct_mean",
                 "survival_pct_sd", "n_plates"]

    def _df(rows, cols):
        return pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)

    # Thresholds/tiling are echoed from the inference meta line rather than from
    # what we asked for, so the report records what actually ran.
    eff_conf = meta.get("class_conf") or {}
    seam = meta.get("seam") or {}
    overlap = meta.get("overlap", "?")

    run_info = [
        ("timestamp", datetime.now().isoformat(timespec="seconds")),
        ("model_path", str(_MODEL_PATH)),
        ("conf_min", meta.get("conf", "")),
        ("conf_per_class",
         ", ".join(f"{k}={float(v):.2f}" for k, v in eff_conf.items())
         or "(uniform)"),
        ("tile", f"676x608 (tiled_infer, overlap {overlap})"),
        ("seam_suppression",
         f"margin {seam.get('margin_px')} px, cover {seam.get('cover_frac')}"
         if seam.get("cover_frac") is not None else "off"),
        ("class_agnostic_iou", meta.get("class_agnostic_iou") or "off"),
        ("class_size_px",
         ", ".join(f"{k}={list(v)}" for k, v in
                   (meta.get("class_size_px") or {}).items()) or "off"),
        ("excluded_classes",
         ", ".join(meta.get("exclude_classes") or []) or "(none)"),
        ("excluded_note",
         "excluded classes were NOT detected — their absence is not a count of 0"
         if meta.get("exclude_classes") else ""),
        ("grouping_mode", summary.get("mode", "")),
        ("encoded_fraction", f"{summary.get('encoded_fraction', 0.0):.2f}"),
        ("n_conditions", summary.get("n_conditions", 0)),
        ("n_plates", summary.get("n_plates", 0)),
        ("image_count", n_images),
        ("n_unparsed_images", summary.get("n_unparsed", 0)),
        ("model_classes", ", ".join(stage_names)),
        ("survivors", ", ".join(SURVIVAL_CONFIG["survivors"])),
        ("non_survivors", ", ".join(SURVIVAL_CONFIG["non_survivors"])),
        ("excluded", ", ".join(SURVIVAL_CONFIG["excluded"])),
        ("unmapped_stages", ", ".join(unmapped) if unmapped else "(none)"),
        ("survival_formula", "survivors / (survivors + non_survivors) * 100"),
        ("vision_python", str(_VISION_PY)),
    ]

    with pd.ExcelWriter(out_path, engine="openpyxl") as xw:
        _df(run_info, ["key", "value"]).to_excel(xw, sheet_name="run_info", index=False)
        _df(agg["per_image"], per_image_cols).to_excel(xw, sheet_name="per_image", index=False)
        _df(agg["per_plate"], per_plate_cols).to_excel(xw, sheet_name="per_plate", index=False)
        _df(agg["per_condition"], per_condition_cols).to_excel(
            xw, sheet_name="per_condition", index=False)
        if agg["dose_response"]:
            _df(agg["dose_response"], dose_cols).to_excel(
                xw, sheet_name="dose_response", index=False)

    write_log(f"Wrote {out_path}")


def _console_summary(agg: dict, write_log: Callable[[str], None]) -> None:
    write_log("Per-condition survival %:")
    for row in agg["per_condition"]:
        mean = row["survival_pct_mean"]
        sd = row["survival_pct_sd"]
        mn = row["survival_pct_min"]
        mx = row["survival_pct_max"]
        sd_s = "nan" if sd != sd else f"{sd:.1f}"
        mean_s = "nan" if mean != mean else f"{mean:.1f}"
        write_log(
            f"  {row['condition']}: {mean_s} ± {sd_s}% "
            f"(min {mn:.1f}, max {mx:.1f}, n={row['n_plates']} plates)"
        )


def write_survival_curve(
    out_png: Path, agg: dict, write_log: Callable[[str], None]
) -> None:
    """Render a dose-response survival curve (one line per strain, SD error bars,
    jittered per-plate scatter) to out_png.

    Bonus output: every failure — including matplotlib being absent — is logged
    and swallowed so the Excel (primary) always completes. Survival % here is the
    per-plate value already computed under SURVIVAL_CONFIG; this does NOT redefine
    survivors.
    """
    try:
        from collections import defaultdict

        import numpy as np
        import matplotlib
        matplotlib.use("Agg")  # headless; no GUI backend
        import matplotlib.pyplot as plt

        # strain -> {dose: [per-plate survival %, ...]}; collect unit for x-label.
        by_strain: dict[str, dict[int, list[float]]] = defaultdict(
            lambda: defaultdict(list))
        units: list[str] = []
        for row in agg["per_plate"]:
            parsed = parse_condition(row["condition"])
            if parsed is None:
                continue  # e.g. '__unparsed__' — not part of the dose-response
            strain, dose, unit = parsed
            pct = row["survival_pct"]
            if pct != pct:  # NaN plate (empty denominator) — skip the point
                continue
            by_strain[strain][dose].append(pct)
            units.append(unit)

        if not by_strain:
            write_log("survival_curve: no plottable strain+dose data; skipping plot.")
            return

        unit_label = Counter(units).most_common(1)[0][0] if units else ""
        rng = np.random.default_rng(0)  # deterministic jitter

        fig, ax = plt.subplots(figsize=(7, 5))
        ns: set[int] = set()
        all_doses = sorted({d for m in by_strain.values() for d in m})
        span = (max(all_doses) - min(all_doses)) if len(all_doses) > 1 else 1

        for strain in sorted(by_strain, key=str):
            dose_map = by_strain[strain]
            doses = sorted(dose_map)
            means = [float(np.mean(dose_map[d])) for d in doses]
            sds = [float(np.std(dose_map[d], ddof=1)) if len(dose_map[d]) > 1 else 0.0
                   for d in doses]
            line = ax.errorbar(
                doses, means, yerr=sds, marker="o", capsize=4,
                linewidth=1.8, markersize=6, label=str(strain), zorder=3,
            )
            color = line[0].get_color()
            for d in doses:
                pts = dose_map[d]
                ns.add(len(pts))
                jitter = (rng.random(len(pts)) - 0.5) * span * 0.02
                ax.scatter(np.full(len(pts), d) + jitter, pts,
                           color=color, alpha=0.35, s=22, zorder=1)

        ax.set_xlabel(f"Dose ({unit_label})" if unit_label else "Dose")
        ax.set_ylabel("Survival %")
        ax.set_ylim(-2, 102)
        n_txt = (f"n = {next(iter(ns))} plates/condition" if len(ns) == 1
                 else "n varies per condition (see per_condition)")
        ax.set_title(f"Worm survival dose–response\nerror bars = SD; {n_txt}")
        ax.legend(title="Strain")
        ax.grid(True, alpha=0.25)
        fig.tight_layout()
        fig.savefig(out_png, dpi=150)
        plt.close(fig)
        write_log(f"Wrote {out_png}")
    except Exception as exc:  # never fail the run over a plot
        write_log(f"survival_curve: plot failed ({exc}); Excel is unaffected.")
        log.warning("survival_curve failed", exc_info=True)


# ---------------------------------------------------------------------------
# Orchestrator (called by the agent worker thread)
# ---------------------------------------------------------------------------

def analyze(
    root: Path,
    class_conf: dict[str, float],
    save_previews: bool,
    out_dir: Path,
    *,
    exclude_classes: Optional[list[str]] = None,
    write_log: Callable[[str], None],
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> dict:
    """End-to-end run writing into the caller-provided out_dir (so the agent's
    log.txt and the results share one folder).

    Returns {'n_plates', 'n_error', 'out_dir', 'out_xlsx'}.
    """
    preview_dir = (out_dir / "previews") if save_previews else None

    images = find_images(root)
    write_log(f"Folder: {root}")
    write_log(f"Images found: {len(images)}")
    write_log(
        "Conf (per class): "
        + (", ".join(f"{k}={float(v):.2f}" for k, v in class_conf.items())
           if class_conf else "(vision/stage_conf.json defaults)")
    )
    write_log(f"Save previews: {save_previews}")

    stage_names, records, meta = run_inference(
        images, class_conf,
        exclude_classes=exclude_classes,
        preview_dir=preview_dir,
        write_log=write_log,
        progress_cb=progress_cb,
        cancel_check=cancel_check,
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
              "survival denominator. Update SURVIVAL_CONFIG in survival.py."
        )
        log.warning("Unmapped staging classes: %s", unmapped)

    # Decide grouping mode from the full image set, then aggregate under it.
    mode, encoded_fraction = decide_grouping_mode(images)
    agg = aggregate(stage_names, records, root, cats, mode)

    # LOUD grouping summary — the last bug hid because a wrong plate count was
    # never surfaced. Make it impossible to miss (console + run_info).
    write_log("=" * 60)
    write_log(
        f"GROUPING: mode={mode} (encoded fraction {encoded_fraction:.0%}) — "
        f"{agg['n_conditions']} condition(s), {agg['n_plates']} plate(s), "
        f"{agg['n_images_ok']} image(s)"
    )
    if agg["n_unparsed"]:
        write_log(
            f"WARNING: {agg['n_unparsed']} image(s) had no dose+plate tokens in "
            f"filename mode — grouped under condition '{_UNPARSED_CONDITION}' "
            f"(visible in the sheets, not silently dropped)."
        )
        log.warning("Survival: %d unparsed image(s)", agg["n_unparsed"])
    write_log("=" * 60)

    summary = {
        "mode": mode,
        "encoded_fraction": encoded_fraction,
        "n_conditions": agg["n_conditions"],
        "n_plates": agg["n_plates"],
        "n_unparsed": agg["n_unparsed"],
    }

    out_xlsx = out_dir / "worm_survival_results.xlsx"
    write_excel(
        out_xlsx, agg,
        stage_names=stage_names, unmapped=unmapped, meta=meta,
        n_images=len(images), summary=summary, write_log=write_log,
    )
    _console_summary(agg, write_log)

    # Dose-response curve — bonus output; must never fail the run.
    if agg["dose_response"]:
        write_survival_curve(out_dir / "survival_curve.png", agg, write_log)
    else:
        write_log("survival_curve: no strain+dose conditions parsed; skipping plot.")

    n_plates = len(agg["per_plate"])
    write_log(
        f"done: {n_plates} plate(s), {len(agg['per_condition'])} condition(s), "
        f"{agg['n_error']} image error(s) -> {out_dir}"
    )
    return {
        "n_plates": n_plates,
        "n_error": agg["n_error"],
        "out_dir": out_dir,
        "out_xlsx": out_xlsx,
    }


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------

def survival_preflight(folder: Path) -> list[str]:
    """Return human-readable error messages; empty = OK. Same contract as
    counting_preflight / docker_utils.run_preflight."""
    errors: list[str] = []

    if len(find_images(folder)) == 0:
        errors.append(
            "No images found (.tif/.tiff/.png/.jpg/.jpeg, checked up to 3 levels deep)"
        )
    if not _VISION_PY.exists():
        errors.append(
            f"Vision venv not found:\n    {_VISION_PY}\n"
            "Create it (Python 3.12) and install ultralytics + torch."
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

    def mark_completed(self, n_ok: int, n_fail: int, out_dir: Path) -> None:
        with self._lock:
            self._color = "green"
            self._label = f"Analysis complete: {n_ok} plate(s)"
            self._running = False
            self._current_stage = ""
            self._completed_result = {
                "n_ok": n_ok,
                "n_fail": n_fail,
                "out_dir": out_dir,
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
    """Background thread for worm-survival analysis. Idle until start_analysis()."""

    def __init__(self, settings: object, status: SurvivalStatus) -> None:
        super().__init__(daemon=True, name="SurvivalAgent")
        self._lock = threading.Lock()
        self._settings = settings
        self.status = status
        self._stop = threading.Event()
        self._cancel = threading.Event()
        self._wake = threading.Event()
        self._folder: Optional[Path] = None
        self._class_conf: dict[str, float] = {}
        self._exclude_classes: Optional[list[str]] = None
        self._save_previews: bool = False

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
        folder: Path,
        class_conf: Optional[dict[str, float]] = None,
        save_previews: bool = False,
        exclude_classes: Optional[list[str]] = None,
    ) -> None:
        """UI thread: trigger a survival run on the given folder.

        class_conf maps stage name -> confidence floor. None or {} means "use
        vision/stage_conf.json", which is also what the Analyze-on-laptop
        button uses, so the two paths agree by construction.
        """
        with self._lock:
            self._folder = folder
            self._class_conf = dict(class_conf or {})
            self._exclude_classes = (None if exclude_classes is None
                                     else list(exclude_classes))
            self._save_previews = save_previews
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
                folder = self._folder
                class_conf = self._class_conf
                exclude_classes = self._exclude_classes
                save_previews = self._save_previews
                self._folder = None
            if folder is not None:
                self._cancel.clear()
                try:
                    self._run_analysis(folder, class_conf, save_previews,
                                       exclude_classes)
                except Exception:
                    log.exception("SurvivalAgent crashed")
                    self.status.update(
                        color="red",
                        label="Analysis crashed — see log",
                        running=False,
                    )

    def _run_analysis(
        self, folder: Path, class_conf: dict[str, float], save_previews: bool,
        exclude_classes: Optional[list[str]] = None,
    ) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        out_dir = folder / f"{_ANALYSIS_PREFIX}_{timestamp}"
        out_dir.mkdir(parents=True, exist_ok=True)
        log_path = out_dir / "log.txt"

        with open(log_path, "w", encoding="utf-8") as lf:

            def write_log(msg: str) -> None:
                lf.write(msg + "\n")
                lf.flush()
                log.info("[survival] %s", msg)

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

            write_log(f"Run: {timestamp}")
            result = analyze(
                folder, class_conf, save_previews, out_dir,
                exclude_classes=exclude_classes,
                write_log=write_log,
                progress_cb=progress_cb,
                cancel_check=self._cancel.is_set,
            )

            n_plates = result["n_plates"]
            n_error = result["n_error"]
            log.info(
                "Survival analysis complete: %d plate(s), %d image error(s). Results: %s",
                n_plates, n_error, out_dir,
            )
            self.status.mark_completed(n_plates, n_error, out_dir)
