"""The shared interactive explorer for motility, crawling and colony survival.

One template, three assays. Development keeps its own explorer: it has panels
nothing else has (stage composition, quadrant agreement) and rewriting it to fit
a generic shape would trade a working, specific view for a worse general one.
What IS shared is the layer underneath — assay_common's aggregation — so the
four explorers disagree about layout and never about arithmetic.

LAYOUT, AND WHY IT IS FACETED RATHER THAN OVERLAID. Panels are a grid of metric
(rows) x strain (columns), with dose on x and the y-axis shared across each row
so columns compare directly. The obvious alternative — one panel per metric with
a coloured line per strain — needs six or more categorical colours in a single
plot, and six hues cannot be told apart reliably under common colour-vision
deficiencies at small-multiple sizes. Facets need no categorical palette at all.
The only ramp used is an ordered one for dose, where the ordering is the point.

EVERY PANEL SHOWS ITS PLATES. The condition mean is a marker with an SD bar;
behind it sit one dot per plate. A condition whose plates disagree looks
different from one whose plates agree, without having to open the workbook —
which is the whole reason for shipping an explorer rather than another figure.

SELF-CONTAINED. The payload is inlined at the __DATA__ placeholder, so the file
survives being emailed, copied to a stick, or opened years later with no server,
no network and no sibling files.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable, Optional, Sequence

import assay_common as AC

log = logging.getLogger(__name__)

_TEMPLATE = Path(__file__).parent / "assay_explorer_template.html"
_PLACEHOLDER = "__DATA__"


def _r(v, nd=4):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, nd) if f == f and abs(f) != float("inf") else None


def build_payload(*, title: str, subtitle: str, dr_caption: str,
                  agg: AC.Aggregation, metrics: Sequence[AC.Metric],
                  caveat: str = "", dist: Optional[dict] = None,
                  dist_title: str = "", dist_caption: str = "",
                  dist_label: str = "", dist_unit: str = "",
                  survival_metric: Optional[AC.Metric] = None,
                  survival_caption: str = "",
                  norm_metric: Optional[AC.Metric] = None,
                  meta: Optional[dict] = None) -> dict:
    """Turn an Aggregation into the JSON the template draws.

    EVERY ROW CARRIES ITS TIMEPOINT. This used to drop timepoint_h on the way
    in, which meant a five-day run reached the page as thirty condition rows
    with six distinct names and every panel drew them on top of each other. The
    template's job is to decide what to show; deciding it here, by discarding a
    column, is how a viewer ends up looking at one arbitrary day and believing
    it is the experiment.
    """
    cond, plates = agg.per_condition, agg.per_plate
    tps = [float(t) for t in (getattr(agg, "timepoints", []) or [])]

    has_dose = any(c.get("dose") is not None for c in cond)
    doses = sorted({c["dose"] for c in cond if c.get("dose") is not None})
    strains = sorted({c["strain"] for c in cond}, key=AC.strain_sort_key)
    # Distinct names, in a stable order — a timecourse repeats each of them
    # once per day and the filter chips must not repeat with them.
    conditions = list(dict.fromkeys(c["condition"] for c in cond))

    cond_out = []
    for c in cond:
        cond_out.append({
            "condition": c["condition"], "strain": c["strain"],
            "dose": c["dose"], "tp": _r(c.get("timepoint_h")),
            "n_plates": c["n_plates"],
            "n_items": c["n_items"], "n_kept": c["n_kept"],
            "stats": {m.key: {"mean": _r(c.get(f"{m.key}_mean")),
                              "sd": _r(c.get(f"{m.key}_sd")),
                              "pooled": _r(c.get(f"{m.key}_pooled_median"))}
                      for m in metrics},
        })
    plates_out = []
    for p in plates:
        plates_out.append({
            "condition": p["condition"], "strain": p["strain"],
            "dose": p["dose"], "plate": p["plate"],
            "tp": _r(p.get("timepoint_h")),
            "n_items": p["n_items"], "n_kept": p["n_kept"],
            "vals": {m.key: _r(p.get(m.key)) for m in metrics},
        })

    dist_out = None
    if dist:
        dist_out = dict(dist)
        dist_out.pop("bin_edges", None)          # the workbook carries those
        dist_out.update({"title": dist_title or "Distribution",
                         "caption": dist_caption, "label": dist_label,
                         "unit": dist_unit})

    surv = None
    if survival_metric is not None:
        built = AC.survival_series(agg, survival_metric.key)
        if built is not None:
            # Capped at eight for the same reason the figure is: past that, two
            # strains would have to share a colour.
            drop = max(0, len(built["series"]) - 8)
            surv = {
                "label": survival_metric.label,
                "caption": survival_caption,
                "notes": built["notes"],
                "capped": drop,
                "series": [
                    {"strain": s["strain"], "ctrl_dose": s["ctrl_dose"],
                     "base": _r(s["base"], 3),
                     "pts": [{"dose": q["dose"], "mean": _r(q["mean"], 3),
                              "sd": _r(q["sd"], 3),
                              "vals": [_r(x, 3) for x in q["vals"]],
                              "plates": q["plates"]}
                             for q in s["pts"]]}
                    for s in built["series"][:8]],
            }

    norm = None
    if norm_metric is not None and len(tps) > 1:
        built = AC.normalised_series(agg, norm_metric.key)
        if built is not None:
            drop = max(0, len(built["series"]) - 8)
            norm = {
                "label": norm_metric.label,
                "unit": norm_metric.unit,
                "notes": built["notes"],
                "capped": drop,
                "series": [
                    {"strain": s["strain"], "dose": s["dose"],
                     "ctrl_dose": s["ctrl_dose"],
                     "t_half": _r(s["t_half"], 1),
                     "pts": [{"tp": _r(q["tp"]), "mean": _r(q["mean"], 2),
                              "sd": _r(q["sd"], 2),
                              "vals": [_r(x, 2) for x in q["vals"]],
                              "plates": q["plates"]}
                             for q in s["pts"]]}
                    for s in built["series"][:8]],
            }

    return {
        "title": title, "subtitle": subtitle, "caveat": caveat,
        "survival": surv,
        "normalised": norm,
        "dr_caption": dr_caption,
        "has_dose": has_dose,
        "dose_unit": AC.dose_unit_of(cond),
        "doses": doses, "strains": strains, "conditions": conditions,
        "timepoints": tps,
        "metrics": [{"key": m.key, "label": m.label, "unit": m.unit,
                     "note": m.note,
                     "headline": bool(getattr(m, "headline", True)),
                     "log": bool(getattr(m, "log", False))}
                    for m in metrics],
        "cond": cond_out, "plates": plates_out, "dist": dist_out,
        "meta": meta or {},
    }


def write_explorer(out_html: Path, payload: dict,
                   write_log: Callable[[str], None]) -> Optional[Path]:
    """Write a self-contained explorer. Bonus output — never fails the run.

    Mirrors survival_explorer: an assay is not worth losing because a viewer
    could not be written, so every failure is logged and swallowed.
    """
    try:
        tpl = _TEMPLATE.read_text(encoding="utf-8")
        if _PLACEHOLDER not in tpl:
            raise RuntimeError(f"{_TEMPLATE.name} has no {_PLACEHOLDER} slot")
        blob = json.dumps(payload, separators=(",", ":"), allow_nan=False)
        out_html.write_text(tpl.replace(_PLACEHOLDER, blob), encoding="utf-8")
        write_log(f"Wrote {out_html} ({out_html.stat().st_size // 1024} KB, "
                  f"{len(payload.get('cond', []))} condition(s), "
                  f"{len(payload.get('plates', []))} plate(s))")
        return out_html
    except Exception as exc:                                   # noqa: BLE001
        log.warning("explorer not written: %s", exc, exc_info=True)
        write_log(f"WARNING: explorer.html could not be written ({exc}). "
                  "Every other output of this run is unaffected.")
        return None
