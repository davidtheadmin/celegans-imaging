"""Sweep the three Tierpsy parameters that plausibly cost motility its skeletons.

WHY THIS EXISTS
---------------
On `601 10J plate 02` (27 Aug run) only 4 worms survived out of 117 tracks, and
the flicker filter was not the cause — it flags zero frames there. What cuts
the tracks is `np.isnan(lengths)`: a frame without a skeleton counts as
flicker, so every skeleton gap severs the track.

Most of those missing skeletons are not lost worms. 105 of the 117 tracks are
under 10 s with a median area of 32 px — specks admitted by
`traj_min_area = 25` (crawling uses 500). Of the twelve tracks that last 10 s
or more, area predicts everything: the four at 396-532 px skeletonise at
51-88 %, while worm-sized tracks 7 (553 px) and 14 (291 px) run the whole video
at under 10 %. Those two are the real loss.

Three parameters differ from crawling in a way that could explain it:
`traj_min_area`, `mask_min_area`, and `worm_bw_thresh_factor` — the last being
the binarisation knob. The other three motility/crawling differences are
deliberate; crawling's `filt_min_displacement = 100` would delete every
swimmer, so they are not swept.

THE DEFAULT GRID IS 12 CELLS, and the axes are not equally interesting.
`worm_bw_thresh_factor` gets three levels (1.05 baseline, 0.98, 0.92 crawling)
because it is the only one that changes what is segmented, and therefore the
only one that can make a worm skeletonise that did not before. `mask_min_area`
gets two (50, 500) because it decides what reaches the mask at all.
`traj_min_area` gets two (25, 250) and is expected to be INERT on the
objective: it filters trajectories after masking, so it should remove specks
without touching a real worm's skeletons. Two levels is enough to confirm that
rather than assume it. Widen any axis with its flag if the results argue for
it.

WHAT IT SCORES
--------------
NOT overall skeleton coverage — the specks dominate it and a parameter set that
merely stops admitting specks would win while helping no worm. The objective is
the worm-like subset: tracks lasting >= --min-dur whose median area falls in
[--area-min, --area-max], and how well THOSE skeletonise. The headline column
is `n_good`, the number of them at 50 % coverage or better.

Baseline on 601 10J plate 02, measured: 117 tracks, 90 of them specks under
100 px, 12 lasting 10 s or more, 6 worm-like, n_good = 4, median coverage of
the worm-like set 0.53. A better cell raises n_good without inventing
worm-like tracks that are really clumps — watch n_wormlike too.

HOW IT RUNS
-----------
It reuses the flat-fielded AVI the pipeline already cached, so no video is
re-transcoded and flat-field is held constant across cells. Each cell gets its
own directory containing a copy of that AVI and its own params.json, because
Tierpsy is batch-oriented and writes MaskedVideos/ and Results/ beside the
video it is given.

Nothing it does touches the launcher, the run cache, or any analysis output. It
writes one CSV and, unless --keep, deletes each cell's HDF5s once measured.
Re-running skips cells already in the CSV, so it is safe to interrupt.

USAGE
-----
    launcher\\.venv\\Scripts\\python.exe dev\\tierpsy_param_sweep.py ^
        --plate "E:\\Wormdata\\260521_Motility\\601 10J\\plate 02" ^
        --out E:\\Wormdata\\_sweep --workers 6

    # a quick 3-cell shakedown before committing to the full grid
    ... --bw 1.05 0.92 --traj-min-area 25 --mask-min-area 50 500

Add --dry-run to print the grid and exit.
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "launcher"))

import numpy as np                                                # noqa: E402
import pandas as pd                                               # noqa: E402
import h5py                                                       # noqa: E402
from analysis.docker_utils import run_tierpsy                      # noqa: E402

FIELDS = ["cell", "traj_min_area", "mask_min_area", "worm_bw_thresh_factor",
          "n_tracks", "n_specks", "n_long", "n_wormlike", "n_good",
          "skel_median", "skel_p25", "skel_p75", "overall_skel_frac",
          "seconds", "status"]

_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def find_avi(plate: Path) -> Path:
    """The flat-fielded AVI the pipeline already made, so we sweep Tierpsy alone."""
    hits = sorted(plate.glob("_wormscan_cache/*/*.avi"))
    if not hits:
        raise SystemExit(
            f"No cached AVI under {plate}\\_wormscan_cache.\n"
            "Run the motility pipeline on this folder once first, or pass "
            "--avi with a path to one.")
    if len(hits) > 1:
        log(f"note: {len(hits)} AVIs found, using {hits[0].name}")
    return hits[0]


def measure(results_dir: Path, min_dur: float, area_min: float,
            area_max: float, fps: float) -> dict:
    """Score one Tierpsy output on the worm-like subset."""
    feats = sorted(results_dir.glob("*_featuresN.hdf5"))
    if not feats:
        return {"status": "no featuresN.hdf5"}
    with h5py.File(feats[0], "r") as h:
        if "trajectories_data" not in h:
            return {"status": "no trajectories_data"}
        tr = pd.DataFrame(h["trajectories_data"][:])
    if tr.empty:
        return {"status": "empty trajectories_data"}

    if "was_skeletonized" in tr.columns:
        tr["ok"] = tr["was_skeletonized"].astype(bool)
    else:
        tr["ok"] = tr["skeleton_id"].values >= 0

    g = tr.groupby("worm_index_joined").agg(
        frames=("frame_number", "size"), skel=("ok", "sum"),
        area=("area", "median"))
    g["cov"] = g["skel"] / g["frames"]
    g["dur"] = g["frames"] / fps

    wormlike = g[(g["dur"] >= min_dur) & (g["area"] >= area_min)
                 & (g["area"] <= area_max)]
    cov = wormlike["cov"].values
    return {
        "n_tracks": int(len(g)),
        "n_specks": int((g["area"] < 100).sum()),
        "n_long": int((g["dur"] >= min_dur).sum()),
        "n_wormlike": int(len(wormlike)),
        "n_good": int((cov >= 0.5).sum()),
        "skel_median": round(float(np.median(cov)), 4) if len(cov) else "",
        "skel_p25": round(float(np.percentile(cov, 25)), 4) if len(cov) else "",
        "skel_p75": round(float(np.percentile(cov, 75)), 4) if len(cov) else "",
        "overall_skel_frac": round(float(g["skel"].sum() / g["frames"].sum()), 4),
        "status": "ok",
    }


def run_cell(cell: dict, avi: Path, base: dict, outdir: Path, args) -> dict:
    """One parameter set: its own directory, its own Tierpsy run, one CSV row."""
    d = outdir / ("cell_%s" % cell["cell"])
    row = dict(cell)
    t0 = time.time()
    try:
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
        shutil.copy2(avi, d / avi.name)
        params = dict(base)
        params.update({k: cell[k] for k in
                       ("traj_min_area", "mask_min_area",
                        "worm_bw_thresh_factor")})
        pj = d / "params.json"
        pj.write_text(json.dumps(params, indent=2), encoding="utf-8")

        log(f"  [{cell['cell']}] tierpsy: traj_min_area={cell['traj_min_area']} "
            f"mask_min_area={cell['mask_min_area']} "
            f"bw={cell['worm_bw_thresh_factor']}")
        run_tierpsy(d / avi.name, pj, args.image, docker_cmd=args.docker,
                    timeout_s=args.timeout)
        row.update(measure(d / "Results", args.min_dur, args.area_min,
                           args.area_max, args.fps))
    except Exception as exc:                                       # noqa: BLE001
        row["status"] = f"FAILED: {type(exc).__name__}: {exc}"[:300]
    row["seconds"] = round(time.time() - t0, 1)
    if not args.keep:
        shutil.rmtree(d, ignore_errors=True)
    log(f"  [{cell['cell']}] {row.get('status')} "
        f"n_good={row.get('n_good','-')} median={row.get('skel_median','-')} "
        f"({row['seconds']:.0f}s)")
    return row


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--plate", type=Path,
                   help="plate folder, e.g. 'E:\\Wormdata\\260521_Motility\\601 10J\\plate 02'")
    p.add_argument("--avi", type=Path, help="use this AVI instead of finding one")
    p.add_argument("--out", type=Path, required=True, help="working directory for the sweep")
    p.add_argument("--params", type=Path,
                   default=REPO / "launcher" / "motility_params.json",
                   help="base parameter file the grid overrides")
    p.add_argument("--traj-min-area", type=float, nargs="+", default=[25, 250])
    p.add_argument("--mask-min-area", type=float, nargs="+", default=[50, 500])
    p.add_argument("--bw", type=float, nargs="+", default=[1.05, 0.98, 0.92],
                   help="worm_bw_thresh_factor values")
    p.add_argument("--min-dur", type=float, default=10.0, help="s, worm-like track floor")
    p.add_argument("--area-min", type=float, default=200.0)
    p.add_argument("--area-max", type=float, default=800.0)
    p.add_argument("--fps", type=float, default=30.0)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--timeout", type=int, default=1800, help="s per cell")
    p.add_argument("--image", default="docker.io/tierpsy/tierpsy-tracker:latest")
    p.add_argument("--docker", default="docker")
    p.add_argument("--keep", action="store_true", help="keep each cell's HDF5s")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if not args.avi and not args.plate:
        p.error("give --plate or --avi")
    avi = args.avi if args.avi else find_avi(args.plate)
    base = json.loads(Path(args.params).read_text(encoding="utf-8"))

    grid = []
    for i, (t, m, b) in enumerate(itertools.product(
            args.traj_min_area, args.mask_min_area, args.bw)):
        grid.append({"cell": f"{i:03d}", "traj_min_area": t,
                     "mask_min_area": m, "worm_bw_thresh_factor": b})

    args.out.mkdir(parents=True, exist_ok=True)
    csv_path = args.out / "sweep_results.csv"
    done = set()
    if csv_path.exists():
        with open(csv_path, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r.get("status") == "ok":
                    done.add((r["traj_min_area"], r["mask_min_area"],
                              r["worm_bw_thresh_factor"]))
    todo = [c for c in grid
            if (str(c["traj_min_area"]), str(c["mask_min_area"]),
                str(c["worm_bw_thresh_factor"])) not in done]

    print(f"video : {avi}")
    print(f"grid  : {len(grid)} cells, {len(todo)} to run, {len(grid)-len(todo)} already done")
    print(f"score : tracks >= {args.min_dur:g}s with median area "
          f"{args.area_min:g}-{args.area_max:g} px; n_good = those at >=50% coverage")
    if args.dry_run:
        for c in todo:
            print("  ", c)
        return 0
    if not todo:
        print("nothing to do")
        return 0

    new = not csv_path.exists()
    fh = open(csv_path, "a", newline="", encoding="utf-8")
    w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
    if new:
        w.writeheader()
    t0 = time.time()
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(run_cell, c, avi, base, args.out, args): c for c in todo}
            for fut in as_completed(futs):
                w.writerow(fut.result())
                fh.flush()
    finally:
        fh.close()

    rows = [r for r in csv.DictReader(open(csv_path, newline="", encoding="utf-8"))
            if r.get("status") == "ok"]
    rows.sort(key=lambda r: (-int(r["n_good"] or 0), -float(r["skel_median"] or 0)))
    print(f"\ndone in {(time.time()-t0)/60:.1f} min — {csv_path}\n")
    print("%-6s %-9s %-9s %-6s %-7s %-7s %-8s %-8s" % (
        "cell", "traj_min", "mask_min", "bw", "n_good", "n_wormy", "median", "specks"))
    for r in rows[:15]:
        print("%-6s %-9s %-9s %-6s %-7s %-7s %-8s %-8s" % (
            r["cell"], r["traj_min_area"], r["mask_min_area"],
            r["worm_bw_thresh_factor"], r["n_good"], r["n_wormlike"],
            r["skel_median"], r["n_specks"]))
    print("\nBaseline for comparison is the row at traj_min_area=25, "
          "mask_min_area=50, bw=1.05 — the settings the 27 Aug run used.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
