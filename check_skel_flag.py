# save as check_skel_flag.py
import h5py
import numpy as np

path = r"C:\Users\Isabe\Desktop\Tierpsyclips\sweep_phase1d_2026-05-13_171334\run_000_baseline\Results\sick_worm_clip_featuresN.hdf5"

with h5py.File(path, "r") as f:
    td = f["/trajectories_data"][:]
    print("Available fields:", td.dtype.names)
    
    for frag_id in [2, 4, 7, 8, 11]:
        mask = td["worm_index_joined"] == frag_id
        n = mask.sum()
        if n == 0:
            continue
        sid_valid = (td["skeleton_id"][mask] != -1).sum()
        print(f"\nFragment {frag_id}: {n} rows")
        print(f"  skeleton_id != -1:   {sid_valid} ({100*sid_valid/n:.1f}%)")
        for flag_name in ["is_good_skel", "was_skeletonized", "has_skeleton"]:
            if flag_name in td.dtype.names:
                count = int((td[flag_name][mask] == 1).sum())
                print(f"  {flag_name} == 1: {count} ({100*count/n:.1f}%)")