"""
The four Development figures, as individual PNGs.

Exactly four, by decision — mean stage index, stage composition, body-size
distribution, quality control. There is deliberately NO survival curve and no
survival % on any axis: see the survival.py docstring for why that readout was
demoted. If you are about to add a fifth figure, add it to the explorer first
and see whether anyone opens it.

Every figure is a bonus output. A failure here is logged and swallowed — the
workbook is the primary artefact and must always complete.

Palette
-------
Strains carry fixed colours (wild type blue, 604 green, 601 orange) so the same
strain is the same colour in every figure, every run, and in the explorer.
Unknown strains fall through to a fixed cycle. "Wild type" is whatever
assay_common.is_wildtype accepts — N2, WT, wildtype — so the colour follows the
strain rather than one spelling of its name. It is a colour and a position in a
legend, nothing more: the wild type is not treated as anyone's control, and
% of control is always a condition against its own strain's lowest dose. Stages use a single-hue ordinal ramp
assigned over the classes actually emitted, so "later stage = darker" reads
without consulting a legend.
"""
from __future__ import annotations

import logging
import textwrap
from pathlib import Path
from typing import Callable, Optional

import assay_common as AC

log = logging.getLogger(__name__)

# Strain colours — fixed by name first, then a cycle for anything unknown.
# A recognised wild type gets _WT_COLOR wherever it appears and whatever it is
# called; only the FIRST one does, because two strains painted the same blue
# would be worse than one of them taking a cycle colour. A run with no wild type
# in it simply has no fixed blue — nothing is promoted into the role.
_WT_COLOR = "#2a78d6"
STRAIN_COLORS = {"604": "#1baf7a", "601": "#eb6834"}
_CYCLE = ["#2a78d6", "#1baf7a", "#eb6834", "#eda100", "#e87ba4",
          "#4a3aa7", "#008300", "#e34948"]
# Single-hue ordinal ramps, indexed by how many stages are actually present.
_RAMP = ["#b7d3f6", "#86b6ef", "#3987e5", "#1c5cab", "#0d366b", "#104281",
         "#08203f"]
_RAMP_PICK = {1: [4], 2: [1, 4], 3: [1, 3, 4], 4: [0, 2, 3, 4],
              5: [0, 1, 2, 3, 4], 6: [0, 1, 2, 3, 4, 5],
              7: [0, 1, 2, 3, 4, 5, 6]}

_GRID = "#e1e0d9"
_AXIS = "#c3c2b7"
_INK = "#0b0b0b"
_MUT = "#898781"


def strain_colors(strains: list[str]) -> dict[str, str]:
    wt = next((s for s in sorted(strains, key=AC.strain_sort_key)
               if AC.is_wildtype(s)), None)
    taken = {STRAIN_COLORS[str(s).lower()] for s in strains
             if str(s).lower() in STRAIN_COLORS}
    if wt is not None:
        taken.add(_WT_COLOR)
    spare = [c for c in _CYCLE if c not in taken]
    out: dict[str, str] = {}
    i = 0
    for s in strains:
        fixed = _WT_COLOR if s == wt else STRAIN_COLORS.get(str(s).lower())
        if fixed:
            out[s] = fixed
        else:
            out[s] = spare[i % len(spare)] if spare else _CYCLE[i % len(_CYCLE)]
            i += 1
    return out


def stage_colors(stages: list[str]) -> dict[str, str]:
    pick = _RAMP_PICK.get(len(stages), list(range(len(_RAMP))))
    return {s: _RAMP[pick[i % len(pick)]] for i, s in enumerate(stages)}


def _sorted_strains(rows: list[dict]) -> list[str]:
    """A recognised wild type first, then the rest alphabetically. Display
    order only — it does not make that strain the control."""
    return sorted({str(r["strain"]) for r in rows}, key=AC.strain_sort_key)


def _sorted_doses(rows: list[dict]) -> list:
    with_dose = sorted({r["dose"] for r in rows if r["dose"] is not None})
    has_none = any(r["dose"] is None for r in rows)
    return with_dose + ([None] if has_none else [])


def _dose_label(dose, unit: str) -> str:
    return "(no dose)" if dose is None else f"{dose} {unit}".strip()


def _tp_label(t: float) -> str:
    return f"{t:g} h"


def _unit_of(rows: list[dict]) -> str:
    for r in rows:
        if r.get("unit"):
            return str(r["unit"])
    return ""


def _find(rows: list[dict], strain, dose, tp) -> Optional[dict]:
    for r in rows:
        if (str(r["strain"]) == str(strain) and r["dose"] == dose
                and r["timepoint_h"] == tp):
            return r
    return None


def _caption(fig, text: str, width: int = 128) -> float:
    """Wrap and place the footnote; return the bottom margin it needs.

    matplotlib's wrap=True does not survive tight_layout, so the text is
    pre-wrapped and the rect is sized from the line count. Otherwise the last
    line is clipped off the bottom — which is how a figure ends up shipping
    without the sentence that says what its error bars mean.
    """
    wrapped = textwrap.fill(text, width)
    n = wrapped.count("\n") + 1
    fig.text(0.5, 0.008, wrapped, ha="center", va="bottom", fontsize=7.5,
             color=_MUT, linespacing=1.35)
    return 0.028 * n + 0.02


def _style_axes(ax) -> None:
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color(_AXIS)
    ax.spines["bottom"].set_color(_AXIS)
    ax.tick_params(colors=_MUT, labelsize=8)
    ax.grid(True, axis="y", color=_GRID, linewidth=0.8, alpha=0.9)
    ax.set_axisbelow(True)


def _replicate_note(rows: list[dict]) -> str:
    units = sorted({r["replicate_unit"] for r in rows})
    if units == ["plate"]:
        return "error bars = ±1 SD across plates"
    if units == ["quadrant image"]:
        return ("error bars = ±1 SD across the four quadrant images "
                "(one plate per condition)")
    return ("error bars = ±1 SD across plates, or across quadrant images where "
            "a condition has a single plate — see 'replicate_unit' in the "
            "workbook for which is which")


# ---------------------------------------------------------------------------
# 1 — mean stage index
# ---------------------------------------------------------------------------

def fig_stage_index(out_png: Path, agg: dict, write_log) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    cond = agg["per_condition"]
    plates = agg["per_plate"]
    images = agg["per_image"]
    strains = _sorted_strains(cond)
    doses = _sorted_doses(cond)
    tps = agg["timepoints"]
    unit = _unit_of(cond)
    colors = strain_colors(strains)

    fig, axes = plt.subplots(1, len(tps), figsize=(3.6 * len(tps) + 1.4, 4.0),
                             squeeze=False, sharey=True)
    numeric_x = all(d is not None for d in doses) and len(doses) > 1
    for ci, tp in enumerate(tps):
        ax = axes[0][ci]
        _style_axes(ax)
        for s in strains:
            xs, ys, es = [], [], []
            for di, d in enumerate(doses):
                r = _find(cond, s, d, tp)
                if r is None or r["stage_index_mean"] != r["stage_index_mean"]:
                    continue
                x = d if numeric_x else di
                xs.append(x)
                ys.append(r["stage_index_mean"])
                sd = r["stage_index_sd"]
                es.append(0.0 if sd != sd else sd)
                # individual replicates, faint
                src = plates if r["replicate_unit"] == "plate" else images
                pts = [q["stage_index"] for q in src
                       if str(q["strain"]) == str(s) and q["dose"] == d
                       and q["timepoint_h"] == tp
                       and q["stage_index"] == q["stage_index"]]
                if pts:
                    jit = (np.arange(len(pts)) - (len(pts) - 1) / 2) * (
                        (max(doses) - min(doses) if numeric_x and len(doses) > 1
                         else 1) * 0.012 if numeric_x else 0.035)
                    ax.scatter(np.full(len(pts), x) + jit, pts, s=14,
                               color=colors[s], alpha=0.35, zorder=1,
                               linewidths=0)
            if xs:
                ax.errorbar(xs, ys, yerr=es, marker="o", capsize=3,
                            linewidth=1.8, markersize=5, color=colors[s],
                            label=str(s), zorder=3)
        ax.set_title(_tp_label(tp), fontsize=11, color=_INK)
        ax.set_ylim(0.8, 5.3)
        ax.set_yticks([1, 2, 3, 4, 5])
        if ci == 0:
            ax.set_yticklabels(["1\nL1", "2\nL2", "3\nL3", "4\nL4", "5\nadult"])
            ax.set_ylabel("Mean stage index", fontsize=10, color=_INK)
        if numeric_x:
            ax.set_xlabel(f"Dose ({unit})" if unit else "Dose", fontsize=9,
                          color=_MUT)
        else:
            ax.set_xticks(range(len(doses)))
            ax.set_xticklabels([_dose_label(d, unit) for d in doses],
                               rotation=30, ha="right")
    axes[0][-1].legend(title="Strain", fontsize=8, title_fontsize=8,
                       frameon=False, loc="best")
    fig.suptitle("Mean stage index", fontsize=13, y=0.99, color=_INK)
    bottom = _caption(fig,
                      "L1=1 … adult=5, eggs excluded. Plate means first, then "
                      "averaged; " + _replicate_note(cond)
                      + "; faint dots are the replicates.")
    fig.tight_layout(rect=(0, bottom, 1, 0.95))
    fig.savefig(out_png, dpi=200)
    plt.close(fig)
    write_log(f"Wrote {out_png}")


# ---------------------------------------------------------------------------
# 2 — stage composition
# ---------------------------------------------------------------------------

def fig_composition(out_png: Path, agg: dict, write_log) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    cond = agg["per_condition"]
    stage_cols = agg["stage_cols"]
    strains = _sorted_strains(cond)
    doses = _sorted_doses(cond)
    tps = agg["timepoints"]
    unit = _unit_of(cond)
    scolors = stage_colors(stage_cols)

    fig, axes = plt.subplots(len(doses), len(tps),
                             figsize=(2.5 * len(tps) + 1.8,
                                      1.85 * len(doses) + 1.5),
                             squeeze=False, sharey=True)
    for ri, d in enumerate(doses):
        for ci, tp in enumerate(tps):
            ax = axes[ri][ci]
            _style_axes(ax)
            ax.grid(False)
            for si, s in enumerate(strains):
                r = _find(cond, s, d, tp)
                if r is None or not r["pooled_total"]:
                    continue
                bottom = 0.0
                for st in stage_cols:
                    pct = r[f"pct_{st}"]
                    if pct != pct or pct <= 0:
                        continue
                    ax.bar(si, pct, bottom=bottom, width=0.62,
                           color=scolors[st], edgecolor="white", linewidth=0.8)
                    if pct >= 11:
                        ax.text(si, bottom + pct / 2, f"{round(pct)}",
                                ha="center", va="center", fontsize=7,
                                color="white" if st in stage_cols[len(stage_cols) // 2:]
                                else _INK)
                    bottom += pct
                ax.text(si, 102, f"n={r['pooled_total']}", ha="center",
                        fontsize=6.5, color=_MUT)
            ax.set_ylim(0, 112)
            ax.set_yticks([0, 50, 100])
            ax.set_xticks(range(len(strains)))
            ax.set_xticklabels(strains if ri == len(doses) - 1 else [],
                               fontsize=8)
            if ri == 0:
                ax.set_title(_tp_label(tp), fontsize=10, color=_INK)
            if ci == 0:
                ax.set_ylabel(_dose_label(d, unit), fontsize=9, color=_INK)
    handles = [plt.Rectangle((0, 0), 1, 1, color=scolors[s]) for s in stage_cols]
    fig.legend(handles, stage_cols, loc="lower center", ncol=min(7, len(stage_cols)),
               frameon=False, fontsize=8, bbox_to_anchor=(0.5, 0.035))
    fig.suptitle("Stage composition", fontsize=13, color=_INK)
    bottom = _caption(fig,
                      "100% stacked; numbers inside segments are % of animals "
                      "detected, n above each bar is how many that was.")
    fig.tight_layout(rect=(0, bottom + 0.055, 1, 0.955))
    fig.savefig(out_png, dpi=200)
    plt.close(fig)
    write_log(f"Wrote {out_png}")


# ---------------------------------------------------------------------------
# 3 — body-size distribution
# ---------------------------------------------------------------------------

_TICK_MANTISSAS = (1, 1.5, 2, 3, 4, 6, 8)


def _log_ticks(lo: float, hi: float) -> list:
    """A 1-1.5-2-3-4-6-8-per-decade ladder covering [lo, hi].

    Replaces a hardcoded pixel-range tick list so the same axis works whether
    the run reports pixels (~20-200) or micrometres (~100-1500). The mantissas
    are chosen to reproduce roughly the old hand-picked pixel ticks
    (20/30/40/60/80/…) rather than leaving a bare decade gap above 100. An
    empty result leaves matplotlib's own ticker in charge.
    """
    import math
    if not (hi > lo > 0):
        return []
    out = []
    dec = math.floor(math.log10(lo))
    while 10 ** dec <= hi:
        for m in _TICK_MANTISSAS:
            t = m * 10 ** dec
            if lo <= t <= hi:
                out.append(int(t) if float(t).is_integer() else t)
        dec += 1
    return out


def fig_body_size(out_png: Path, agg: dict, size: dict, write_log) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    size_unit = size.get("unit_label", "px")
    cond = agg["per_condition"]
    strains = _sorted_strains(cond)
    doses = _sorted_doses(cond)
    tps = agg["timepoints"]
    unit = _unit_of(cond)
    colors = strain_colors(strains)
    x = np.array(size["x"], dtype=float)

    fig, axes = plt.subplots(len(doses), len(tps),
                             figsize=(2.7 * len(tps) + 1.8,
                                      1.7 * len(doses) + 1.6),
                             squeeze=False, sharex=True, sharey=True)
    drawn = 0
    for ri, d in enumerate(doses):
        for ci, tp in enumerate(tps):
            ax = axes[ri][ci]
            _style_axes(ax)
            ax.grid(True, axis="x", color=_GRID, linewidth=0.8)
            ax.grid(False, axis="y")
            ax.set_yticks([])
            for s in strains:
                r = _find(cond, s, d, tp)
                if r is None:
                    continue
                g = size["groups"].get(f"{r['condition']} @ {tp:g}h")
                if not g or g["y"] is None:
                    continue
                y = np.array(g["y"], dtype=float)
                ax.fill_between(x, 0, y, color=colors[s], alpha=0.13,
                                linewidth=0)
                ax.plot(x, y, color=colors[s], linewidth=1.6,
                        solid_joinstyle="round")
                ax.plot([g["p50"]], [-0.07], marker="^", markersize=5,
                        color=colors[s], clip_on=False)
                drawn += 1
            ax.set_xscale("log")
            # Plain integers, not 2x10^1: scientific notation on a range this
            # narrow reads as a mistake. Ticks are derived from the data range
            # rather than hardcoded, because that range is ~20-200 in pixels
            # and ~100-1500 in micrometres.
            ticks = _log_ticks(float(x[0]), float(x[-1]))
            if ticks:
                ax.set_xticks(ticks)
                ax.set_xticklabels([f"{t:g}" for t in ticks])
            ax.set_xticks([], minor=True)
            ax.set_ylim(-0.12, 1.12)
            if ri == 0:
                ax.set_title(_tp_label(tp), fontsize=10, color=_INK)
            if ci == 0:
                ax.set_ylabel(_dose_label(d, unit), fontsize=9, color=_INK)
            if ri == len(doses) - 1:
                ax.set_xlabel(f"apparent size √(w·h)  [{size_unit}]",
                              fontsize=8, color=_MUT)
    handles = [plt.Line2D([], [], color=colors[s], linewidth=2) for s in strains]
    fig.suptitle("Body-size distribution", fontsize=13, color=_INK)
    # Caption first, legend above it: the caption is three lines here, and a
    # legend pinned to a fixed offset lands on top of it.
    bottom = _caption(fig,
                      "Kernel density in log space, curves scaled to equal "
                      f"height; ▲ marks the median. {size['n_total']:,} "
                      "animals; a group needs at least 8 to draw a curve. "
                      "Apparent size is √(w·h) of the detection box, not a body "
                      "length: a coiled animal reads smaller than a straight "
                      "one of the same length.")
    fig.legend(handles, strains, loc="lower center", ncol=min(6, len(strains)),
               frameon=False, fontsize=8, bbox_to_anchor=(0.5, bottom + 0.005))
    fig.tight_layout(rect=(0, bottom + 0.075, 1, 0.955))
    fig.savefig(out_png, dpi=200)
    plt.close(fig)
    write_log(f"Wrote {out_png} ({drawn} curve(s))")


# ---------------------------------------------------------------------------
# 4 — quality control
# ---------------------------------------------------------------------------

def fig_quality_control(out_png: Path, agg: dict, write_log) -> None:
    """Animals per plate (absolute and vs the condition's own control),
    quadrant-to-quadrant spread, and — only when there are any — errored and
    ungrouped images. Panels that would be all zeros are not drawn: an empty
    panel reads as a measurement, and it is not one."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    qc = agg["qc"]
    strains = _sorted_strains(qc)
    doses = _sorted_doses(qc)
    tps = agg["timepoints"]
    unit = _unit_of(qc)
    colors = strain_colors(strains)

    n_err = sum(int(r["n_image_errors"]) for r in qc)
    n_unparsed = agg["n_unparsed"]
    n_gaps = len(agg["gaps"])
    extra = bool(n_err or n_unparsed or n_gaps)
    panels = 3 + (1 if extra else 0)

    fig, axes = plt.subplots(1, panels, figsize=(3.7 * panels + 0.6, 4.3),
                             squeeze=False)
    ax_n, ax_pct, ax_cv = axes[0][0], axes[0][1], axes[0][2]

    def _xpos(d, tp):
        di = doses.index(d)
        ti = tps.index(tp)
        return di * len(tps) + ti

    xticks, xlabels = [], []
    for d in doses:
        for tp in tps:
            xticks.append(_xpos(d, tp))
            xlabels.append(_dose_label(d, unit)
                           + (f"\n{_tp_label(tp)}" if len(tps) > 1 else ""))

    width = 0.8 / max(len(strains), 1)
    for ax, key, title, ylab in (
        (ax_n, "animals_per_plate_mean", "Animals detected per plate",
         "animals / plate"),
        (ax_pct, "pct_of_control", "…as % of this strain's own lowest dose",
         "% of control"),
        (ax_cv, "quadrant_cv_pct_mean", "Quadrant-to-quadrant spread",
         "SD across quadrants (% of plate mean)"),
    ):
        _style_axes(ax)
        for si, s in enumerate(strains):
            xs, ys, es = [], [], []
            for d in doses:
                for tp in tps:
                    r = next((q for q in qc if str(q["strain"]) == str(s)
                              and q["dose"] == d and q["timepoint_h"] == tp),
                             None)
                    if r is None or r[key] != r[key]:
                        continue
                    xs.append(_xpos(d, tp) + (si - (len(strains) - 1) / 2) * width)
                    ys.append(r[key])
                    if key == "animals_per_plate_mean":
                        sd = r["animals_per_plate_sd"]
                        es.append(0.0 if sd != sd else sd)
                    elif key == "quadrant_cv_pct_mean":
                        mx = r["quadrant_cv_pct_max"]
                        es.append(0.0 if mx != mx else max(0.0, mx - r[key]))
                    else:
                        es.append(0.0)
            if xs:
                ax.bar(xs, ys, width=width * 0.9, color=colors[s],
                       label=str(s) if ax is ax_n else None)
                if any(es):
                    ax.errorbar(xs, ys, yerr=es, fmt="none", ecolor=_MUT,
                                elinewidth=0.9, capsize=2)
        ax.set_title(title, fontsize=10, color=_INK)
        ax.set_ylabel(ylab, fontsize=8.5, color=_MUT)
        ax.set_xticks(xticks)
        ax.set_xticklabels(xlabels, fontsize=7, rotation=30, ha="right")
    ax_pct.axhline(100, color="#d03b3b", linewidth=1.1, linestyle="--")
    ax_pct.text(0.995, 100, " control", color="#d03b3b", fontsize=7,
                va="bottom", ha="right", transform=ax_pct.get_yaxis_transform())
    ax_n.legend(title="Strain", fontsize=8, title_fontsize=8, frameon=False)

    if extra:
        ax = axes[0][3]
        _style_axes(ax)
        ax.grid(False)
        labels, values = [], []
        if n_err:
            labels.append("images that\nerrored")
            values.append(n_err)
        if n_unparsed:
            labels.append("images with no\ndose+plate token")
            values.append(n_unparsed)
        if n_gaps:
            labels.append("condition ×\ntimepoint gaps")
            values.append(n_gaps)
        ax.bar(range(len(values)), values, width=0.5, color="#d03b3b")
        for i, v in enumerate(values):
            ax.text(i, v, f" {v}", ha="center", va="bottom", fontsize=9,
                    color=_INK)
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, fontsize=7.5)
        ax.set_ylim(0, max(values) * 1.25)
        ax.set_title("Things that did not go cleanly", fontsize=10,
                     color=_INK)
        ax.set_ylabel("count", fontsize=8.5, color=_MUT)

    fig.suptitle("Quality control", fontsize=13, color=_INK)
    note = ("Bars are the mean across plates; whiskers are ±1 SD (left) and the "
            "worst plate (right). % of control compares each condition with the "
            "LOWEST dose of the same strain at the same timepoint.")
    if not extra:
        note += (" No image errors, no ungrouped images and no missing "
                 "condition × timepoint cells in this run.")
    bottom = _caption(fig, note, 150)
    fig.tight_layout(rect=(0, bottom, 1, 0.93))
    fig.savefig(out_png, dpi=200)
    plt.close(fig)
    write_log(f"Wrote {out_png}")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def write_figures(out_dir: Path, agg: dict, size: Optional[dict],
                  write_log: Callable[[str], None]) -> list[Path]:
    """Write all four PNGs. Each is independently guarded — one failing figure
    never costs the others, and never costs the workbook."""
    jobs = [
        ("stage_index.png", lambda p: fig_stage_index(p, agg, write_log)),
        ("stage_composition.png", lambda p: fig_composition(p, agg, write_log)),
        ("quality_control.png",
         lambda p: fig_quality_control(p, agg, write_log)),
    ]
    if size:
        jobs.insert(2, ("body_size.png",
                        lambda p: fig_body_size(p, agg, size, write_log)))
    else:
        write_log("body_size.png: no body-size data — figure skipped.")

    written: list[Path] = []
    for name, fn in jobs:
        path = out_dir / name
        try:
            fn(path)
            written.append(path)
        except Exception as exc:
            write_log(f"{name}: figure failed ({exc}); the workbook is "
                      "unaffected.")
            log.warning("figure %s failed", name, exc_info=True)
    return written
