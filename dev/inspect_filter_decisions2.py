# save as inspect_filter_decisions2.py
import h5py
import numpy as np

path = r"C:\Users\Isabe\Desktop\Tierpsyclips\sweep_phase1d_2026-05-13_171334\run_000_baseline\Results\sick_worm_clip_featuresN.hdf5"

with h5py.File(path, "r") as f:
    td = f["/trajectories_data"][:]
    bf = f["/blob_features"][:]
    widths = f["/coordinates/widths"][:]
    skels = f["/coordinates/skeletons"][:]

idx = np.where(td["worm_index_joined"] == 4)[0]
acc = td["was_skeletonized"][idx] == 1
rej = ~acc
print(f"Fragment 4: {acc.sum()} accepted, {rej.sum()} rejected\n")

# blob_features ranked by Cohen's d
print("=== blob_features (sorted by effect size) ===")
results = []
for field in bf.dtype.names:
    a = bf[field][idx][acc]
    r = bf[field][idx][rej]
    pooled = np.sqrt((a.std()**2 + r.std()**2) / 2)
    d = abs(a.mean() - r.mean()) / pooled if pooled > 0 else 0
    results.append((field, d, a.mean(), r.mean(), a.std(), r.std()))

results.sort(key=lambda x: -x[1])
print(f"{'field':<22} {'cohen_d':>8} {'acc_mean':>10} {'rej_mean':>10} {'acc_std':>10} {'rej_std':>10}")
for field, d, am, rm, ast, rst in results:
    print(f"{field:<22} {d:>8.3f} {am:>10.3f} {rm:>10.3f} {ast:>10.3f} {rst:>10.3f}")

# Width metrics from /coordinates/widths
print("\n=== Width metrics along body ===")
sids = td["skeleton_id"][idx]
fw = widths[sids]  # (150, 49)
fw_clean = np.where(np.isfinite(fw) & (fw > 0), fw, np.nan)
wmax = np.nanmax(fw_clean, axis=1)
wmin = np.nanmin(fw_clean, axis=1)
wmean = np.nanmean(fw_clean, axis=1)
wstd = np.nanstd(fw_clean, axis=1)
wratio = wmax / wmin
for name, vals in [("width_max", wmax), ("width_min", wmin), ("width_mean", wmean),
                    ("width_std", wstd), ("width_ratio (max/min)", wratio)]:
    a = vals[acc][np.isfinite(vals[acc])]
    r = vals[rej][np.isfinite(vals[rej])]
    if len(a) and len(r):
        print(f"  {name}: accepted {a.mean():.3f}±{a.std():.3f}  rejected {r.mean():.3f}±{r.std():.3f}")

# Skeleton length
print("\n=== Skeleton length ===")
fs = skels[sids]  # (150, 49, 2)
lengths = np.array([
    np.sum(np.sqrt(((np.diff(s, axis=0))**2).sum(axis=1))) if np.isfinite(s).all() else np.nan
    for s in fs
])
a = lengths[acc][np.isfinite(lengths[acc])]
r = lengths[rej][np.isfinite(lengths[rej])]
if len(a) and len(r):
    print(f"  accepted: mean={a.mean():.3f}  std={a.std():.3f}  range=[{a.min():.3f}, {a.max():.3f}]")
    print(f"  rejected: mean={r.mean():.3f}  std={r.std():.3f}  range=[{r.min():.3f}, {r.max():.3f}]")