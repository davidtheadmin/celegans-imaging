#!/usr/bin/env python3
"""
Diagnostic: frequency analysis of the head-angle signal for selected worms.

Goal: determine whether flicker affecting worms 4 and 9 in video 2 is
high-frequency noise (→ low-pass filter will fix it) or wide-band noise
(→ different approach needed).

Output PNGs at C:/Users/Isabe/Documents/WormScan/test/:
  head_angle_spectrum_<video_stem>.png   (2 x N grid, time-series + FFT)

Run:
  launcher/.venv/Scripts/python.exe launcher/tools/inspect_head_angle_spectrum.py
"""
import json
import logging
import sys
from pathlib import Path

import h5py
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))
from analysis.analysis_csv import compute_head_angle_signal

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
_PARAMS = json.loads((Path(__file__).parent.parent / "motility_params.json").read_text())
HEAD_ANGLE_PROMINENCE: float = float(_PARAMS.get("head_angle_prominence", 0.30))
FPS: float = float(_PARAMS.get("expected_fps", 30.0))

OUT_DIR = Path(r"C:\Users\Isabe\Documents\WormScan\test")

# Video 1: top 4 worms by raw trajectory length (clean, well-tracked).
# Video 2: IDs 4 + 9 (flickery targets), plus top 2 longest as clean reference.
_CACHE = r"C:\Users\Isabe\Documents\WormScan\test"
VIDEOS: list[dict] = [
    {
        "stem": "20260521T123858_video",
        "hdf5": Path(_CACHE) / "newdetectiontest01" / "_wormscan_cache"
                / "20260521T123858_video" / "Results"
                / "20260521T123858_video_featuresN.hdf5",
        "specific_ids": None,       # None → pick top 4 by length
        "n_top": 4,
    },
    {
        "stem": "20260521T124952_video",
        "hdf5": Path(_CACHE) / "newdetectiontest02" / "_wormscan_cache"
                / "20260521T124952_video" / "Results"
                / "20260521T124952_video_featuresN.hdf5",
        "specific_ids": [4, 9],     # flickery targets; topped up to 4 with longest others
        "n_top": 2,                 # add this many clean-reference worms
    },
]

BEND_BAND_HZ = (0.5, 2.0)          # expected physiological bend frequency range


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _select_worm_ids(
    traj: pd.DataFrame,
    specific_ids: "list[int] | None",
    n_top: int,
) -> "list[tuple[int, bool]]":
    """
    Return [(worm_id, is_specific), ...].
    Worms are ordered: specific IDs first (in the order given), then top-N
    longest trajectories that do not appear in specific_ids.
    """
    track_lengths = (traj.groupby("worm_index_joined")
                     .size()
                     .sort_values(ascending=False))
    all_ids = list(track_lengths.index)

    if specific_ids is None:
        return [(wid, False) for wid in all_ids[:n_top]]

    present_specific = [(wid, True) for wid in specific_ids if wid in track_lengths.index]
    missing = [wid for wid, _ in present_specific if wid not in track_lengths.index]
    if missing:
        log.warning("Requested worm IDs not in trajectories_data: %s", missing)

    specific_set = {wid for wid, _ in present_specific}
    top_others = [(wid, False) for wid in all_ids if wid not in specific_set][:n_top]
    return present_specific + top_others


def _compute_signal(
    worm_id: int,
    traj: pd.DataFrame,
    skel_all: np.ndarray,
    fps: float,
) -> "dict | None":
    frame_col = ("timestamp_raw" if "timestamp_raw" in traj.columns
                 else "frame_number")
    worm_traj = (traj[traj["worm_index_joined"] == worm_id]
                 .sort_values(frame_col)
                 .reset_index(drop=True))
    n_raw_frames = len(worm_traj)
    sig = compute_head_angle_signal(worm_traj, skel_all, fps, HEAD_ANGLE_PROMINENCE)
    if sig is None:
        log.warning("  worm %d: compute_head_angle_signal returned None "
                    "(fewer than 10 valid skeleton frames)", worm_id)
        return None
    sig["n_raw_frames"] = n_raw_frames
    return sig


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _process_video(cfg: dict) -> None:
    stem: str = cfg["stem"]
    hdf5_path: Path = cfg["hdf5"]
    specific_ids: "list[int] | None" = cfg["specific_ids"]
    n_top: int = cfg["n_top"]

    if not hdf5_path.exists():
        log.warning("HDF5 not found: %s — skipping %s", hdf5_path, stem)
        return

    log.info("Loading %s …", stem)
    traj = pd.read_hdf(str(hdf5_path), key="trajectories_data")
    with h5py.File(str(hdf5_path), "r") as fh:
        skel_all: np.ndarray = fh["coordinates"]["skeletons"][:]

    worm_list = _select_worm_ids(traj, specific_ids, n_top)
    log.info("  worms selected: %s", [(wid, "specific" if sp else "top-N")
                                       for wid, sp in worm_list])

    results: list[dict] = []
    for worm_id, is_specific in worm_list:
        log.info("  computing signal for worm %d …", worm_id)
        sig = _compute_signal(worm_id, traj, skel_all, FPS)
        if sig is not None:
            results.append({"worm_id": worm_id, "is_specific": is_specific, "sig": sig})

    if not results:
        log.warning("No valid signals for %s — no PNG written", stem)
        return

    n_cols = len(results)
    fig, axes = plt.subplots(2, n_cols,
                             figsize=(max(n_cols * 4, 8), 8),
                             squeeze=False)
    fig.suptitle(f"{stem} — head-angle time series & frequency spectrum",
                 fontsize=11, y=1.01)

    for col, r in enumerate(results):
        worm_id: int = r["worm_id"]
        is_specific: bool = r["is_specific"]
        sig: dict = r["sig"]

        t_sec = sig["frame_nums"] / FPS
        signal = sig["detrended"]
        n_raw = sig["n_raw_frames"]
        n_valid = sig["n_valid"]
        label_suffix = " [flickery]" if is_specific else ""

        # --- time series ---
        ax_t = axes[0][col]
        ax_t.plot(t_sec, signal, color="steelblue", linewidth=0.7)
        ax_t.axhline(0, color="gray", linewidth=0.5, linestyle="--")
        # mark detected peaks
        if len(sig["pos_peaks"]):
            ax_t.scatter(t_sec[sig["pos_peaks"]], signal[sig["pos_peaks"]],
                         color="red", s=12, zorder=5, label="peaks")
        if len(sig["neg_peaks"]):
            ax_t.scatter(t_sec[sig["neg_peaks"]], signal[sig["neg_peaks"]],
                         color="red", s=12, zorder=5)
        ax_t.set_title(f"Worm {worm_id}{label_suffix}\n"
                       f"N_raw={n_raw}, N_valid={n_valid}",
                       fontsize=8)
        ax_t.set_xlabel("Time (s)", fontsize=8)
        ax_t.set_ylabel("Head angle (rad)", fontsize=8)
        ax_t.tick_params(labelsize=7)

        # --- FFT ---
        ax_f = axes[1][col]
        n_pts = len(signal)
        # subtract mean before FFT (remove DC residual after detrend)
        fft_mag = np.abs(np.fft.rfft(signal - signal.mean())) * (2.0 / n_pts)
        freqs = np.fft.rfftfreq(n_pts, d=1.0 / FPS)
        ax_f.semilogy(freqs, fft_mag + 1e-9, color="darkorange", linewidth=0.8)
        ax_f.axvspan(*BEND_BAND_HZ, alpha=0.12, color="green",
                     label=f"{BEND_BAND_HZ[0]}–{BEND_BAND_HZ[1]} Hz (bend)")
        ax_f.set_xlim(0, FPS / 2)
        ax_f.set_xlabel("Frequency (Hz)", fontsize=8)
        ax_f.set_ylabel("Magnitude (log, normalised)", fontsize=8)
        ax_f.tick_params(labelsize=7)
        ax_f.set_title(f"FFT — worm {worm_id}{label_suffix}", fontsize=8)
        if col == 0:
            ax_f.legend(fontsize=7)

    fig.tight_layout()
    out_path = OUT_DIR / f"head_angle_spectrum_{stem}.png"
    fig.savefig(out_path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    log.info("Saved → %s", out_path)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for cfg in VIDEOS:
        _process_video(cfg)


if __name__ == "__main__":
    main()
