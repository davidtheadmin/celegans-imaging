"""Shared figures for motility, crawling and colony survival.

Two per assay, matching the Development set in style and in restraint, plus
one that only the assays with a real untreated control ask for:

  <assay>_dose_response.png   metric (rows) x strain (columns), dose on x,
                              y shared across a row so columns compare
  <assay>_distribution.png    the per-item quantity behind the headline metric,
                              as a density per condition
  <assay>_survival.png        one metric, every strain in one axis, each
                              normalised to its own untreated control
  <assay>_timecourse.png      metric (rows) x strain (columns), TIME on x —
                              self-skipping below two timepoints
  <assay>_normalised.png      one metric, every treated condition in one axis,
                              as a percentage of its own control AT THE SAME
                              TIMEPOINT, with the 50% crossing marked

Every one is a bonus output — a failure is logged and swallowed, because the
workbook is the primary artefact and must always complete.

NOTHING HERE POOLS ACROSS TIMEPOINTS. Every function that indexes a condition
row keys it on (timepoint, condition), never on the condition name: in a
timecourse the name repeats once per imaging day, and an index that ignores
that keeps whichever day came last and draws it as if it were the experiment.
It is a silent wrong answer rather than a crash, so the rule is a rule.

The faceted layout is deliberate and is explained in assay_explorer's docstring:
overlaying six strains needs six categorical colours in one axis, which is not
reliably readable at small-multiple size. Facets need none. The single ordered
ramp here is for dose, where the ordering carries the meaning.
"""
from __future__ import annotations

import logging
import textwrap
from pathlib import Path
from typing import Callable, Optional, Sequence

import assay_common as AC

log = logging.getLogger(__name__)

_GRID = "#e1e0d9"
_AXIS = "#c3c2b7"
_INK = "#0b0b0b"
_MUT = "#898781"
_MARK = "#2a78d6"
_RAMP = ["#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#104281", "#0d366b",
         "#08203f"]


def _ramp(i: int, n: int) -> str:
    if n <= 1:
        return _RAMP[2]
    return _RAMP[min(len(_RAMP) - 1, round(i * (len(_RAMP) - 1) / (n - 1)))]


def _style(ax) -> None:
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color(_AXIS)
    ax.spines["bottom"].set_color(_AXIS)
    ax.tick_params(colors=_MUT, labelsize=8)
    for lab in ax.get_xticklabels() + ax.get_yticklabels():
        lab.set_color(_MUT)


def _caption(fig, text: str, width: int = 128) -> float:
    wrapped = textwrap.fill(text, width)
    n = wrapped.count("\n") + 1
    fig.text(0.5, 0.008, wrapped, ha="center", va="bottom", fontsize=7.5,
             color=_MUT, linespacing=1.35)
    return 0.028 * n + 0.02


def _log_ticks(lo: float, hi: float) -> list:
    import math
    if not (hi > lo > 0):
        return []
    out, dec = [], math.floor(math.log10(lo))
    while 10 ** dec <= hi:
        for m in (1, 1.5, 2, 3, 4, 6, 8):
            t = m * 10 ** dec
            if lo <= t <= hi:
                out.append(int(t) if float(t).is_integer() else t)
        dec += 1
    return out


def fig_dose_response(out_png: Path, agg: AC.Aggregation,
                      metrics: Sequence[AC.Metric], title: str,
                      write_log: Callable[[str], None]) -> Optional[Path]:
    """Grid of metric x strain. Condition mean ± SD across plates, plus a dot
    per plate — a condition whose plates disagree has to look different from one
    whose plates agree.

    IN A TIMECOURSE THIS DRAWS ONE LINE PER TIMEPOINT. It used to key the
    condition rows by dose alone; with a timepoint dimension that dict keeps
    whichever day happened to come last and draws it as though it were the
    condition, with every day's plates scattered behind it. That is not a
    cluttered figure, it is a wrong one, and it read as plausible — which is
    why the rule now is that nothing in this module indexes a condition row by
    anything less than its full key.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np

        metrics = AC.headline(metrics)
        cond, plates = agg.per_condition, agg.per_plate
        if not cond or not metrics:
            write_log("dose-response figure: nothing to draw — skipped.")
            return None
        has_dose = any(c.get("dose") is not None for c in cond)
        strains = sorted({c["strain"] for c in cond}, key=AC.strain_sort_key)
        xs = (sorted({c["dose"] for c in cond if c.get("dose") is not None})
              if has_dose else [None])
        unit = AC.dose_unit_of(cond)
        tps = list(getattr(agg, "timepoints", []) or [])
        series = tps if len(tps) > 1 else [None]

        def _plates_of(c, t):
            return [p for p in plates
                    if p["condition"] == c["condition"]
                    and (t is None or p.get("timepoint_h") == t)]

        cols = strains if has_dose else [None]
        fig, axes = plt.subplots(len(metrics), len(cols),
                                 figsize=(2.35 * len(cols) + 1.5,
                                          1.65 * len(metrics) + 1.5),
                                 squeeze=False, sharey="row")
        for ri, m in enumerate(metrics):
            for ci, s in enumerate(cols):
                ax = axes[ri][ci]
                _style(ax)
                ax.grid(True, axis="y", color=_GRID, linewidth=0.8)
                ax.set_axisbelow(True)
                if has_dose:
                    rows = [c for c in cond if c["strain"] == s]
                    order = xs
                else:
                    rows = list(cond)
                    order = [c["condition"] for c in cond]
                for ti, t in enumerate(series):
                    here = [c for c in rows
                            if t is None or c.get("timepoint_h") == t]
                    keyed = {(c["dose"] if has_dose else c["condition"]): c
                             for c in here}
                    col = _MARK if t is None else _ramp(ti, len(series))
                    mus, errs, X = [], [], []
                    for xi, x in enumerate(order):
                        c = keyed.get(x)
                        if c is None:
                            continue
                        for p in _plates_of(c, t):
                            y = p.get(m.key)
                            if y is None:
                                continue
                            # Jitter is in CATEGORY units, so it has to stay
                            # small: with two doses the axis spans 0..1 and
                            # what looks like a modest nudge throws a plate
                            # dot a tenth of the panel away from its tick.
                            ax.plot([xi + (hash(p["plate"]) % 5 - 2) * 0.012],
                                    [y], marker="o", markersize=2.4,
                                    color=col, alpha=0.28,
                                    linestyle="none", zorder=2)
                        mu = c.get(f"{m.key}_mean")
                        if mu is None:
                            continue
                        X.append(xi)
                        mus.append(mu)
                        errs.append(c.get(f"{m.key}_sd") or 0.0)
                    if X:
                        ax.errorbar(X, mus, yerr=errs, marker="o",
                                    markersize=4.0, linewidth=1.6, capsize=2.5,
                                    color=col, zorder=3,
                                    label=(None if t is None else f"{t:g} h"))
                ax.set_xticks(range(len(order)))
                ax.set_xticklabels(
                    [str(x) for x in order], fontsize=7,
                    rotation=0 if has_dose else 30,
                    ha="center" if has_dose else "right")
                if ri == 0 and s is not None:
                    ax.set_title(str(s), fontsize=10, color=_INK)
                if ci == 0:
                    ax.set_ylabel(m.label + (f"\n({m.unit})" if m.unit else ""),
                                  fontsize=8.5, color=_INK)
                if ri == len(metrics) - 1 and has_dose:
                    ax.set_xlabel(f"Dose ({unit})" if unit else "Dose",
                                  fontsize=8, color=_MUT)
        fig.suptitle(title, fontsize=13, y=0.995, color=_INK)
        cap = ("Marker is the condition mean across plates, bars are ±1 SD "
               "(sample SD, blank at one plate); faint dots are individual "
               "plates. The y-axis is shared across each row, so columns can "
               "be compared directly. n is the number of plates, not the "
               "number of animals.")
        if len(series) > 1:
            cap += (" One line per timepoint, light to dark with time — no "
                    "quantity here is averaged across days.")
        bottom = _caption(fig, cap)
        if len(series) > 1:
            h, l = axes[0][0].get_legend_handles_labels()
            if h:
                # Above the caption, never on top of it — _caption returns the
                # figure fraction it just claimed for exactly this.
                fig.legend(h, l, loc="lower center", ncol=min(len(l), 8),
                           frameon=False, fontsize=8,
                           bbox_to_anchor=(0.5, bottom + 0.004))
                bottom += 0.035
        fig.tight_layout(rect=(0, bottom + 0.02, 1, 0.965))
        fig.savefig(out_png, dpi=200)
        plt.close(fig)
        write_log(f"Wrote {out_png}")
        return out_png
    except Exception as exc:                                   # noqa: BLE001
        log.warning("dose-response figure failed: %s", exc, exc_info=True)
        write_log(f"WARNING: {Path(out_png).name} was not written ({exc}).")
        return None


def fig_distribution(out_png: Path, dist: Optional[dict], label: str,
                     unit: str, doses: Sequence, strains: Sequence,
                     dose_unit: str, title: str,
                     write_log: Callable[[str], None],
                     timepoints: Sequence = ()) -> Optional[Path]:
    """One row per strain, one curve per dose — and one COLUMN per timepoint.

    Pooling five imaging days into one density is not a summary of them: the
    day-0 and day-4 populations of the same condition are different
    populations, and a curve that merges them has a shape neither of them had.
    Splitting the panel is the only honest way to keep the distribution in a
    timecourse, and it is also the view that shows what the condition means
    cannot — whether a falling mean is the whole population slowing or half of
    it stopping.
    """
    try:
        if not dist:
            return None
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np

        x = np.array(dist["x"], dtype=float)
        rows = list(strains) or ["all"]
        series = list(doses) or [None]
        tps = list(timepoints or [])
        cols = tps if len(tps) > 1 else [None]
        fig, axes = plt.subplots(
            len(rows), len(cols), squeeze=False, sharex=True,
            figsize=(2.3 * len(cols) + 2.6 if len(cols) > 1 else 7.4,
                     1.15 * len(rows) + 1.9))
        drawn = 0
        for ri, rk in enumerate(rows):
            for ci, t in enumerate(cols):
                ax = axes[ri][ci]
                _style(ax)
                ax.grid(True, axis="x", color=_GRID, linewidth=0.8)
                ax.grid(False, axis="y")
                ax.set_yticks([])
                for si, s in enumerate(series):
                    g = dist["groups"].get(AC.dist_key(rk, s, t))
                    if not g:
                        continue
                    col = _ramp(si, len(series))
                    if g["y"] is not None:
                        y = np.array(g["y"], dtype=float)
                        ax.fill_between(x, 0, y, color=col, alpha=0.13,
                                        linewidth=0)
                        ax.plot(x, y, color=col, linewidth=1.6)
                        drawn += 1
                    ax.plot([g["p50"]], [-0.07], marker="^", markersize=5,
                            color=col, clip_on=False)
                if dist.get("log"):
                    ax.set_xscale("log")
                    # Narrow columns get decades only. The 1/1.5/2/3/4/6/8 set
                    # is right for one wide axis and unreadable in five, and an
                    # axis whose labels overlap into a grey bar is worse than
                    # one with three labels on it.
                    ticks = (_decade_ticks(float(x[0]), float(x[-1]))
                             if len(cols) > 1
                             else _log_ticks(float(x[0]), float(x[-1])))
                    if ticks:
                        ax.set_xticks(ticks)
                        ax.set_xticklabels([f"{v:g}" for v in ticks],
                                           fontsize=7.5 if len(cols) > 1 else 8)
                    ax.set_xticks([], minor=True)
                ax.set_ylim(-0.12, 1.12)
                if ci == 0:
                    ax.set_ylabel(str(rk), fontsize=9, color=_INK, rotation=0,
                                  ha="right", va="center")
                if ri == 0 and t is not None:
                    ax.set_title(f"{t:g} h", fontsize=9.5, color=_INK)
        xlab = label + (f"  [{unit}]" if unit else "")
        for ci, ax in enumerate(axes[-1]):
            # One x-label per figure, under the middle column: repeating it
            # under all five says nothing the first one did not.
            if len(cols) == 1 or ci == len(cols) // 2:
                ax.set_xlabel(xlab, fontsize=8.5, color=_MUT)
        handles = [plt.Line2D([], [], color=_ramp(i, len(series)), linewidth=2)
                   for i in range(len(series))]
        labels = ([f"{s} {dose_unit}".strip() for s in series] if doses
                  else ["all"])
        fig.suptitle(title, fontsize=13, color=_INK)
        bottom = _caption(
            fig, "Kernel density " + ("in log space" if dist.get("log")
                                      else "in linear space")
                 + f", curves scaled to equal height; ▲ marks the median. "
                   f"{dist['n_total']:,} items. A group needs at least 8 to "
                   "draw a curve; its median is still marked."
                 + (" Columns are timepoints and are never pooled."
                    if len(cols) > 1 else ""))
        fig.legend(handles, labels, loc="lower center", ncol=min(7, len(series)),
                   frameon=False, fontsize=8, bbox_to_anchor=(0.5, bottom + 0.005))
        fig.tight_layout(rect=(0, bottom + 0.06, 1, 0.955))
        fig.savefig(out_png, dpi=200)
        plt.close(fig)
        write_log(f"Wrote {out_png} ({drawn} curve(s))")
        return out_png
    except Exception as exc:                                   # noqa: BLE001
        log.warning("distribution figure failed: %s", exc, exc_info=True)
        write_log(f"WARNING: {Path(out_png).name} was not written ({exc}).")
        return None


# Categorical hues, in fixed order, for the one figure that puts several
# strains in a single axis. Validated as a set on a light surface: worst
# all-pairs colour-vision-deficiency separation ΔE 9.2, worst normal-vision
# ΔE 24.0. They are never cycled — past the eighth strain the figure stops
# drawing rather than repeat a colour, because two strains sharing a colour is
# a wrong figure, not a crowded one.
_CAT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
        "#e87ba4", "#008300", "#4a3aa7", "#e34948"]


def _decade_ticks(lo: float, hi: float) -> list:
    """Powers of ten inside [lo, hi]. A survival axis is read in decades — 100,
    10, 1 — and intermediate labels only compete with the curves."""
    import math
    if not (hi > lo > 0):
        return []
    out, dec = [], math.floor(math.log10(lo))
    while 10.0 ** dec <= hi:
        if 10.0 ** dec >= lo:
            out.append(10.0 ** dec)
        dec += 1
    return out


def _survival_curves(agg: AC.Aggregation, metric: AC.Metric,
                     write_log: Callable[[str], None]):
    """AC.survival_series, capped at the palette and given its colours."""
    built = AC.survival_series(agg, metric.key, write_log)
    if built is None:
        return None
    series = built["series"]
    if len(series) > len(_CAT):
        write_log(f"survival figure: {len(series)} strains present, only the "
                  f"first {len(_CAT)} are drawn — beyond that the colours stop "
                  "being distinguishable.")
        series = series[:len(_CAT)]
    for i, s in enumerate(series):
        s["color"] = _CAT[i]
    return series, built["notes"], built["unit"]


def _draw_survival(ax, series, *, logscale: bool, floor: float, top: float,
                   span: float, label: bool):
    """One survival panel. Returns (n_zero_plates, n_clipped_bars).

    The two panels differ in exactly one thing that matters: zero. A linear
    axis can draw it, so a wiped-out condition is a point on the floor like any
    other. A log axis cannot, so those plates become open triangles on the axis
    floor with the curve dropping to them dotted — the floor is a margin, not a
    value, and it is labelled as such in the caption.
    """
    import matplotlib.pyplot as plt
    from matplotlib.ticker import LogLocator, NullFormatter

    _style(ax)
    if logscale:
        ax.set_yscale("log")
        ax.yaxis.set_minor_locator(
            LogLocator(base=10.0, subs=tuple(range(2, 10)), numticks=100))
        ax.yaxis.set_minor_formatter(NullFormatter())
        ax.grid(True, axis="y", which="major", color=_GRID, linewidth=0.8)
        ax.grid(True, axis="y", which="minor", color=_GRID, linewidth=0.5,
                alpha=0.45)
        ax.tick_params(axis="y", which="minor", length=2.5, color=_AXIS)
        ax.tick_params(axis="y", which="major", length=4.5, color=_AXIS)
    else:
        ax.grid(True, axis="y", color=_GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.axhline(100, color=_AXIS, linewidth=0.9, linestyle=(0, (4, 3)), zorder=1)

    n_zero = n_clip = 0
    base_y = floor if logscale else 0.0
    for s in series:
        for p in s["pts"]:
            n = len(p["vals"])
            for j, v in enumerate(p["vals"]):
                dx = (j - (n - 1) / 2) * 0.014 * span
                if v > 0:
                    ax.plot([p["dose"] + dx], [v], marker="o", markersize=3.4,
                            color=s["color"], alpha=0.5, linestyle="none",
                            markeredgecolor="white", markeredgewidth=0.5,
                            zorder=2)
                elif logscale:
                    n_zero += 1
                    ax.plot([p["dose"] + dx], [floor], marker="v",
                            markersize=3.6, markerfacecolor="none",
                            markeredgecolor=s["color"], markeredgewidth=0.9,
                            alpha=0.5, linestyle="none", zorder=2)
                else:
                    n_zero += 1
                    ax.plot([p["dose"] + dx], [0.0], marker="o", markersize=3.4,
                            color=s["color"], alpha=0.5, linestyle="none",
                            markeredgecolor="white", markeredgewidth=0.5,
                            zorder=2)

        X, Y, elo, ehi = [], [], [], []
        for p in s["pts"]:
            mu = p["mean"]
            if mu is None or (logscale and mu <= 0):
                continue
            X.append(p["dose"])
            Y.append(mu)
            ehi.append(p["sd"])
            room = max(mu - base_y * (1.02 if logscale else 1.0), 0.0)
            if p["sd"] > room:
                n_clip += 1
            elo.append(min(p["sd"], room))
        if not X:
            continue
        ax.errorbar(X, Y, yerr=[elo, ehi], marker="o", markersize=5.4,
                    linewidth=1.9, capsize=3, color=s["color"],
                    markeredgecolor="white", markeredgewidth=0.7, zorder=3)

        lx, ly = X[-1], Y[-1]
        if logscale:
            zeros_at = sorted(p["dose"] for p in s["pts"]
                              if p["mean"] is not None and p["mean"] <= 0)
            if zeros_at:
                ax.plot([X[-1], zeros_at[0]], [Y[-1], floor], color=s["color"],
                        linewidth=1.5, linestyle=(0, (2, 2)), zorder=3)
                if len(zeros_at) > 1:
                    ax.plot(zeros_at, [floor] * len(zeros_at), color=s["color"],
                            linewidth=1.5, linestyle=(0, (2, 2)), zorder=3)
                for zd in zeros_at:
                    ax.plot([zd], [floor], marker="v", markersize=7.5,
                            markerfacecolor="white", markeredgecolor=s["color"],
                            markeredgewidth=1.7, linestyle="none", zorder=4)
                lx, ly = zeros_at[-1], floor
        if label:
            ax.annotate(str(s["strain"]), xy=(lx, ly), xytext=(7, 0),
                        textcoords="offset points", va="center", fontsize=8.5,
                        color=_INK)
    return n_zero, n_clip


def fig_survival(out_png: Path, agg: AC.Aggregation, metric: AC.Metric,
                 title: str, write_log: Callable[[str], None]) -> Optional[Path]:
    """Survival against dose, every strain in one axis, each normalised to its
    own untreated control — drawn twice, linear and logarithmic.

    The only figure in this file that overlays strains, and it earns the
    exception: "which strain falls off faster" is a comparison BETWEEN curves,
    and facets answer that badly. The cost is paid twice over — a
    colour-vision-validated categorical set, capped at eight, and every curve
    labelled at its end, so identity never rests on colour alone.

    TWO PANELS, NOT A CHOICE BETWEEN THEM. Linear is how the number is spoken
    and is the only one of the two that can draw a zero; log is how a
    multiplicative quantity behaves — a fall from 100% to 10% and one from 10%
    to 1% are the same event twice, and only the log panel shows them as the
    same step. Both are the same numbers on the same x-axis, so nothing is
    hidden by preferring one.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        built = _survival_curves(agg, metric, write_log)
        if built is None:
            return None
        series, notes, unit = built

        every = [v for s in series for p in s["pts"] for v in p["vals"]]
        pos = [v for v in every if v > 0]
        import math
        floor = 10.0 ** math.floor(math.log10(min(pos + [100.0])))
        # The two panels want different headroom: the log panel needs room
        # above 100% for the label row, the linear one only looks empty with it.
        top_log = max(max(every + [100.0]) * 1.3, 100.0)
        top_lin = max(105.0, max(every + [100.0]) * 1.08)
        all_d = sorted({p["dose"] for s in series for p in s["pts"]})
        span = (all_d[-1] - all_d[0]) or 1.0

        fig, axes = plt.subplots(1, 2, figsize=(11.6, 4.7))
        n_zero = n_clip = 0
        for ax, is_log in ((axes[0], False), (axes[1], True)):
            z, c = _draw_survival(ax, series, logscale=is_log, floor=floor,
                                  top=top_log if is_log else top_lin,
                                  span=span, label=is_log)
            n_zero, n_clip = max(n_zero, z), max(n_clip, c)
            if is_log:
                ticks = _decade_ticks(floor, top_log)
                if ticks:
                    ax.set_yticks(ticks)
                    ax.set_yticklabels([f"{t:g}%" for t in ticks])
                ax.set_ylim(floor / 1.6, top_log)
                ax.set_title("Log scale", fontsize=9.5, color=_MUT)
            else:
                step = (20.0 if top_lin <= 140 else
                        25.0 if top_lin <= 220 else 50.0)
                ticks = [t for t in
                         [step * i for i in range(int(top_lin // step) + 2)]
                         if t <= top_lin]
                ax.set_yticks(ticks)
                ax.set_yticklabels([f"{t:g}%" for t in ticks])
                ax.set_ylim(0, top_lin)
                ax.set_title("Linear scale", fontsize=9.5, color=_MUT)
                ax.set_ylabel(f"{metric.label}, % of untreated", fontsize=8.5,
                              color=_INK)
            ax.set_xticks(all_d)
            ax.set_xticklabels([f"{d:g}" for d in all_d], fontsize=8)
            ax.set_xlim(all_d[0] - 0.06 * span,
                        all_d[-1] + (0.20 if is_log else 0.08) * span)
            ax.set_xlabel(f"Dose ({unit})" if unit else "Dose", fontsize=8.5,
                          color=_MUT)
        fig.suptitle(title, fontsize=13, y=0.985, color=_INK)

        cap = ("Each plate's " + metric.label.lower() + " as a percentage of "
               "the mean of that strain's untreated plates. Markers are the "
               "condition mean across plates, bars ±1 SD across those "
               "normalised plates, faint dots the plates themselves. The "
               "untreated point is 100% by construction; its bar is the spread "
               "of the controls. Same numbers in both panels.")
        if n_clip:
            cap += (" A bar that reaches the bottom of a panel is one whose "
                    "mean minus SD is at or below zero — those plates disagree "
                    "by more than their own mean.")
        if n_zero:
            cap += (f" {n_zero} plate(s) scored zero: a real point at 0% on the "
                    "left, and on the right an open triangle on the axis floor "
                    "reached by a dotted drop, because zero has no place on a "
                    "log axis. That floor is a margin, not a value.")
        if notes:
            cap += " " + "; ".join(notes) + "."
        bottom = _caption(fig, cap, width=142)
        handles = [plt.Line2D([], [], color=s["color"], linewidth=2.2,
                              marker="o", markersize=5) for s in series]
        fig.legend(handles, [str(s["strain"]) for s in series],
                   loc="lower center", ncol=min(8, len(series)), frameon=False,
                   fontsize=8.5, bbox_to_anchor=(0.5, bottom + 0.005))
        fig.tight_layout(rect=(0, bottom + 0.06, 1, 0.945))
        fig.savefig(out_png, dpi=200)
        plt.close(fig)
        write_log(f"Wrote {out_png} ({len(series)} strain(s), linear and log, "
                  "normalised to each strain's untreated control)")
        return out_png
    except Exception as exc:                                   # noqa: BLE001
        log.warning("survival figure failed: %s", exc, exc_info=True)
        write_log(f"WARNING: {Path(out_png).name} was not written ({exc}).")
        return None


def fig_timecourse(out_png: Path, agg: AC.Aggregation,
                   metrics: Sequence[AC.Metric], title: str,
                   write_log: Callable[[str], None]) -> Optional[Path]:
    """Metric against time, one line per condition — the timecourse headline.

    One row per metric, one column per strain, so the doses of a strain sit
    together and can be read against each other. x is the timepoint in hours,
    y is the condition mean across plates with +/-1 SD, and a faint dot per
    plate sits behind it. A condition missing at one timepoint leaves a GAP in
    its line rather than interpolating across it — an unimaged day is not a
    measurement, and a straight line through it would claim otherwise.

    Returns None, having said why in the log, when the run has fewer than two
    timepoints. That is the ordinary single-folder case and not an error.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np

        metrics = AC.headline(metrics)
        cond, plates = agg.per_condition, agg.per_plate
        tps = list(getattr(agg, "timepoints", []) or [])
        if len(tps) < 2:
            write_log("timecourse figure: fewer than two timepoints — skipped.")
            return None
        if not cond or not metrics:
            write_log("timecourse figure: nothing to draw — skipped.")
            return None

        strains = sorted({c["strain"] for c in cond}, key=AC.strain_sort_key)
        unit = AC.dose_unit_of(cond)
        doses = sorted({c["dose"] for c in cond if c.get("dose") is not None})

        def colour_for(dose):
            if dose is None or not doses:
                return _MARK
            return _ramp(doses.index(dose), len(doses))

        fig, axes = plt.subplots(
            len(metrics), len(strains),
            figsize=(2.9 * len(strains) + 1.6, 1.9 * len(metrics) + 1.6),
            squeeze=False, sharex=True, sharey="row")

        for ri, m in enumerate(metrics):
            for ci, strain in enumerate(strains):
                ax = axes[ri][ci]
                _style(ax)
                ax.grid(True, axis="y", color=_GRID, linewidth=0.8)
                here = [c for c in cond if c["strain"] == strain]
                names = sorted({(c.get("dose") is None, c.get("dose") or 0.0,
                                 c["condition"]) for c in here})
                for _nd, _d, cname in names:
                    dose = next((c.get("dose") for c in here
                                 if c["condition"] == cname), None)
                    col = colour_for(dose)
                    xs, ys, es = [], [], []
                    for tp in tps:
                        row = next((c for c in here
                                    if c["condition"] == cname
                                    and c.get("timepoint_h") == tp), None)
                        if row is None:
                            continue
                        v = row.get(f"{m.key}_mean")
                        if v is None or not np.isfinite(v):
                            continue
                        xs.append(tp)
                        ys.append(v)
                        e = row.get(f"{m.key}_sd")
                        es.append(e if e is not None and np.isfinite(e) else 0.0)
                        pv = [q.get(m.key) for q in plates
                              if q.get("timepoint_h") == tp
                              and q.get("condition") == cname
                              and q.get(m.key) is not None
                              and np.isfinite(q.get(m.key))]
                        if pv:
                            ax.plot([tp] * len(pv), pv, marker="o",
                                    linestyle="none", markersize=2.6,
                                    color=col, alpha=0.3, zorder=1)
                    if xs:
                        ax.errorbar(
                            xs, ys, yerr=es, color=col, marker="o",
                            markersize=4.2, linewidth=1.6, capsize=2.5,
                            elinewidth=0.9, zorder=3,
                            label=(f"{dose:g} {unit}".strip()
                                   if dose is not None else str(cname)))
                if ri == 0:
                    ax.set_title(str(strain), fontsize=10, color=_INK)
                if ci == 0:
                    ax.set_ylabel(f"{m.label}\n({m.unit})" if m.unit else m.label,
                                  fontsize=8.5, color=_INK)
                if ri == len(metrics) - 1:
                    ax.set_xlabel("time (h)", fontsize=8, color=_MUT)
                if m.log:
                    ax.set_yscale("log")
                ax.tick_params(labelsize=7)

        seen, handles, labels = set(), [], []
        for axrow in axes:
            for ax in axrow:
                for h, lb in zip(*ax.get_legend_handles_labels()):
                    if lb not in seen:
                        seen.add(lb)
                        handles.append(h)
                        labels.append(lb)
        if handles:
            fig.legend(handles, labels, loc="lower center",
                       ncol=min(len(labels), 6), frameon=False, fontsize=8)
        fig.suptitle(title, fontsize=13, y=0.995, color=_INK)
        fig.tight_layout(rect=(0, 0.06, 1, 0.97))
        fig.savefig(out_png, dpi=200, bbox_inches="tight")
        plt.close(fig)
        write_log(f"Wrote {out_png} ({len(tps)} timepoints, "
                  f"{len(cond)} condition-timepoint rows)")
        return out_png
    except Exception as exc:                                   # noqa: BLE001
        write_log(f"WARNING: the timecourse figure could not be drawn ({exc}). "
                  "Every other output of this run is unaffected.")
        log.warning("timecourse figure failed", exc_info=True)
        return None


def fig_normalised(out_png: Path, agg: AC.Aggregation, metric: AC.Metric,
                   title: str, write_log: Callable[[str], None]
                   ) -> Optional[Path]:
    """One metric, every treated condition, as a percentage of ITS OWN control
    at the SAME timepoint — the sensitivity curve, and the one panel that puts
    the strains in a single axis.

    Why this is the figure the timecourse grid cannot be. The grid answers
    "what did each condition do", one panel at a time, and leaves the reader to
    hold three panels in their head and subtract. This answers "which strain
    lost more of its locomotion, and when", which is a comparison BETWEEN
    curves and therefore has to be drawn as one. It pays for overlaying with a
    colour-vision-validated categorical set, a label on every curve, and a cap
    at eight.

    The denominator moves with the x-axis. Untreated plates decline too — ours
    are down sharply by 96 h — and a fixed day-0 baseline would charge that to
    the treatment and manufacture a dose-response out of the plates simply
    getting older. Dividing by the same day's control removes it. That is also
    why the control line sits flat at 100% by construction: it is the
    definition, not a result, and the figure says so rather than inviting the
    reading that the control was stable.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np

        built = AC.normalised_series(agg, metric.key, write_log)
        if built is None:
            return None
        series = built["series"]
        capped = max(0, len(series) - len(_CAT))
        series = series[:len(_CAT)]
        unit = built["unit"]
        tps = built["timepoints"]

        fig, ax = plt.subplots(figsize=(7.6, 4.3))
        _style(ax)
        ax.grid(True, axis="y", color=_GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        ax.axhline(100.0, color=_AXIS, linewidth=1.0, linestyle=(0, (5, 4)))
        ax.axhline(50.0, color=_AXIS, linewidth=0.8, linestyle=(0, (2, 4)))

        labs = []
        for i, s in enumerate(series):
            col = _CAT[i % len(_CAT)]
            xs = [q["tp"] for q in s["pts"] if q["mean"] is not None]
            ys = [q["mean"] for q in s["pts"] if q["mean"] is not None]
            es = [(q["sd"] or 0.0) for q in s["pts"] if q["mean"] is not None]
            for q in s["pts"]:
                for val in q["vals"]:
                    ax.plot([q["tp"]], [val], marker="o", markersize=2.4,
                            color=col, alpha=0.30, linestyle="none", zorder=1)
            if xs:
                ax.errorbar(xs, ys, yerr=es, color=col, marker="o",
                            markersize=4.4, linewidth=1.9, capsize=2.5,
                            elinewidth=0.9, zorder=3)
                labs.append((xs[-1], ys[-1],
                             f"{s['strain']} {s['dose']:g} {unit}".strip(), col))
            th = s.get("t_half")
            if th is not None:
                ax.plot([th], [50.0], marker="v", markersize=6.5, color=col,
                        markeredgecolor="white", markeredgewidth=0.9, zorder=4)

        labs.sort(key=lambda q: q[1])
        for j in range(1, len(labs)):
            if labs[j][1] - labs[j - 1][1] < 6:
                labs[j] = (labs[j][0], labs[j - 1][1] + 6, labs[j][2], labs[j][3])
        for lx, ly, txt, col in labs:
            ax.annotate(txt, (lx, ly), xytext=(7, 0),
                        textcoords="offset points", fontsize=9,
                        color=col, fontweight="bold", va="center")

        ax.set_xlabel("time (h)", fontsize=9, color=_MUT)
        ax.set_ylabel(f"{metric.label}, % of same-day control", fontsize=9.5,
                      color=_INK)
        ax.set_xticks(tps)
        ax.set_xlim(min(tps) - 4, max(tps) + max(22.0, 0.28 * (max(tps) or 1)))
        ax.set_ylim(bottom=0)
        fig.suptitle(title, fontsize=13, y=0.985, color=_INK)

        halves = [f"{s['strain']} {s['dose']:g} {unit}".strip()
                  + (f" at {s['t_half']:.0f} h" if s.get("t_half") is not None
                     else ": never")
                  for s in series]
        cap = ("Each treated plate as a percentage of the mean of its own "
               "strain's control plates FROM THE SAME DAY, so the decline of "
               "the untreated plates themselves is divided out rather than "
               "charged to the treatment. Marker is the mean across those "
               "normalised plates, bar is ±1 SD, faint dots are the plates. "
               "The dashed line at 100% is the control by construction. "
               "▼ marks where a curve first crosses 50%, interpolated between "
               "timepoints — " + "; ".join(halves) + ".")
        if capped:
            cap += (f" {capped} further condition(s) are not drawn: eight is "
                    "as far as the colours stay distinguishable.")
        for n in built.get("notes", []):
            cap += " " + n
        bottom = _caption(fig, cap)
        fig.tight_layout(rect=(0, bottom + 0.02, 1, 0.945))
        fig.savefig(out_png, dpi=200)
        plt.close(fig)
        write_log(f"Wrote {out_png} ({len(series)} normalised curve(s); "
                  + "; ".join(halves) + ")")
        return out_png
    except Exception as exc:                                   # noqa: BLE001
        log.warning("normalised figure failed: %s", exc, exc_info=True)
        write_log(f"WARNING: {Path(out_png).name} was not written ({exc}).")
        return None
