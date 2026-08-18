"""
Body size from soft_stage_scores.csv.

size_px = sqrt(w * h) of the detection box, in full-frame pixels — already a
column in the CSV the vision side writes. Distributions are estimated in LOG
space: growth is multiplicative, so a fixed bandwidth in linear space
over-smooths the tight L1 peak and under-smooths the broad adult tail.

UNITS. When every joined detection's image carries a spatial calibration (see
survival_scale), sizes are converted to MICROMETRES and everything downstream —
grid, percentiles, histogram edges, figure axis, workbook headers, explorer —
follows the payload's "unit" field. If even one image is uncalibrated the WHOLE
run stays in pixels and says so: a distribution that is micrometres for some
plates and pixels for others is not a distribution, and silently defaulting the
missing ones to a nominal scale would bury a real magnification change.

APPARENT SIZE, NOT LENGTH. sqrt(w * h) of an axis-aligned box grows about 2.5x
from L1 to adult where real body length grows 4-5x, because older animals coil
more and a box does not care. Converting it to micrometres makes it comparable
across magnifications; it does not make it a body length, and nothing here or
downstream should call it one. (The box diagonal is no better on this point —
measured 2.6x — so this is a property of boxes, not of the choice of formula.)

Pure numpy. The launcher venv has scipy, but the KDE is eight lines and keeping
it dependency-free means this module also runs inside the vision venv if it
ever needs to.

Ported from the population-study prototype (explorer_generator.py), unchanged
in its numerics: the bandwidth rule, the grid padding and the density->size
conversion are the ones whose output David already signed off on.
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

# The KDE itself now lives in assay_common, unchanged, so motility, crawling and
# counting draw their distributions with the same numerics this module's output
# was signed off on. _kde_log stays as the name this module calls.
from assay_common import GRID_N as _GRID_N            # noqa: E402
from assay_common import kde_log as _kde_log          # noqa: E402


def write_merged_soft_csv(out_path: Path, parts: list[dict],
                          write_log: Callable[[str], None]) -> int:
    """Write the run's single soft_stage_scores.csv from every source.

    ``parts`` is one dict per folder: {"plan": FolderPlan, "cached": CachedRows
    or None, "fresh_csv": Path or None}. Cached rows arrive carrying the folder
    and timepoint of the run that produced them; both columns are REWRITTEN to
    this run's values, because the same folder can legitimately be given a
    different timepoint today and the body-size join keys on it.

    Returns the number of data rows written. The per-folder files inference
    wrote are removed afterwards — one merged CSV is the agreed output, and
    leaving both invites someone to analyse the wrong one.
    """
    base_header: Optional[list] = None
    for part in parts:
        cached = part.get("cached")
        if cached is not None and base_header is None:
            base_header = list(cached.header[2:])
        fresh = part.get("fresh_csv")
        if fresh and Path(fresh).exists():
            try:
                with open(fresh, newline="", encoding="utf-8") as fh:
                    head = next(csv.reader(fh))
            except (OSError, StopIteration, csv.Error):
                continue
            if base_header is None:
                base_header = list(head)
            elif list(head) != base_header:
                # Should be impossible: the cache digest covers the model, and
                # the model decides the columns. Say so loudly rather than
                # interleaving two different row shapes.
                write_log(
                    "WARNING: a cached soft-score file has different columns "
                    "from this run's. The cached rows were DROPPED and only "
                    "freshly analysed detections are in the CSV. Re-run with "
                    "\"Re-analyse images\" ticked to rebuild it cleanly."
                )
                for p2 in parts:
                    p2["cached"] = None
                base_header = list(head)
    if base_header is None:
        write_log("soft scores: nothing to write.")
        return 0

    n = 0
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["folder", "timepoint_h"] + base_header)
        for part in parts:
            plan = part["plan"]
            tag = [plan.folder.name, f"{plan.hours:g}"]
            cached = part.get("cached")
            if cached is not None:
                for row in cached.rows:
                    w.writerow(tag + list(row[2:]))
                    n += 1
            fresh = part.get("fresh_csv")
            if fresh and Path(fresh).exists():
                with open(fresh, newline="", encoding="utf-8") as src:
                    r = csv.reader(src)
                    try:
                        next(r)
                    except StopIteration:
                        continue
                    for row in r:
                        w.writerow(tag + row)
                        n += 1
    for part in parts:
        fresh = part.get("fresh_csv")
        if fresh:
            try:
                Path(fresh).unlink()
            except OSError:
                pass
    write_log(f"Wrote {out_path} ({n} detection row(s) across "
              f"{len(parts)} folder(s))")
    return n


def build_size_payload(soft_csv: Path, per_image_rows: list[dict],
                       write_log: Callable[[str], None],
                       scale_by_key: Optional[dict] = None,
                       scale_report=None) -> Optional[dict]:
    """Group every detection's size by (condition, timepoint) and summarise.

    Returns None when the CSV has no usable sizes — the caller then skips the
    body-size figure and the two size sheets rather than drawing an empty axis.

    The join is on (timepoint, image basename), because that is all the vision
    side records. Within one folder a basename is normally unique; if the same
    basename appears under two different conditions (possible, since discovery
    recurses up to three levels), those rows are DROPPED and the collision is
    logged. Silently attributing an animal to the wrong condition would be
    worse than a smaller n.

    ``scale_by_key`` maps the same (timepoint, image basename) key to that
    image's micrometres per pixel, or to None where the image carries no
    calibration. Pass it to get micrometres; omit it to stay in pixels. The
    decision is all-or-nothing over the detections that actually joined — see
    the module docstring — and the unit that won is in the returned payload.
    """
    import numpy as np

    if not soft_csv.exists():
        write_log("body size: soft_stage_scores.csv missing — size outputs skipped.")
        return None

    # (timepoint, image) -> condition, or None when ambiguous
    index: dict[tuple[str, str], Optional[dict]] = {}
    for r in per_image_rows:
        key = (f"{r['timepoint_h']:g}", r["image"])
        prev = index.get(key, "missing")
        if prev == "missing":
            index[key] = r
        elif prev is not None and (prev["condition"] != r["condition"]
                                   or prev["plate"] != r["plate"]):
            index[key] = None

    rows_by_group: dict[tuple, list[float]] = {}
    scales_by_group: dict[tuple, list[Optional[float]]] = {}
    meta_by_group: dict[tuple, dict] = {}
    n_total = 0
    n_unjoined = 0
    n_ambiguous = 0
    with open(soft_csv, newline="", encoding="utf-8") as fh:
        rd = csv.DictReader(fh)
        if "size_px" not in (rd.fieldnames or []):
            write_log("body size: soft_stage_scores.csv has no size_px column "
                      "— size outputs skipped.")
            return None
        for row in rd:
            key = (row.get("timepoint_h", ""), row.get("image", ""))
            hit = index.get(key, "missing")
            if hit == "missing":
                n_unjoined += 1
                continue
            if hit is None:
                n_ambiguous += 1
                continue
            try:
                size = float(row["size_px"])
            except (TypeError, ValueError):
                continue
            if not (size > 0):
                continue
            g = (hit["timepoint_h"], hit["condition"])
            rows_by_group.setdefault(g, []).append(size)
            scales_by_group.setdefault(g, []).append(
                (scale_by_key or {}).get(key))
            meta_by_group[g] = hit
            n_total += 1

    if n_unjoined:
        write_log(f"body size: {n_unjoined} detection row(s) could not be "
                  "matched to an image row (excluded images or a stale CSV) — "
                  "left out of the size figure.")
    if n_ambiguous:
        write_log(f"body size: {n_ambiguous} detection row(s) had an ambiguous "
                  "filename (the same basename under two conditions) — dropped "
                  "rather than guessed.")
    if not n_total:
        write_log("body size: no detections joined — size outputs skipped.")
        return None

    # --- unit: micrometres if EVERY joined detection has a scale, else pixels
    # for the whole run. All-or-nothing, because a mixed-unit distribution is
    # not a distribution and a substituted default would hide a magnification
    # change. See the module docstring.
    n_unscaled = sum(1 for v in scales_by_group.values()
                     for s in v if not (s and s > 0))
    if scale_by_key is None:
        unit, unit_label = "px", "px"
        scale_note = "no image scales were supplied; sizes are in pixels"
    elif n_unscaled:
        unit, unit_label = "px", "px"
        scale_note = (
            f"{n_unscaled:,} of {n_total:,} detection(s) came from images with "
            "no spatial calibration, so the WHOLE run stays in pixels — "
            "micrometres for some plates and pixels for others would not be "
            "comparable")
        write_log("body size: " + scale_note + ".")
    else:
        unit, unit_label = "um", "µm"
        for g, sizes in rows_by_group.items():
            scales = scales_by_group[g]
            rows_by_group[g] = [s * k for s, k in zip(sizes, scales)]
        scale_note = "converted to micrometres from each image's own TIFF tags"
        if scale_report is not None:
            write_log("body size: " + scale_report.describe() + ".")
        write_log("body size: sizes are in µm — " + scale_note + ".")
    write_log(
        "body size: this is APPARENT size, √(w·h) of the detection box, not a "
        "body length — a coiled animal reads smaller than a straight one of "
        "the same length.")

    all_sizes = np.array([s for v in rows_by_group.values() for s in v],
                         dtype=float)
    lo = float(np.log(max(float(np.quantile(all_sizes, 0.002)), 1e-6)))
    hi = float(np.log(float(np.quantile(all_sizes, 0.998))))
    if not (hi > lo):
        hi = lo + 1.0
    pad = 0.18 * (hi - lo)
    grid = np.linspace(lo - pad, hi + pad, _GRID_N)
    # Bin edges in whatever unit this run settled on — px or µm. The name is
    # historical; ``unit`` in the returned payload is what says which.
    edges_px = np.exp(grid)

    groups: dict[str, dict] = {}
    for g in sorted(rows_by_group, key=lambda k: (k[0], str(k[1]))):
        tp, condition = g
        vals = np.array(rows_by_group[g], dtype=float)
        meta = meta_by_group[g]
        y = _kde_log(vals, grid, np)
        hist, _ = np.histogram(vals, bins=edges_px)
        key = f"{condition} @ {tp:g}h"
        groups[key] = {
            "condition": condition,
            "strain": meta["strain"],
            "dose": meta["dose"],
            "unit": meta["unit"],
            "timepoint_h": tp,
            "n": int(vals.size),
            "hist": [int(v) for v in hist],
            "y": None if y is None else [round(float(v), 5) for v in y],
            "p10": round(float(np.percentile(vals, 10)), 2),
            "p25": round(float(np.percentile(vals, 25)), 2),
            "p50": round(float(np.percentile(vals, 50)), 2),
            "p75": round(float(np.percentile(vals, 75)), 2),
            "p90": round(float(np.percentile(vals, 90)), 2),
            "mean": round(float(vals.mean()), 2),
            "gmean": round(float(np.exp(np.log(vals).mean())), 2),
        }
        if y is None:
            write_log(f"body size: {key} has {vals.size} animal(s) — under the "
                      "8 needed for a density curve; its percentiles are still "
                      "in size_summary, but it draws no curve.")

    n_curves = sum(1 for g in groups.values() if g["y"] is not None)
    write_log(f"Body size: {n_total:,} animals in {len(groups)} group(s), "
              f"{n_curves} with a density curve. Grid: "
              f"{edges_px[0]:.1f}–{edges_px[-1]:.1f} {unit_label}, "
              f"{_GRID_N - 1} bins (log-spaced; size_histogram lists them).")
    return {
        "x": [round(float(v), 4) for v in edges_px],
        "bin_edges": [float(v) for v in edges_px],
        "groups": groups,
        "n_total": n_total,
        # "px" or "um" — the machine-readable one, used for column names.
        "unit": unit,
        # "px" or "µm" — for axis labels and prose.
        "unit_label": unit_label,
        "scale_note": scale_note,
        "um_per_px_min": getattr(scale_report, "min", None) if unit == "um" else None,
        "um_per_px_median": getattr(scale_report, "median", None) if unit == "um" else None,
        "um_per_px_max": getattr(scale_report, "max", None) if unit == "um" else None,
    }
