"""Shared figures for motility, crawling and colony survival.

Two per assay, matching the Development set in style and in restraint:

  <assay>_dose_response.png   metric (rows) x strain (columns), dose on x,
                              y shared across a row so columns compare
  <assay>_distribution.png    the per-item quantity behind the headline metric,
                              as a density per condition

Both are bonus outputs — a failure is logged and swallowed, because the workbook
is the primary artefact and must always complete.

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
    whose plates agree."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np

        cond, plates = agg.per_condition, agg.per_plate
        if not cond or not metrics:
            write_log("dose-response figure: nothing to draw — skipped.")
            return None
        has_dose = any(c.get("dose") is not None for c in cond)
        strains = sorted({c["strain"] for c in cond})
        xs = (sorted({c["dose"] for c in cond if c.get("dose") is not None})
              if has_dose else [None])
        unit = AC.dose_unit_of(cond)

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
                    keyed = {c["dose"]: c for c in rows}
                else:
                    order = [c["condition"] for c in cond]
                    keyed = {c["condition"]: c for c in cond}
                mus, errs, X = [], [], []
                for xi, x in enumerate(order):
                    c = keyed.get(x)
                    if c is None:
                        continue
                    for p in [p for p in plates
                              if p["condition"] == c["condition"]]:
                        y = p.get(m.key)
                        if y is None:
                            continue
                        ax.plot([xi + (hash(p["plate"]) % 5 - 2) * 0.04], [y],
                                marker="o", markersize=2.6, color=_AXIS,
                                linestyle="none", zorder=2)
                    mu = c.get(f"{m.key}_mean")
                    if mu is None:
                        continue
                    X.append(xi)
                    mus.append(mu)
                    errs.append(c.get(f"{m.key}_sd") or 0.0)
                if X:
                    ax.errorbar(X, mus, yerr=errs, marker="o", markersize=4.4,
                                linewidth=1.7, capsize=3, color=_MARK, zorder=3)
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
        bottom = _caption(
            fig, "Marker is the condition mean across plates, bars are ±1 SD "
                 "(sample SD, blank at one plate); grey dots are individual "
                 "plates. The y-axis is shared across each row, so columns can "
                 "be compared directly. n is the number of plates, not the "
                 "number of animals.")
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
                     write_log: Callable[[str], None]) -> Optional[Path]:
    """One row per strain, one curve per dose."""
    try:
        if not dist:
            return None
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np

        x = np.array(dist["x"], dtype=float)
        rows = list(strains) or ["all"]
        series = list(doses) or ["*"]
        fig, axes = plt.subplots(len(rows), 1, squeeze=False, sharex=True,
                                 figsize=(7.4, 1.15 * len(rows) + 1.9))
        drawn = 0
        for ri, rk in enumerate(rows):
            ax = axes[ri][0]
            _style(ax)
            ax.grid(True, axis="x", color=_GRID, linewidth=0.8)
            ax.grid(False, axis="y")
            ax.set_yticks([])
            for si, s in enumerate(series):
                key = f"{rk}|{s}" if doses else str(rk)
                g = dist["groups"].get(key)
                if not g:
                    continue
                col = _ramp(si, len(series))
                if g["y"] is not None:
                    y = np.array(g["y"], dtype=float)
                    ax.fill_between(x, 0, y, color=col, alpha=0.13, linewidth=0)
                    ax.plot(x, y, color=col, linewidth=1.6)
                    drawn += 1
                ax.plot([g["p50"]], [-0.07], marker="^", markersize=5,
                        color=col, clip_on=False)
            if dist.get("log"):
                ax.set_xscale("log")
                ticks = _log_ticks(float(x[0]), float(x[-1]))
                if ticks:
                    ax.set_xticks(ticks)
                    ax.set_xticklabels([f"{t:g}" for t in ticks])
                ax.set_xticks([], minor=True)
            ax.set_ylim(-0.12, 1.12)
            ax.set_ylabel(str(rk), fontsize=9, color=_INK, rotation=0,
                          ha="right", va="center")
        axes[-1][0].set_xlabel(label + (f"  [{unit}]" if unit else ""),
                               fontsize=8, color=_MUT)
        handles = [plt.Line2D([], [], color=_ramp(i, len(series)), linewidth=2)
                   for i in range(len(series))]
        labels = [f"{s} {dose_unit}".strip() for s in series] if doses else ["all"]
        fig.suptitle(title, fontsize=13, color=_INK)
        bottom = _caption(
            fig, "Kernel density " + ("in log space" if dist.get("log")
                                      else "in linear space")
                 + f", curves scaled to equal height; ▲ marks the median. "
                   f"{dist['n_total']:,} items. A group needs at least 8 to "
                   "draw a curve; its median is still marked.")
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
