"""
The self-contained Development explorer (explorer.html).

Ported from the population-study prototype. Two things about it are load-bearing
and must not be "tidied":

  * The template's palette is hard-coded in JavaScript, not read from CSS custom
    properties at draw time. In a sandboxed frame ``getComputedStyle`` returns
    '' for a custom property, which becomes ``fill: black`` (bars survive,
    looking merely ugly) and ``stroke: none`` (lines vanish entirely, looking
    like missing data). Keep the palette in JS.
  * The whole payload is inlined into the HTML. No network, no sidecar files —
    the file has to survive being emailed to someone.

The panels are exactly the four agreed figures. The prototype's metric
comparison, per-plate table and trajectory panels are gone, and so is anything
that plotted survival %.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable, Optional

import assay_common as AC

log = logging.getLogger(__name__)

_TEMPLATE = Path(__file__).parent / "survival_explorer_template.html"
_PLACEHOLDER = "__DATA__"


def _round(x, n=4):
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if f != f:          # NaN -> null, so JS can test for it once
        return None
    return round(f, n)


def build_payload(agg: dict, size: Optional[dict], meta: dict,
                  plans, summary: dict) -> dict:
    """Turn the aggregate into the JSON the template draws.

    Doses and timepoints become INDEXES in the payload (``di`` / ``ti``). A
    condition with no parseable dose has dose None, and JSON object keys cannot
    be null — indexing sidesteps that instead of inventing a -1 sentinel that
    would then need explaining in three places.
    """
    cond = agg["per_condition"]
    stage_cols = list(agg["stage_cols"])

    strains = sorted({str(r["strain"]) for r in cond},
                     key=AC.strain_sort_key)
    doses_num = sorted({r["dose"] for r in cond if r["dose"] is not None})
    doses = list(doses_num) + ([None] if any(r["dose"] is None for r in cond)
                               else [])
    tps = list(agg["timepoints"])
    unit = next((r["unit"] for r in cond if r.get("unit")), "")

    di_of = {d: i for i, d in enumerate(doses)}
    ti_of = {t: i for i, t in enumerate(tps)}

    cond_out = []
    for r in cond:
        cond_out.append({
            "condition": r["condition"],
            "strain": str(r["strain"]),
            "di": di_of[r["dose"]],
            "ti": ti_of[r["timepoint_h"]],
            "n_plates": r["n_plates"],
            "n_images": r["n_images"],
            "replicate_unit": r["replicate_unit"],
            "n_replicates": r["n_replicates"],
            "si_mean": _round(r["stage_index_mean"], 4),
            "si_sd": _round(r["stage_index_sd"], 4),
            "pooled_total": int(r["pooled_total"]),
            "n": {s: int(r[f"n_{s}"]) for s in stage_cols},
            "pct": {s: _round(r[f"pct_{s}"], 3) for s in stage_cols},
        })

    # Replicate-level points, whichever unit each condition used. Sent flat so
    # the panel can scatter them without re-deciding the replication rule.
    reps = []
    for r in cond:
        src = (agg["per_plate"] if r["replicate_unit"] == "plate"
               else agg["per_image"])
        for q in src:
            if (q["timepoint_h"] != r["timepoint_h"]
                    or q["condition"] != r["condition"]):
                continue
            reps.append({
                "strain": str(r["strain"]),
                "di": di_of[r["dose"]],
                "ti": ti_of[r["timepoint_h"]],
                "stage_index": _round(q["stage_index"], 4),
                "total": int(q["total"]),
            })

    qc_out = []
    for r in agg["qc"]:
        qc_out.append({
            "strain": str(r["strain"]),
            "di": di_of[r["dose"]],
            "ti": ti_of[r["timepoint_h"]],
            "n_plates": r["n_plates"],
            "n_animals_total": int(r["n_animals_total"]),
            "animals_per_plate_mean": _round(r["animals_per_plate_mean"], 2),
            "animals_per_plate_sd": _round(r["animals_per_plate_sd"], 2),
            "animals_per_plate_min": int(r["animals_per_plate_min"]),
            "animals_per_plate_max": int(r["animals_per_plate_max"]),
            "pct_of_control": _round(r["pct_of_control"], 2),
            "quadrant_cv_pct_mean": _round(r["quadrant_cv_pct_mean"], 2),
            "quadrant_cv_pct_max": _round(r["quadrant_cv_pct_max"], 2),
            "n_image_errors": int(r["n_image_errors"]),
        })

    size_out = None
    if size:
        groups = {}
        for k, g in size["groups"].items():
            groups[k] = {"y": g["y"], "n": g["n"], "p25": g["p25"],
                         "p50": g["p50"], "p75": g["p75"]}
        size_out = {"x": size["x"], "groups": groups,
                    "n_total": size["n_total"],
                    # px or µm — the template puts this in the axis note, so a
                    # saved explorer never has to be read against the wrong unit.
                    "unit": size.get("unit_label", "px"),
                    "scale_note": size.get("scale_note", "")}

    rescore = meta.get("rescore") or {}
    alpha = float(rescore.get("alpha") or 0.0)
    units = sorted({r["replicate_unit"] for r in cond})
    if units == ["plate"]:
        rep_note = "replication unit is the plate"
    elif units == ["quadrant image"]:
        rep_note = ("replication unit is the quadrant image — every condition "
                    "here has a single plate")
    else:
        rep_note = ("replication unit is the plate, except for conditions with "
                    "a single plate, where it is the quadrant image")

    return {
        "strains": strains,
        # The strain the wild-type rule recognised, if any — so the template
        # colours it consistently without carrying its own copy of the rule.
        # None is normal: plenty of experiments have no wild type in them.
        "wt": next((s for s in strains if AC.is_wildtype(s)), None),
        "doses": doses,
        "tps": tps,
        "stages": stage_cols,
        "unit": unit,
        "cond": cond_out,
        "reps": reps,
        "qc": qc_out,
        "size": size_out,
        "meta": {
            "n_detections": sum(int(r["pooled_total"]) for r in cond),
            "n_plates": summary.get("n_plates", 0),
            "n_images": summary.get("n_images", 0),
            "n_errors": agg["n_error"],
            "n_unparsed": agg["n_unparsed"],
            "gaps": [{"condition": g["condition"],
                      "timepoint_h": g["timepoint_h"]} for g in agg["gaps"]],
            "replicate_note": rep_note,
            "rescore": (f"ON, alpha {alpha:g} (relabels only — the number of "
                        "animals found is unchanged)"
                        if alpha else "OFF (arg-max on raw model scores)"),
            "folders": [{"name": p.folder.name, "hours": p.hours,
                         "detail": p.detail} for p in plans],
        },
    }


def write_explorer(out_html: Path, agg: dict, size: Optional[dict],
                   meta: dict, plans, summary: dict,
                   write_log: Callable[[str], None]) -> Optional[Path]:
    """Write explorer.html. Bonus output — never fails the run."""
    try:
        payload = build_payload(agg, size, meta, plans, summary)
        tpl = _TEMPLATE.read_text(encoding="utf-8")
        if _PLACEHOLDER not in tpl:
            raise RuntimeError(f"{_TEMPLATE.name} has no {_PLACEHOLDER} slot")
        # separators: no spaces — the payload is the bulk of the file.
        blob = json.dumps(payload, separators=(",", ":"), allow_nan=False)
        out_html.write_text(tpl.replace(_PLACEHOLDER, blob), encoding="utf-8")
        write_log(f"Wrote {out_html} ({out_html.stat().st_size // 1024} KB, "
                  "self-contained — no network)")
        return out_html
    except Exception as exc:
        write_log(f"explorer.html: failed ({exc}); every other output is "
                  "unaffected.")
        log.warning("explorer failed", exc_info=True)
        return None
