# inspect_filter_decisions3.py
import h5py
import numpy as np

path = r"C:\Users\Isabe\Desktop\Tierpsyclips\sweep_phase1d_2026-05-13_171334\run_000_baseline\Results\sick_worm_clip_featuresN.hdf5"
FRAGMENTS = [2, 4, 8, 10, 11, 12]  # mix of differently-flickery long fragments

with h5py.File(path, "r") as f:
    td = f["/trajectories_data"][:]
    bf = f["/blob_features"][:]

for frag_id in FRAGMENTS:
    idx = np.where(td["worm_index_joined"] == frag_id)[0]
    if len(idx) == 0:
        continue
    acc = td["was_skeletonized"][idx] == 1
    rej = ~acc
    if acc.sum() == 0 or rej.sum() == 0:
        print(f"\n=== Fragment {frag_id}: {acc.sum()} acc, {rej.sum()} rej — skipping (no contrast) ===")
        continue
    print(f"\n=== Fragment {frag_id}: {acc.sum()} acc, {rej.sum()} rej ===")
    # Position stability
    cx, cy = td["coord_x"][idx], td["coord_y"][idx]
    print(f"  Position spread: x_std={cx.std():.2f}px, y_std={cy.std():.2f}px (over whole fragment)")
    # Top discriminating blob features
    results = []
    for field in bf.dtype.names:
        a = bf[field][idx][acc]
        r = bf[field][idx][rej]
        pooled = np.sqrt((a.std()**2 + r.std()**2) / 2)
        d = abs(a.mean() - r.mean()) / pooled if pooled > 0 else 0
        results.append((field, d, a.mean(), r.mean(), a.std(), r.std()))
    results.sort(key=lambda x: -x[1])
    print(f"  Top 5 discriminators (Cohen's d):")
    print(f"    {'field':<20} {'d':>6} {'acc':>10} {'rej':>10}")
    for field, d, am, rm, _, _ in results[:5]:
        print(f"    {field:<20} {d:>6.3f} {am:>10.3f} {rm:>10.3f}")