"""
Crawling overview figure.

One multi-panel PNG: a grid of subplots, one per metric (engine BPM plus all
crawling kinematics), each a box plot by condition (or a bar chart when a
condition has a single kept worm), annotated with the per-condition n. Only
filter-passing worms (passed_filter) contribute, matching the per_condition
aggregates.
"""
import logging
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from analysis.crawling_metrics import AGG_COLS

log = logging.getLogger(__name__)

# Human-readable panel titles for the metric columns (falls back to the raw
# column name for anything not listed).
_LABELS: dict[str, str] = {
    "bpm": "Head-bend rate (BPM)",
    "bend_interval_cv": "Bend interval CV",
    "mean_speed_pxs": "Mean speed (px/s)",
    "mean_forward_speed_pxs": "Forward speed (px/s)",
    "mean_backward_speed_pxs": "Backward speed (px/s)",
    "fraction_forward": "Fraction forward",
    "fraction_backward": "Fraction backward",
    "fraction_paused": "Fraction paused",
    "reversal_count": "Reversal count",
    "reversal_rate_per_min": "Reversal rate (/min)",
    "path_length_px": "Path length (px)",
    "net_displacement_px": "Net displacement (px)",
    "tortuosity": "Tortuosity",
    "mean_length_px": "Body length (px)",
    "mean_width_midbody_px": "Midbody width (px)",
    "track_duration_s": "Track duration (s)",
    "longest_continuous_run_s": "Longest run (s)",
    "skeleton_coverage": "Skeleton coverage",
}


def make_crawling_overview_png(
    per_worm_rows: list[dict],
    out_path: Path,
    min_span_s: float,
    elapsed_s: float | None = None,
) -> None:
    """
    Render the multi-panel crawling overview to out_path.

    per_worm_rows is the full (unfiltered) per-worm table; this function applies
    the same passed_filter gate used for aggregation so the figure reflects the
    kept worms only. Panels are laid out on a roughly square grid.
    """
    if not per_worm_rows:
        log.warning("crawling overview: no per-worm rows — skipping figure")
        return

    df = pd.DataFrame(per_worm_rows)
    if "passed_filter" in df.columns:
        kept = df[df["passed_filter"].astype(bool)]
    else:
        kept = df
    if kept.empty:
        log.warning("crawling overview: no worms passed the filter — skipping figure")
        return

    conditions = sorted(kept["condition"].astype(str).unique())
    n_per_cond = {c: int((kept["condition"].astype(str) == c).sum()) for c in conditions}

    # Metrics that actually have at least one finite value among kept worms.
    metrics = [
        m for m in AGG_COLS
        if m in kept.columns
        and np.isfinite(pd.to_numeric(kept[m], errors="coerce").values.astype(float)).any()
    ]
    if not metrics:
        log.warning("crawling overview: no finite metrics — skipping figure")
        return

    n = len(metrics)
    ncols = int(np.ceil(np.sqrt(n)))
    nrows = int(np.ceil(n / ncols))

    fig, axes = plt.subplots(
        nrows, ncols,
        figsize=(max(4.0, ncols * 3.2), max(3.0, nrows * 2.8)),
        squeeze=False,
    )

    n_worms_total = len(kept)
    title = f"Crawling overview — {len(conditions)} condition(s), {n_worms_total} worms kept (span ≥{min_span_s:.0f}s)"
    if elapsed_s is not None:
        title += f", {elapsed_s / 60:.1f} min"
    fig.suptitle(title, fontsize=12)

    xlabels = [f"{c}\n(n={n_per_cond[c]})" for c in conditions]

    for idx, metric in enumerate(metrics):
        ax = axes[idx // ncols][idx % ncols]
        data_by_cond = []
        single_vals = []
        all_single = True
        for c in conditions:
            vals = pd.to_numeric(
                kept[kept["condition"].astype(str) == c][metric], errors="coerce"
            ).values.astype(float)
            vals = vals[np.isfinite(vals)]
            data_by_cond.append(vals)
            single_vals.append(float(np.mean(vals)) if len(vals) else 0.0)
            if len(vals) > 1:
                all_single = False

        if all_single:
            ax.bar(range(len(conditions)), single_vals,
                   color="#4caf50", alpha=0.85)
        else:
            # Box plot; positions with no data are skipped by matplotlib if empty,
            # so substitute a single-value list to keep alignment.
            box_data = [v if len(v) else np.array([np.nan]) for v in data_by_cond]
            ax.boxplot(box_data, positions=range(len(conditions)), widths=0.6,
                       showfliers=False)

        ax.set_title(_LABELS.get(metric, metric), fontsize=9)
        ax.set_xticks(range(len(conditions)))
        ax.set_xticklabels(xlabels, fontsize=7, rotation=0)
        ax.tick_params(axis="y", labelsize=7)

    # Hide any unused panels.
    for idx in range(n, nrows * ncols):
        axes[idx // ncols][idx % ncols].axis("off")

    fig.tight_layout(rect=(0, 0, 1, 0.96))
    try:
        fig.savefig(str(out_path), dpi=110)
    finally:
        plt.close(fig)
    log.info("crawling overview written: %s", out_path)
