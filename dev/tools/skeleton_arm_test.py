#!/usr/bin/env python3
"""
skeleton_arm_test.py — Phase 1 of the crawling pipeline revision.

Question this tool answers: WHY do Tierpsy skeletons drop out on crawling
videos, and which segmentation parameters fix it?

Diagnosis it is built on (20260530T153913_video, N2 10J day1). Baseline is
145 fragments, 2075.7 tracked worm-seconds, skeleton yield 0.692.

  * NOT collisions. 98.8% of tracked frames have no other worm within 60 px,
    and yield is 69.3% on isolated frames vs 63.3% on crowded ones.
  * NOT the plate mask. Where has_skeleton == 0, contour_area and
    skeleton_length are ALSO NaN — every single one. The ROI never produced a
    contour, so the failure is inside SKE_CREATE's per-ROI binarisation.
  * It is the ILLUMINATION GRADIENT. The temporal-median background runs 106
    counts at the frame centre to 169 in the corners (+60%), and skeleton
    yield tracks it exactly:

        r <200 px   99.3%        r 600-800    81.7%
        r 200-400   86.9%        r 800-1000   51.4%
        r 400-600   90.1%        r >1000      31.2%

    63% of all lost frames sit within 200 px of a frame border. Tierpsy's
    per-worm threshold follows the background up (113 -> 161) while measured
    blob area inflates (1392 -> 2139 px) — the signature of collapsing
    worm-to-background contrast: the ROI binarisation returns a bloated ragged
    blob with no clean head/tail, contour splitting fails, nothing is emitted.

So the arms below attack the gradient first (flat-field pre-correction, and
Tierpsy's own background subtraction), and treat worm_bw_thresh_factor /
thresh_block_size as secondary — they matter for the residual 82% interior
yield, not for the 31% in the corners.

Success is a FLAT radial profile, not merely a higher mean: yield_r0_200 minus
yield_r1000_max should collapse toward zero. An arm that lifts the average
without flattening the profile has not fixed the cause.

What it does per arm
--------------------
  1. writes params.json (crawling baseline + the arm's overrides)
  2. runs Tierpsy in the container, checkpoints COMPRESS..SKE_FILT only
     (SKE_ORIENT / INT_* / FEAT_* are not needed to judge skeleton yield and
     are the expensive tail of the pipeline)
  3. computes skeleton diagnostics from *_skeletons.hdf5
  4. renders a side-by-side diagnostic video: original | mask, skeletons drawn
     where they exist and a RED MARKER drawn wherever a worm is tracked but
     has NO skeleton — so a dropout is visible rather than merely absent

Then writes arms_comparison.csv across all arms.

The numbers are a cross-check. The video is the verdict.

Usage (from the repo root, in the launcher venv):

    python dev/tools/skeleton_arm_test.py ^
        --video "C:\\Users\\Isabe\\Documents\\WormScan\\test\\20260530T153913_video.mp4" ^
        --out   "C:\\Users\\Isabe\\Documents\\WormScan\\test\\skeleton_arms"

Useful flags:
    --arms arm0_baseline,arm1_bw105     run a subset
    --skip-tierpsy                      re-diagnose / re-render existing output
    --no-render                         numbers only
    --render-seconds 0:60               render only part of the video
    --no-reuse-mask                     force COMPRESS on every arm

Nothing here touches the launcher, crawling_params.json, or any
_wormscan_cache directory. All output goes under --out.
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parent.parent.parent
_LAUNCHER = _REPO / "launcher"
if str(_LAUNCHER) not in sys.path:
    sys.path.insert(0, str(_LAUNCHER))

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

# Keys consumed by WormScan's own code that Tierpsy's validator rejects.
_WORMSCAN_ONLY_KEYS = frozenset({"head_angle_prominence"})

# Only these checkpoints are needed to judge skeleton yield. Dropping
# SKE_ORIENT, INT_PROFILE, INT_SKE_ORIENT, FEAT_INIT and FEAT_TIERPSY removes
# the expensive tail without changing has_skeleton or is_good_skel.
_CHECKPOINTS = [
    "COMPRESS", "TRAJ_CREATE", "TRAJ_JOIN", "SKE_INIT",
    "BLOB_FEATS", "SKE_CREATE", "SKE_FILT",
]

# Parameters that change the PLATE MASK produced by COMPRESS. If an arm leaves
# all of these at baseline, its MaskedVideos file is identical to the
# baseline's and can be copied in, letting Tierpsy skip COMPRESS.
_MASK_KEYS = frozenset({
    "mask_min_area", "mask_max_area", "thresh_C", "thresh_block_size",
    "dilation_size", "keep_border_data", "is_light_background",
    "is_extract_timestamp", "expected_fps", "compression_buff",
    "save_full_interval", "mask_bgnd_buff_size", "mask_bgnd_frame_gap",
    "is_full_bgnd_subtraction",
})

# ---------------------------------------------------------------------------
# The arms
# ---------------------------------------------------------------------------
# Each value is a dict of overrides applied to launcher/crawling_params.json.
# arm0 is the current shipped configuration, re-run here so every other arm is
# compared against a baseline produced by the same container and the same
# checkpoint list rather than against the old cached result.

# "video" selects which input the arm is tracked from:
#   "raw"  — the AVI as the launcher makes it today
#   "sub"  — frame - smoothed_median + mean. Subtractive flat-field. This is
#            the correction the measurements support (see build_flatfield).
#   "div"  — frame / smoothed_median * mean. Multiplicative flat-field, kept
#            as a control so the video decides rather than the inference.
#   "subx" — frame - raw_median + mean. Also removes static lawn texture and
#            debris, but a worm that never moves is baked into the median.

ARMS: "dict[str, dict]" = {
    # --- reference: exactly what ships today -----------------------------
    "arm0_baseline": {"video": "raw"},

    # --- leading hypothesis: kill the illumination gradient --------------
    "arm1_sub": {"video": "sub"},
    "arm2_div": {"video": "div"},
    "arm3_subx": {"video": "subx"},

    # --- Tierpsy's own built-in background subtraction, no pre-pass ------
    #     (the in-container equivalent of arm1; worth knowing if it suffices)
    "arm4_bgnd": {"video": "raw", "mask_bgnd_buff_size": 50,
                  "mask_bgnd_frame_gap": 10, "is_full_bgnd_subtraction": True},

    # --- secondary: the ROI threshold, with and without flat-fielding ----
    #     A single global scalar cannot fix a spatial gradient, so arm5 is
    #     expected to disappoint; it is here to show that, and arm6 to show
    #     that the knob becomes meaningful once the field is flat.
    "arm5_bw105": {"video": "raw", "worm_bw_thresh_factor": 1.05},
    "arm6_sub_bw105": {"video": "sub", "worm_bw_thresh_factor": 1.05},

    # --- flat-field + a background window matched to the worm ------------
    #     31 px blocks against a ~155 x 12 px worm is a tight window
    "arm7_sub_block61": {"video": "sub", "thresh_block_size": 61, "thresh_C": 10},

    # --- flat-field + motility's segmentation wholesale ------------------
    "arm8_sub_motility_seg": {
        "video": "sub",
        "worm_bw_thresh_factor": 1.05,
        "thresh_C": 10,
        "thresh_block_size": 61,
        "mask_min_area": 50,
        "traj_min_area": 25,
        "filt_min_displacement": 0,
    },
}

NEIGHBOUR_PX = 60.0   # "another worm is close" radius, for the isolated-frame split
_FF_SAMPLE_FRAMES = 120   # frames sampled to build the temporal-median field
_FF_BLUR_SIGMA = 80.0     # px; illumination is low-frequency, worms are not


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _probe_fps(video: Path) -> float:
    try:
        from analysis.ffmpeg_utils import probe_fps
        return float(probe_fps(video))
    except Exception:
        return 30.0


def _ffmpeg_exe() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def _convert_to_avi(src: Path, dst: Path) -> None:
    from analysis.ffmpeg_utils import convert_to_avi
    convert_to_avi(src, dst)


def build_flatfield(avi: Path, outs: "dict[str, Path]", diag_png: Path) -> None:
    """
    Build flat-field corrected copies of the AVI.

    Why this exists. On 20260530T153913 the background is not flat: the
    temporal median runs 106 counts at the frame centre to 169 in the corners
    (+60%), and skeleton yield tracks it exactly — 99.3% at r<200 px, 51.4% at
    r 800-1000, 31.2% beyond 1000. 63% of all lost frames sit within 200 px of
    a border.

    SUBTRACT, not divide. Sampling real worms against their local background:

        radius      background   worm-bg   worm/bg
        0-200          105          50      1.48
        400-600        116          57      1.49
        800-1000       153          59      1.40
        1000-1300      160        58.5      1.37

    Background climbs 1.45x while ABSOLUTE worm contrast barely moves
    (50 -> 59). That is additive stray light, not a multiplicative gain.
    Dividing would rescale the rim worm's 59 counts down to ~52 while lifting
    the centre's 50 to ~65 — trading a level gradient for a contrast gradient
    in the wrong direction. Subtracting leaves 50 vs 59 intact on a flat base.
    "div" is still built, as a control.

    The failure mechanism is the brightness RAMP inside one worm's ROI:
    1.6 counts across a 110 px ROI at the centre, 8.7 at r 800-1000. Tierpsy's
    per-ROI threshold sits ~7 counts above local background everywhere (113 vs
    105 at the centre, 161 vs 154 at the rim). At the centre that is clean; at
    the rim a threshold 7 counts up against a +/-9-count ramp admits the bright
    half of the ramp, which is the bloated ragged blob the numbers show
    (area 1392 -> 2139) and why the contour will not split. No Tierpsy
    parameter removes a ramp — the per-ROI threshold is ALREADY locally
    adaptive, which is exactly why it tracks the background from 113 to 161.

    Fields produced:
      sub  — frame - smoothed_median + mean   (recommended)
      div  — frame / smoothed_median * mean   (control)
      subx — frame - raw_median + mean        (also strips static lawn texture,
             but bakes in any worm that never moved)
    """
    import cv2

    cap = cv2.VideoCapture(str(avi))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {avi}")
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    idx = np.linspace(0, max(n - 1, 0), min(_FF_SAMPLE_FRAMES, max(n, 1))).astype(int)
    frames = []
    for i in idx:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(i))
        ok, f = cap.read()
        if ok:
            frames.append(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY))
    cap.release()
    if not frames:
        raise RuntimeError("could not sample any frames for the flat field")

    med = np.median(np.stack(frames), axis=0).astype(np.float32)
    h, w = med.shape
    small = cv2.resize(med, (w // 8, h // 8), interpolation=cv2.INTER_AREA)
    small = cv2.GaussianBlur(small, (0, 0), _FF_BLUR_SIGMA / 8.0)
    smooth = cv2.resize(small, (w, h), interpolation=cv2.INTER_CUBIC)

    yy, xx = np.mgrid[0:h, 0:w]
    r = np.hypot(xx - w / 2, yy - h / 2)
    c, e = float(med[r < 200].mean()), float(med[r > min(h, w) * 0.65].mean())
    print(f"    illumination field: centre {c:.0f} -> rim {e:.0f} "
          f"({(e / c - 1) * 100:+.0f}%)", flush=True)

    if e / c < 1.10:
        print("    gradient is under 10% — correction would be a no-op; "
              "the arms will still run so you can confirm that", flush=True)

    vis = cv2.applyColorMap(
        cv2.normalize(med, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8),
        cv2.COLORMAP_VIRIDIS)
    cv2.imwrite(str(diag_png), cv2.resize(vis, (1200, 900)))

    plan = [("sub", smooth, "subtract"), ("div", smooth, "divide"),
            ("subx", med, "subtract")]
    for key, field, mode in plan:
        dst = outs.get(key)
        if dst is None:
            continue
        if dst.exists():
            print(f"    {dst.name} exists, skipping", flush=True)
            continue
        m = float(field.mean())
        if mode == "subtract":
            off, gain = m - field, None
        else:
            off, gain = None, m / np.maximum(field, 1.0)
        _write_corrected(avi, dst, gain, off)
        print(f"    wrote {dst.name} [{mode}] "
              f"({dst.stat().st_size / 1e6:.0f} MB)", flush=True)


def _write_corrected(src: Path, dst: Path,
                     gain: "np.ndarray | None",
                     offset: "np.ndarray | None") -> None:
    """Apply a per-pixel gain or offset to every frame and re-encode as MJPEG
    q3 — the same codec and quality the launcher's own convert_to_avi
    produces, so Tierpsy sees an input of the same character.

    In production this belongs INSIDE convert_to_avi (decode mp4 -> correct ->
    encode once). Here it is a second pass so the raw AVI stays available as
    the arm0 reference; that costs one extra MJPEG generation, identical
    across every corrected arm, so it does not bias the comparison."""
    import cv2

    cap = cv2.VideoCapture(str(src))
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    cmd = [_ffmpeg_exe(), "-y", "-f", "rawvideo", "-vcodec", "rawvideo",
           "-s", f"{w}x{h}", "-r", str(fps), "-pix_fmt", "gray",
           "-i", "pipe:0", "-vcodec", "mjpeg", "-q:v", "3", str(dst)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    try:
        while True:
            ok, f = cap.read()
            if not ok:
                break
            g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32)
            g = g * gain if gain is not None else g + offset
            proc.stdin.write(np.clip(g, 0, 255).astype(np.uint8).tobytes())
    finally:
        cap.release()
        if proc.stdin:
            try:
                proc.stdin.close()
            except Exception:
                pass
        proc.wait()


def _qualify(image: str) -> str:
    head = image.split("/", 1)[0]
    if "/" not in image or not ("." in head or ":" in head or head == "localhost"):
        image = "docker.io/" + image
    if ":" not in image.rsplit("/", 1)[-1]:
        image += ":latest"
    return image


def _run_tierpsy(root: Path, avi_name: str, arm: str, image: str,
                 engine: str, timeout_s: int) -> None:
    cmd = [
        engine, "run", "--rm",
        "-v", f"{root.as_posix()}:/sweep",
        image,
        "tierpsy_process",
        "--video_dir_root", "/sweep",
        "--mask_dir_root", f"/sweep/{arm}/MaskedVideos",
        "--results_dir_root", f"/sweep/{arm}/Results",
        "--pattern_include", avi_name,
        "--json_file", f"/sweep/{arm}/params.json",
        "--max_num_process", "1",
    ]
    print(f"    $ {' '.join(cmd)}", flush=True)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout_s, creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Tierpsy timed out after {timeout_s}s")
    (root / arm / "tierpsy_stdout.txt").write_text(r.stdout or "", encoding="utf-8")
    (root / arm / "tierpsy_stderr.txt").write_text(r.stderr or "", encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError(f"Tierpsy exited {r.returncode}; see {arm}/tierpsy_stderr.txt\n"
                           f"{(r.stderr or '').strip()[-1500:]}")


# ---------------------------------------------------------------------------
# diagnostics
# ---------------------------------------------------------------------------

def diagnose(skeletons: Path, fps: float, frame_wh: "tuple[int, int] | None" = None) -> dict:
    """Skeleton-yield diagnostics from one *_skeletons.hdf5."""
    import h5py
    import pandas as pd

    t = pd.read_hdf(str(skeletons), key="trajectories_data")
    n_rows = len(t)
    hs = (t["has_skeleton"] > 0.5).values
    gs = ((t["is_good_skel"] > 0.5).values if "is_good_skel" in t.columns
          else np.zeros(n_rows, dtype=bool))

    d: dict = {}
    d["n_fragments"] = int(t["worm_index_joined"].nunique())
    d["tracked_frames"] = int(n_rows)
    d["tracked_worm_seconds"] = round(n_rows / fps, 1)
    d["skel_yield"] = round(float(hs.mean()), 4)
    d["good_skel_yield"] = round(float(gs.mean()), 4)
    d["ske_filt_extra_loss"] = round(float((hs & ~gs).sum() / max(hs.sum(), 1)), 4)

    # concurrency
    per_frame = t.groupby("frame_number")["worm_index_joined"].nunique()
    d["concurrency_median"] = float(per_frame.median())
    d["concurrency_max"] = int(per_frame.max())

    # blob size, skeletonised vs not — the fat-blob signature
    d["area_median_skel1"] = round(float(t["area"][hs].median()), 0) if hs.any() else float("nan")
    d["area_median_skel0"] = round(float(t["area"][~hs].median()), 0) if (~hs).any() else float("nan")
    d["area_ratio_fail_over_ok"] = (round(d["area_median_skel0"] / d["area_median_skel1"], 3)
                                    if d["area_median_skel1"] else float("nan"))

    # skeleton geometry, where it exists
    try:
        with h5py.File(str(skeletons), "r") as f:
            sl = f["skeleton_length"][:]
            wm = f["width_midbody"][:]
        sid = t["skeleton_id"].values
        ok = (sid >= 0) & (sid < len(sl))
        d["skel_length_median"] = round(float(np.nanmedian(sl[sid[ok]])), 1)
        d["width_midbody_median"] = round(float(np.nanmedian(wm[sid[ok]])), 2)
    except Exception:
        d["skel_length_median"] = float("nan")
        d["width_midbody_median"] = float("nan")

    # isolated vs crowded yield — is the loss a collision problem or not?
    iso_ok = iso_n = crowd_ok = crowd_n = 0
    for _f, sub in t.groupby("frame_number"):
        x = sub["coord_x"].values.astype(float)
        y = sub["coord_y"].values.astype(float)
        h = (sub["has_skeleton"].values > 0.5)
        if len(sub) == 1:
            iso_n += 1
            iso_ok += int(h[0])
            continue
        dx = x[:, None] - x[None, :]
        dy = y[:, None] - y[None, :]
        dist = np.hypot(dx, dy)
        np.fill_diagonal(dist, np.inf)
        near = dist.min(axis=1) < NEIGHBOUR_PX
        iso_n += int((~near).sum());   iso_ok += int(h[~near].sum())
        crowd_n += int(near.sum());    crowd_ok += int(h[near].sum())
    d["isolated_frac"] = round(iso_n / max(n_rows, 1), 4)
    d["skel_yield_isolated"] = round(iso_ok / iso_n, 4) if iso_n else float("nan")
    d["skel_yield_crowded"] = round(crowd_ok / crowd_n, 4) if crowd_n else float("nan")

    # dropout run-length structure — short hiccups are cheap to bridge later,
    # long blocks are not, so the split matters more than the total.
    runs: list[int] = []
    for _w, sub in t.sort_values("frame_number").groupby("worm_index_joined"):
        n = 0
        for v in (sub["has_skeleton"].values > 0.5):
            if not v:
                n += 1
            elif n:
                runs.append(n); n = 0
        if n:
            runs.append(n)
    R = np.array(runs) if runs else np.array([0])
    d["dropout_runs"] = int(len(runs))
    d["dropout_frames_total"] = int(R.sum())
    d["dropout_frames_le3"] = int(R[R <= 3].sum())
    d["dropout_frames_4_15"] = int(R[(R > 3) & (R <= 15)].sum())
    d["dropout_frames_16_30"] = int(R[(R > 15) & (R <= 30)].sum())
    d["dropout_frames_gt30"] = int(R[R > 30].sum())

    # Radial yield — the central diagnostic. On the baseline this runs 99% at
    # the frame centre down to 31% in the corners, tracking the illumination
    # gradient. A successful arm flattens this profile; an arm that only
    # raises the mean without flattening it has not fixed the cause.
    if frame_wh:
        W, H = frame_wh
        rad = np.hypot(t["coord_x"].values - W / 2.0, t["coord_y"].values - H / 2.0)
        edge = np.minimum(np.minimum(t["coord_x"].values, W - t["coord_x"].values),
                          np.minimum(t["coord_y"].values, H - t["coord_y"].values))
        for a, b in ((0, 200), (200, 400), (400, 600), (600, 800),
                     (800, 1000), (1000, 100000)):
            m = (rad >= a) & (rad < b)
            lab = f"yield_r{a}_{b if b < 100000 else 'max'}"
            d[lab] = round(float(hs[m].mean()), 4) if m.any() else float("nan")
        d["yield_center_minus_rim"] = round(
            float(d["yield_r0_200"] - d["yield_r1000_max"]), 4)
        m = edge < 200
        d["lost_frac_near_border"] = (round(float((~hs & m).sum() / max((~hs).sum(), 1)), 4)
                                      if (~hs).any() else float("nan"))

    # headline: worm-seconds that carry a skeleton
    d["skeletonised_worm_seconds"] = round(float(hs.sum()) / fps, 1)
    return d


# ---------------------------------------------------------------------------
# diagnostic render
# ---------------------------------------------------------------------------

_PALETTE = [
    (66, 133, 244), (219, 68, 55), (244, 180, 0), (15, 157, 88),
    (171, 71, 188), (0, 172, 193), (255, 112, 67), (158, 157, 36),
    (92, 107, 192), (0, 137, 123), (240, 98, 146), (121, 85, 72),
]


def _colour(worm_id: int) -> tuple:
    return _PALETTE[int(worm_id) % len(_PALETTE)]


def render_diagnostic(avi: Path, masked: Path, skeletons: Path, out: Path,
                      fps: float, f_start: int = 0, f_end: int | None = None) -> None:
    """
    original | mask, with skeletons drawn where they exist and a red marker
    wherever a worm is TRACKED but carries NO skeleton.

    That red marker is the whole point: the stock renderer simply omits a
    missing skeleton, which makes a dropout invisible. Here every dropout is
    a red circle plus a cross, on both panels, with the worm's id beside it.
    """
    import cv2
    import h5py
    import pandas as pd

    t = pd.read_hdf(str(skeletons), key="trajectories_data")
    with h5py.File(str(skeletons), "r") as f:
        skel = f["skeleton"][:]

    by_frame: dict[int, list] = {}
    for row in t.itertuples():
        by_frame.setdefault(int(row.frame_number), []).append(
            (int(row.worm_index_joined), float(row.coord_x), float(row.coord_y),
             int(row.skeleton_id), bool(row.has_skeleton > 0.5))
        )

    cap = cv2.VideoCapture(str(avi))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {avi}")
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    ff = [_ffmpeg_exe(), "-y", "-f", "rawvideo", "-vcodec", "rawvideo",
          "-s", f"{w * 2}x{h}", "-r", str(fps), "-pix_fmt", "bgr24",
          "-i", "pipe:0", "-vcodec", "libx264", "-preset", "fast",
          "-crf", "22", "-pix_fmt", "yuv420p", str(out)]
    proc = subprocess.Popen(ff, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW)

    try:
        with h5py.File(str(masked), "r") as mf:
            mask_ds = mf["mask"]
            n_masked = int(mask_ds.shape[0])
            fn = 0
            while True:
                ret, orig = cap.read()
                if not ret:
                    break
                if fn < f_start:
                    fn += 1
                    continue
                if f_end is not None and fn > f_end:
                    break

                if fn < n_masked:
                    right = cv2.cvtColor(mask_ds[fn], cv2.COLOR_GRAY2BGR)
                    if right.shape[:2] != (h, w):
                        right = cv2.resize(right, (w, h))
                else:
                    right = np.zeros((h, w, 3), dtype=np.uint8)

                rows = by_frame.get(fn, [])
                n_tracked = len(rows)
                n_skel = 0
                for wid, cx, cy, sid, has in rows:
                    col = _colour(wid)
                    if has and 0 <= sid < len(skel):
                        pts = skel[sid]
                        if np.isfinite(pts).all():
                            n_skel += 1
                            p = pts.astype(np.int32).reshape(-1, 1, 2)
                            for panel in (orig, right):
                                cv2.polylines(panel, [p], False, col, 2, cv2.LINE_AA)
                                cv2.circle(panel, tuple(pts[0].astype(int)), 4, col, -1)
                            cv2.putText(orig, str(wid), (int(cx) + 8, int(cy) - 8),
                                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 1, cv2.LINE_AA)
                            continue
                    # tracked but no skeleton — make it loud
                    c = (int(cx), int(cy))
                    for panel in (orig, right):
                        cv2.circle(panel, c, 22, (0, 0, 255), 2, cv2.LINE_AA)
                        cv2.line(panel, (c[0] - 10, c[1] - 10), (c[0] + 10, c[1] + 10),
                                 (0, 0, 255), 2, cv2.LINE_AA)
                        cv2.line(panel, (c[0] - 10, c[1] + 10), (c[0] + 10, c[1] - 10),
                                 (0, 0, 255), 2, cv2.LINE_AA)
                    cv2.putText(orig, str(wid), (c[0] + 26, c[1] - 8),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1, cv2.LINE_AA)

                hud = (f"f{fn}  t={fn / fps:6.2f}s   tracked {n_tracked:2d}   "
                       f"skel {n_skel:2d}   lost {n_tracked - n_skel:2d}")
                combo = np.hstack([orig, right])
                cv2.rectangle(combo, (0, 0), (combo.shape[1], 34), (0, 0, 0), -1)
                cv2.putText(combo, hud, (12, 24), cv2.FONT_HERSHEY_SIMPLEX,
                            0.7, (255, 255, 255), 2, cv2.LINE_AA)
                cv2.putText(combo, "ORIGINAL", (12, h - 12), cv2.FONT_HERSHEY_SIMPLEX,
                            0.7, (200, 200, 200), 2, cv2.LINE_AA)
                cv2.putText(combo, "MASK", (w + 12, h - 12), cv2.FONT_HERSHEY_SIMPLEX,
                            0.7, (200, 200, 200), 2, cv2.LINE_AA)
                proc.stdin.write(combo.tobytes())
                fn += 1
    finally:
        cap.release()
        if proc.stdin:
            try:
                proc.stdin.close()
            except Exception:
                pass
        proc.wait()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--video", required=True, help="source .mp4 (or .avi)")
    ap.add_argument("--out", required=True, help="output root for all arms")
    ap.add_argument("--arms", default="", help="comma-separated subset of arm names")
    ap.add_argument("--base-params", default=str(_LAUNCHER / "crawling_params.json"))
    ap.add_argument("--image", default="docker.io/tierpsy/tierpsy-tracker")
    ap.add_argument("--engine", default="docker")
    ap.add_argument("--timeout-s", type=int, default=7200)
    ap.add_argument("--skip-tierpsy", action="store_true",
                    help="re-diagnose / re-render existing arm output")
    ap.add_argument("--no-render", action="store_true")
    ap.add_argument("--no-reuse-mask", action="store_true",
                    help="force COMPRESS on every arm instead of copying the "
                         "baseline mask into arms with identical mask params")
    ap.add_argument("--render-seconds", default="",
                    help="limit the render, e.g. 0:60 or 30:90")
    args = ap.parse_args()

    video = Path(args.video)
    root = Path(args.out)
    root.mkdir(parents=True, exist_ok=True)

    names = [a.strip() for a in args.arms.split(",") if a.strip()] or list(ARMS)
    unknown = [n for n in names if n not in ARMS]
    if unknown:
        sys.exit(f"unknown arm(s): {unknown}\nknown: {list(ARMS)}")

    base = json.loads(Path(args.base_params).read_text(encoding="utf-8"))
    fps = _probe_fps(video)
    print(f"video: {video}\nfps:   {fps}\nout:   {root}\narms:  {names}\n")

    # one shared AVI at the mount root
    avi = root / (video.stem + ".avi")
    if not avi.exists():
        if video.suffix.lower() == ".avi":
            shutil.copy2(video, avi)
        else:
            print("transcoding to AVI (once, shared by all arms) ...", flush=True)
            _convert_to_avi(video, avi)
    print(f"AVI:   {avi} ({avi.stat().st_size / 1e6:.0f} MB)\n")

    f_start, f_end = 0, None
    if args.render_seconds:
        a, _, b = args.render_seconds.partition(":")
        f_start = int(float(a) * fps)
        f_end = int(float(b) * fps) if b else None

    # --- flat-field inputs, built once and shared by every arm -----------
    videos = {"raw": avi,
              "sub": root / (video.stem + "_sub.avi"),
              "div": root / (video.stem + "_div.avi"),
              "subx": root / (video.stem + "_subx.avi")}
    wanted = {ARMS[n].get("video", "raw") for n in names}
    need = wanted & {"sub", "div", "subx"}
    if need and not args.skip_tierpsy:
        print("building flat-field corrected inputs (once) ...", flush=True)
        build_flatfield(avi, {k: videos[k] for k in need},
                        root / "illumination_field.png")
        print()

    rows: list[dict] = []
    mask_cache: dict[str, Path] = {}   # mask signature -> a completed MaskedVideos

    for arm in names:
        overrides = {k: v for k, v in ARMS[arm].items() if k != "video"}
        vkey = ARMS[arm].get("video", "raw")
        arm_avi = videos[vkey]
        stem = arm_avi.stem
        print(f"=== {arm} : video={vkey} {overrides or '(baseline params)'}", flush=True)
        adir = root / arm
        adir.mkdir(parents=True, exist_ok=True)

        p = copy.deepcopy(base)
        p.update(overrides)
        p["expected_fps"] = fps
        p["analysis_checkpoints"] = list(_CHECKPOINTS)
        for k in _WORMSCAN_ONLY_KEYS:
            p.pop(k, None)
        (adir / "params.json").write_text(json.dumps(p, indent=2), encoding="utf-8")

        # A MaskedVideos file is reusable only when the input video AND every
        # mask-affecting parameter match. Keyed on exactly that.
        sig = vkey + "|" + json.dumps(
            {k: p[k] for k in sorted(_MASK_KEYS) if k in p}, sort_keys=True)
        skeletons = adir / "Results" / f"{stem}_skeletons.hdf5"
        masked = adir / "MaskedVideos" / f"{stem}.hdf5"

        if not args.skip_tierpsy:
            src = mask_cache.get(sig)
            if src and src.exists() and not masked.exists() and not args.no_reuse_mask:
                masked.parent.mkdir(parents=True, exist_ok=True)
                print(f"    reusing MaskedVideos from an earlier arm "
                      f"(identical mask signature) ...", flush=True)
                shutil.copy2(src, masked)
            t0 = time.monotonic()
            _run_tierpsy(root, arm_avi.name, arm, _qualify(args.image),
                         args.engine, args.timeout_s)
            print(f"    Tierpsy done in {time.monotonic() - t0:.0f}s", flush=True)
            if masked.exists():
                mask_cache.setdefault(sig, masked)

        if not skeletons.exists():
            print(f"    !! no skeletons file at {skeletons}; skipping")
            continue

        wh = None
        if masked.exists():
            try:
                import h5py
                with h5py.File(str(masked), "r") as mf:
                    wh = (int(mf["mask"].shape[2]), int(mf["mask"].shape[1]))
            except Exception:
                wh = None
        d = diagnose(skeletons, fps, wh)
        d = {"arm": arm, "video": vkey, "overrides": json.dumps(overrides), **d}
        (adir / "diagnostics.json").write_text(json.dumps(d, indent=2), encoding="utf-8")
        rows.append(d)
        print(f"    fragments {d['n_fragments']:4d} | skel yield {d['skel_yield']:.3f} "
              f"(isolated {d['skel_yield_isolated']:.3f}) | "
              f"skel worm-sec {d['skeletonised_worm_seconds']:.0f} | "
              f"area fail/ok {d['area_ratio_fail_over_ok']}", flush=True)

        if not args.no_render and masked.exists():
            mp4 = adir / f"{arm}_sidebyside.mp4"
            print(f"    rendering {mp4.name} ...", flush=True)
            t0 = time.monotonic()
            try:
                render_diagnostic(arm_avi, masked, skeletons, mp4, fps, f_start, f_end)
                print(f"    render done in {time.monotonic() - t0:.0f}s", flush=True)
            except Exception as exc:
                print(f"    !! render failed: {exc}")

    if rows:
        cols = list(rows[0])
        out_csv = root / "arms_comparison.csv"
        with out_csv.open("w", newline="", encoding="utf-8") as fh:
            wtr = csv.DictWriter(fh, fieldnames=cols)
            wtr.writeheader()
            wtr.writerows(rows)
        print(f"\nwrote {out_csv}")
        key = ["arm", "video", "n_fragments", "skel_yield",
               "skeletonised_worm_seconds", "area_ratio_fail_over_ok",
               "dropout_frames_gt30"]
        print("\n" + "  ".join(f"{c:>26.26}" for c in key))
        for r in rows:
            print("  ".join(f"{str(r[c]):>26.26}" for c in key))
    print("\nWatch the *_sidebyside.mp4 files. Red circle = worm tracked, no skeleton.")


if __name__ == "__main__":
    main()
