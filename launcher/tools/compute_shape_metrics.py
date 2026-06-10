# launcher/tools/compute_shape_metrics.py
"""
Walks the experiment folder, computes length_cv and solidity_median per
Tierpsy worm_index for every cached _featuresN.hdf5, writes a flat CSV.

Usage:  python launcher/tools/compute_shape_metrics.py
"""
from pathlib import Path
import numpy as np
import pandas as pd
import h5py

EXPERIMENT_ROOT = Path(r"C:\Users\Isabe\Documents\WormScan\experiments\260521_Motility")
OUTPUT_CSV = EXPERIMENT_ROOT / "shape_metrics_per_tierpsy_id.csv"

rows = []
hdf5_files = list(EXPERIMENT_ROOT.rglob("*_featuresN.hdf5"))
print(f"Found {len(hdf5_files)} featuresN files")

for i, hdf5_path in enumerate(hdf5_files, 1):
    # Path looks like: .../260521_Motility/{condition}/{plate}/_wormscan_cache/.../Results/*_featuresN.hdf5
    parts = hdf5_path.parts
    try:
        cache_idx = parts.index("_wormscan_cache")
    except ValueError:
        print(f"  skip (no _wormscan_cache in path): {hdf5_path}")
        continue
    condition = parts[cache_idx - 2]
    plate = parts[cache_idx - 1]
    print(f"[{i}/{len(hdf5_files)}] {condition} / {plate}")

    try:
        with h5py.File(hdf5_path, "r") as f:
            ts = pd.DataFrame(f["timeseries_data"][:])
            traj = pd.DataFrame(f["trajectories_data"][:])
            bf = pd.DataFrame(f["blob_features"][:])
            bf["worm_index"] = traj["worm_index_joined"].values
    except Exception as exc:
        print(f"  read error: {exc}")
        continue

    for wi in sorted(ts["worm_index"].unique()):
        sub_ts = ts[ts["worm_index"] == wi]
        sub_bf = bf[bf["worm_index"] == wi]

        lengths = sub_ts["length"].dropna().values
        length_cv = float(np.std(lengths) / np.mean(lengths)) \
            if len(lengths) >= 10 and np.mean(lengths) > 0 else np.nan

        sols = sub_bf["solidity"].dropna().values
        solidity_median = float(np.median(sols)) if len(sols) else np.nan

        speeds = sub_ts["speed"].dropna().values
        speed_median_abs = float(np.median(np.abs(speeds))) if len(speeds) else np.nan

        rows.append({
            "condition": condition,
            "plate": plate,
            "tierpsy_id": int(wi),
            "n_frames": len(sub_ts),
            "length_cv": round(length_cv, 4) if length_cv == length_cv else None,
            "solidity_median": round(solidity_median, 4) if solidity_median == solidity_median else None,
            "speed_median_abs": round(speed_median_abs, 3) if speed_median_abs == speed_median_abs else None,
        })

df = pd.DataFrame(rows).sort_values(["condition", "plate", "tierpsy_id"]).reset_index(drop=True)
df.to_csv(OUTPUT_CSV, index=False)
print(f"\nWrote {len(df)} rows to {OUTPUT_CSV}")