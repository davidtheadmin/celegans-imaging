import logging
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


def make_video_summary_png(
    fragment_rows: list[dict],
    hdf5_path: Path,
    fps: float,
    out_path: Path,
) -> None:
    """Two-panel plot: BPM bar chart + curvature traces for top 3 longest fragments."""
    long_rows = [r for r in fragment_rows if r["is_long"]]
    if not long_rows:
        log.warning("No long fragments for %s — skipping plot", out_path.name)
        return

    bpms_sorted = sorted(r["bpm"] for r in long_rows)
    median_bpm = float(np.median(bpms_sorted))

    top3 = sorted(long_rows, key=lambda r: r["duration_s"], reverse=True)[:3]
    top3_indices = [r["worm_index"] for r in top3]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4))
    fig.suptitle(f"{out_path.stem}  —  median BPM: {median_bpm:.1f}")

    # Panel 1: bar chart of BPM per long fragment, sorted ascending
    ax1.bar(range(len(bpms_sorted)), bpms_sorted, color="#4caf50", alpha=0.8)
    ax1.axhline(median_bpm, color="red", linestyle="--", linewidth=1.5,
                label=f"median {median_bpm:.1f}")
    ax1.set_xlabel("Fragment (sorted by BPM)")
    ax1.set_ylabel("Bends per minute")
    ax1.set_title("BPM per long fragment")
    ax1.legend(fontsize=9)

    # Panel 2: raw curvature trace for top 3 longest fragments
    try:
        df = pd.read_hdf(str(hdf5_path), key="timeseries_data")
        colors = ["#1976d2", "#f57c00", "#7b1fa2"]
        for idx, worm_index in enumerate(top3_indices):
            grp = df[df["worm_index"] == worm_index]["curvature_midbody"].dropna().values
            if len(grp) == 0:
                continue
            t = np.arange(len(grp)) / fps
            ax2.plot(t, grp, color=colors[idx % len(colors)], alpha=0.8,
                     linewidth=0.8, label=f"worm {worm_index}")
        ax2.set_xlabel("Time (s)")
        ax2.set_ylabel("Curvature (midbody)")
        ax2.set_title("Body wiggle — midline-crossing = half a bend")
        if top3_indices:
            ax2.legend(fontsize=9)
    except Exception as exc:
        log.warning("Could not load curvature data for %s: %s", out_path.name, exc)
        ax2.text(0.5, 0.5, "Curvature data unavailable",
                 ha="center", va="center", transform=ax2.transAxes)

    plt.tight_layout()
    plt.savefig(out_path, dpi=100)
    plt.close(fig)


def make_overview_png(
    summary_rows: list[dict], out_path: Path, elapsed_s: float
) -> None:
    """Box-and-whisker BPM by condition (or bar chart if single condition)."""
    df = pd.DataFrame(summary_rows)
    ok = df[df["status"] == "ok"].copy()
    if ok.empty:
        log.warning("No successful videos — skipping overview plot")
        return

    conditions = sorted(ok["condition"].unique())
    n_videos = len(ok)
    title = f"Motility overview — {n_videos} videos, {elapsed_s / 60:.1f} min"

    fig, ax = plt.subplots(figsize=(max(6, len(conditions) * 1.8), 5))
    fig.suptitle(title)

    if len(conditions) <= 1:
        plates = ok["plate"].tolist()
        bpms = ok["bpm_median_long"].tolist()
        ax.bar(range(len(plates)), bpms, color="#4caf50", alpha=0.8)
        ax.set_xticks(range(len(plates)))
        ax.set_xticklabels(plates, rotation=45, ha="right")
        ax.set_xlabel("Plate")
        ax.set_ylabel("Median BPM (long fragments)")
        ax.set_title(conditions[0] if conditions else "unlabeled")
    else:
        data = [
            ok[ok["condition"] == c]["bpm_median_long"].dropna().tolist()
            for c in conditions
        ]
        bp = ax.boxplot(data, labels=conditions, patch_artist=True)
        for patch in bp["boxes"]:
            patch.set_facecolor("#4caf5066")

        rng = np.random.default_rng(42)
        for i, vals in enumerate(data):
            if vals:
                jitter = rng.normal(0, 0.05, size=len(vals))
                ax.scatter(
                    np.full(len(vals), i + 1) + jitter,
                    vals,
                    color="#1976d2", zorder=5, alpha=0.8, s=30,
                )
            y_top = ax.get_ylim()[1]
            ax.text(i + 1, y_top * 0.98, f"n={len(vals)}",
                    ha="center", va="top", fontsize=8)

        ax.set_xlabel("Condition")
        ax.set_ylabel("Median BPM (long fragments)")
        ax.set_title("BPM by condition")
        plt.xticks(rotation=45, ha="right")

    plt.tight_layout()
    plt.savefig(out_path, dpi=100)
    plt.close(fig)
