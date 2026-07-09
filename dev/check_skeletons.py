import h5py
import numpy as np

path = r"C:\Users\Isabe\Desktop\Tierpsyclips\sweep_phase1d_2026-05-13_171334\run_000_baseline\Results\sick_worm_clip_featuresN.hdf5"

with h5py.File(path, "r") as f:
    td = f["/trajectories_data"][:]
    sids = td["skeleton_id"]
    widx = td["worm_index_joined"]
    print(f"Total rows: {len(sids)}")
    print(f"Rows with skeleton_id=-1: {int((sids == -1).sum())}")
    fragments_with_minus_one = sorted(set(widx[sids == -1].tolist()))
    print(f"Fragments with any -1: {fragments_with_minus_one}")
    print(f"Number of such fragments: {len(fragments_with_minus_one)}")