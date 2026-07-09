# save as inspect_filter_decisions.py
import h5py
import numpy as np

path = r"C:\Users\Isabe\Desktop\Tierpsyclips\sweep_phase1d_2026-05-13_171334\run_000_baseline\Results\sick_worm_clip_featuresN.hdf5"

print("=== HDF5 structure ===")
with h5py.File(path, "r") as f:
    def visit(name, obj):
        if isinstance(obj, h5py.Dataset):
            print(f"  {name}: shape={obj.shape}, dtype={obj.dtype}")
    f.visititems(visit)

print("\n=== Fragment 4: per-row metrics vs was_skeletonized ===")
with h5py.File(path, "r") as f:
    td = f["/trajectories_data"][:]
    rows = td[td["worm_index_joined"] == 4]
    accepted = rows[rows["was_skeletonized"] == 1]
    rejected = rows[rows["was_skeletonized"] == 0]
    print(f"Accepted: {len(accepted)}  Rejected: {len(rejected)}")
    for field in rows.dtype.names:
        if field in {"worm_index_joined", "was_skeletonized", "skeleton_id",
                     "frame_number", "timestamp_raw", "timestamp_time",
                     "old_trajectory_data_index"}:
            continue
        try:
            a, r = accepted[field], rejected[field]
            if len(a) == 0 or len(r) == 0:
                continue
            print(f"\n  {field}:")
            print(f"    accepted: mean={float(a.mean()):.3f}  std={float(a.std()):.3f}  range=[{float(a.min()):.3f}, {float(a.max()):.3f}]")
            print(f"    rejected: mean={float(r.mean()):.3f}  std={float(r.std()):.3f}  range=[{float(r.min()):.3f}, {float(r.max()):.3f}]")
        except Exception as e:
            print(f"  {field}: skipped ({e})")