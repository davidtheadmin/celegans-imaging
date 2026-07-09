"""
Inspect Tierpsy skeleton extraction failures for a single trajectory fragment.

List mode:
    python inspect_skeleton_failures.py --results-dir <run_dir> --list-fragments

Inspect mode:
    python inspect_skeleton_failures.py --results-dir <run_dir> --fragment-id 42

ID map mode:
    python inspect_skeleton_failures.py --results-dir <run_dir> --id-map
"""
import argparse
import sys
from pathlib import Path

import cv2
import h5py
import numpy as np


# ---------------------------------------------------------------------------
# HDF5 helpers
# ---------------------------------------------------------------------------

def find_hdf5_pair(results_dir: Path) -> tuple[Path, Path]:
    """Return (masked_video_path, featuresN_path) from a Tierpsy run dir."""
    masked_dir = results_dir / "MaskedVideos"
    results_subdir = results_dir / "Results"

    masked_files = list(masked_dir.glob("*.hdf5"))
    features_files = list(results_subdir.glob("*_featuresN.hdf5"))

    if not masked_files:
        raise FileNotFoundError(f"No .hdf5 found in {masked_dir}")
    if not features_files:
        raise FileNotFoundError(f"No *_featuresN.hdf5 found in {results_subdir}")

    masked_stems = {f.stem: f for f in masked_files}
    for feat in features_files:
        stem = feat.stem.removesuffix("_featuresN")
        if stem in masked_stems:
            return masked_stems[stem], feat

    return masked_files[0], features_files[0]


def load_trajectories(
    feat_path: Path, fragment_id: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Return (frame_numbers, skeleton_ids, coord_x, coord_y, was_skeletonized) sorted by frame."""
    with h5py.File(feat_path, "r") as f:
        traj = f["/trajectories_data"]
        worm_index = traj["worm_index_joined"][:]
        frame_numbers = traj["frame_number"][:]
        skeleton_ids = traj["skeleton_id"][:]
        xs = traj["coord_x"][:]
        ys = traj["coord_y"][:]
        was_skeletonized = traj["was_skeletonized"][:]

    mask = worm_index == fragment_id
    if not mask.any():
        raise ValueError(f"Fragment {fragment_id} not found in trajectories_data.")

    order = np.argsort(frame_numbers[mask])
    return (
        frame_numbers[mask][order],
        skeleton_ids[mask][order],
        xs[mask][order],
        ys[mask][order],
        was_skeletonized[mask][order],
    )


def compute_validity(traj_data_rows) -> np.ndarray:
    """Return a boolean array of per-frame skeleton validity.

    A frame is considered valid only if Tierpsy itself marked it as skeletonized
    via the `was_skeletonized` flag in trajectories_data. This matches what the
    skeleton overlay renderer draws.
    """
    return np.asarray(traj_data_rows["was_skeletonized"]) == 1


def load_masked_frame(masked_path: Path, frame_number: int) -> np.ndarray | None:
    """Return a single grayscale frame from the masked video HDF5."""
    with h5py.File(masked_path, "r") as f:
        ds = f["/mask"]
        n_frames = ds.shape[0]
        if frame_number < 0 or frame_number >= n_frames:
            return None
        frame = ds[frame_number]  # (H, W) uint8
    return frame


# ---------------------------------------------------------------------------
# List mode
# ---------------------------------------------------------------------------

def list_fragments(results_dir: Path):
    masked_path, feat_path = find_hdf5_pair(results_dir)
    print(f"Features : {feat_path}")
    print(f"Masked   : {masked_path}\n")

    with h5py.File(feat_path, "r") as f:
        traj = f["/trajectories_data"]
        worm_index = traj["worm_index_joined"][:]
        frame_numbers = traj["frame_number"][:]
        skeleton_ids = traj["skeleton_id"][:]
        xs = traj["coord_x"][:]
        ys = traj["coord_y"][:]
        was_skeletonized = traj["was_skeletonized"][:]

    fragment_ids = np.unique(worm_index)
    rows = []
    for fid in fragment_ids:
        mask = worm_index == fid
        n = mask.sum()

        valid = compute_validity({"was_skeletonized": was_skeletonized[mask]})
        n_valid = valid.sum()
        valid_frac = n_valid / n if n > 0 else 0.0

        transitions = int(np.diff(valid.astype(int)).astype(bool).sum())
        mean_x = float(np.nanmean(xs[mask]))
        mean_y = float(np.nanmean(ys[mask]))

        rows.append((fid, n, valid_frac, transitions, mean_x, mean_y))

    rows.sort(key=lambda r: r[1], reverse=True)

    header = f"{'frag_id':>10}  {'n_frames':>9}  {'valid_frac':>10}  {'n_trans':>7}  {'mean_x':>8}  {'mean_y':>8}"
    print(header)
    print("-" * len(header))
    for fid, n, vf, nt, mx, my in rows:
        print(f"{fid:>10}  {n:>9}  {vf:>10.3f}  {nt:>7}  {mx:>8.1f}  {my:>8.1f}")


# ---------------------------------------------------------------------------
# ID map mode
# ---------------------------------------------------------------------------

def render_id_map(results_dir: Path):
    masked_path, feat_path = find_hdf5_pair(results_dir)
    print(f"Features : {feat_path}")
    print(f"Masked   : {masked_path}")

    with h5py.File(feat_path, "r") as f:
        traj = f["/trajectories_data"]
        worm_index = traj["worm_index_joined"][:]
        frame_numbers = traj["frame_number"][:]
        xs = traj["coord_x"][:]
        ys = traj["coord_y"][:]

    all_frames = np.unique(frame_numbers)
    mid_frame = int(np.median(all_frames))

    gray = load_masked_frame(masked_path, mid_frame)
    if gray is None:
        for fn in sorted(all_frames, key=lambda x: abs(x - mid_frame)):
            gray = load_masked_frame(masked_path, int(fn))
            if gray is not None:
                mid_frame = int(fn)
                break

    if gray is None:
        print("Could not load any frame for ID map.", file=sys.stderr)
        sys.exit(1)

    norm = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
    bgr = cv2.cvtColor(norm, cv2.COLOR_GRAY2BGR)

    for fid in np.unique(worm_index):
        fmask = worm_index == fid
        fframes = frame_numbers[fmask]
        fxs = xs[fmask]
        fys = ys[fmask]

        closest = int(np.argmin(np.abs(fframes - mid_frame)))
        cx = int(fxs[closest])
        cy = int(fys[closest])
        label = str(int(fid))

        # Dark outline then bright cyan fill for readability on any background
        cv2.putText(bgr, label, (cx, cy), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(bgr, label, (cx, cy), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 255), 2, cv2.LINE_AA)

    out_path = results_dir / "fragment_id_map.png"
    cv2.imwrite(str(out_path), bgr)
    print(f"\nSaved: {out_path}  (frame {mid_frame})")


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------

FONT = cv2.FONT_HERSHEY_SIMPLEX
FONT_SCALE = 0.5
FONT_THICKNESS = 1


def _crop_to_worm(
    gray_norm: np.ndarray, cx: float, cy: float, crop_size: int
) -> tuple[np.ndarray, float, float]:
    """Return (patch, off_x, off_y) — a crop_size×crop_size window around (cx, cy).

    off_x/off_y is the top-left corner of the requested window in original image
    space (pre-clamp). Subtract from skeleton coords before drawing on the patch.
    Black-pads if the window extends past the image edge.
    """
    H, W = gray_norm.shape
    half = crop_size // 2

    x0, y0 = int(cx) - half, int(cy) - half
    x1, y1 = x0 + crop_size, y0 + crop_size

    cx0, cy0 = max(0, x0), max(0, y0)
    cx1, cy1 = min(W, x1), min(H, y1)

    patch = gray_norm[cy0:cy1, cx0:cx1]

    pad_l, pad_t = cx0 - x0, cy0 - y0
    pad_r, pad_b = x1 - cx1, y1 - cy1
    if any((pad_l, pad_t, pad_r, pad_b)):
        patch = cv2.copyMakeBorder(
            patch, pad_t, pad_b, pad_l, pad_r, cv2.BORDER_CONSTANT, value=0
        )

    return patch, float(x0), float(y0)


def draw_panel(
    gray_frame: np.ndarray | None,
    skeleton: np.ndarray,
    is_valid: bool,
    frame_number: int,
    is_pivot: bool,
    *,
    centroid: tuple[float, float] | None = None,
    crop_size: int = 600,
) -> np.ndarray:
    """Render one crop_size×crop_size BGR panel.

    centroid=(cx,cy): crop around the worm centroid; skeleton/overlay coords are
    translated into crop space. centroid=None: resize the full frame (legacy mode).
    """
    panel_size = crop_size

    if gray_frame is not None:
        norm = cv2.normalize(gray_frame, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)

        if centroid is not None:
            cx, cy = centroid
            patch, off_x, off_y = _crop_to_worm(norm, cx, cy, panel_size)
            bgr = cv2.cvtColor(patch, cv2.COLOR_GRAY2BGR)
            skel_draw = skeleton - [off_x, off_y]
            mask_src = patch           # contours in crop space, no scaling
            sx = sy = 1.0
        else:
            H_orig, W_orig = norm.shape
            bgr = cv2.cvtColor(norm, cv2.COLOR_GRAY2BGR)
            bgr = cv2.resize(bgr, (panel_size, panel_size))
            sx, sy = panel_size / W_orig, panel_size / H_orig
            skel_draw = skeleton
            mask_src = norm            # contours in full-frame space, scaled below
    else:
        bgr = np.zeros((panel_size, panel_size, 3), dtype=np.uint8)
        sx = sy = 1.0
        skel_draw = skeleton
        mask_src = None

    # Mask outline in faint cyan
    if mask_src is not None:
        mask_bin = (mask_src > 0).astype(np.uint8) * 255
        contours, _ = cv2.findContours(mask_bin, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        overlay = bgr.copy()
        scaled_contours = [(c * [sx, sy]).astype(np.int32) for c in contours]
        cv2.drawContours(overlay, scaled_contours, -1, (255, 255, 0), 1)
        bgr = cv2.addWeighted(bgr, 0.7, overlay, 0.3, 0)

    # Skeleton polyline
    if is_valid and np.isfinite(skel_draw).all():
        pts = (skel_draw * [sx, sy]).astype(np.int32)
        cv2.polylines(bgr, [pts.reshape(-1, 1, 2)], False, (0, 255, 0), 1, cv2.LINE_AA)
        for pt in pts[::8]:
            cv2.circle(bgr, tuple(pt), 2, (0, 200, 0), -1)

    if is_pivot:
        cv2.rectangle(bgr, (0, 0), (panel_size - 1, panel_size - 1), (0, 165, 255), 3)

    label_validity = "VALID" if is_valid else "NaN"
    color_validity = (0, 255, 0) if is_valid else (0, 0, 255)
    pivot_tag = " <PIVOT>" if is_pivot else ""
    text = f"f={frame_number}  {label_validity}{pivot_tag}"
    cv2.putText(bgr, text, (6, 20), FONT, FONT_SCALE, (0, 0, 0), FONT_THICKNESS + 1, cv2.LINE_AA)
    cv2.putText(bgr, text, (6, 20), FONT, FONT_SCALE, color_validity, FONT_THICKNESS, cv2.LINE_AA)

    return bgr


def _build_strip(
    panel_indices: list[int],
    frame_numbers: np.ndarray,
    skeletons: np.ndarray,
    valid: np.ndarray,
    coord_x: np.ndarray,
    coord_y: np.ndarray,
    masked_path: Path,
    pivot_idx: int | None,
    crop_size: int,
    no_crop: bool,
) -> np.ndarray:
    panels = []
    for pi in panel_indices:
        fn = int(frame_numbers[pi])
        gray = load_masked_frame(masked_path, fn)
        centroid = None if no_crop else (float(coord_x[pi]), float(coord_y[pi]))
        panel = draw_panel(
            gray, skeletons[pi], bool(valid[pi]), fn, pi == pivot_idx,
            centroid=centroid, crop_size=crop_size,
        )
        panels.append(panel)
    return np.concatenate(panels, axis=1)


# ---------------------------------------------------------------------------
# Inspect mode
# ---------------------------------------------------------------------------

def inspect_fragment(
    results_dir: Path,
    fragment_id: int,
    output_dir: Path,
    context_frames: int,
    max_transitions: int,
    crop_size: int,
    no_crop: bool,
    sample_frames: int | None,
):
    masked_path, feat_path = find_hdf5_pair(results_dir)
    print(f"Features : {feat_path}")
    print(f"Masked   : {masked_path}")

    frame_numbers, skeleton_ids, coord_x, coord_y, was_skeletonized = load_trajectories(feat_path, fragment_id)

    valid = compute_validity({"was_skeletonized": was_skeletonized})

    with h5py.File(feat_path, "r") as f:
        all_skel_coords = f["/coordinates/skeletons"][:]  # (N_total, 49, 2) — for drawing only

    # Build per-fragment skeleton array for drawing; NaN-fill rows where id == -1.
    n_pts, n_dims = all_skel_coords.shape[1], all_skel_coords.shape[2]
    skeletons = np.full((len(skeleton_ids), n_pts, n_dims), np.nan)
    has_sk = skeleton_ids != -1
    if has_sk.any():
        skeletons[has_sk] = all_skel_coords[skeleton_ids[has_sk]]

    n_frames = len(frame_numbers)
    n_valid = int(valid.sum())
    valid_frac = n_valid / n_frames if n_frames > 0 else 0.0

    diff = np.diff(valid.astype(np.int8))
    pivot_indices = np.where(diff != 0)[0] + 1  # first frame of each new state

    n_transitions_total = len(pivot_indices)
    n_transitions_rendered = min(n_transitions_total, max_transitions)

    if n_valid == 0:
        longest_run = 0
    else:
        max_run = cur = 0
        for v in valid:
            cur = cur + 1 if v else 0
            max_run = max(max_run, cur)
        longest_run = max_run

    fps = None
    try:
        with h5py.File(feat_path, "r") as f:
            fps = float(f["/trajectories_data"].attrs.get("fps", 0) or 0)
    except Exception:
        pass
    fps_str = f"{fps:.2f}" if fps else "unknown"

    output_dir.mkdir(parents=True, exist_ok=True)

    summary_lines = [
        f"Fragment ID     : {fragment_id}",
        f"Total frames    : {n_frames}",
        f"Valid skeletons : {n_valid} / {n_frames} ({valid_frac:.1%})",
        f"Total transitions: {n_transitions_total}",
        f"Rendered        : {n_transitions_rendered}",
        f"Longest valid run: {longest_run} frames" + (f" / {longest_run/fps:.2f} s" if fps else ""),
        f"FPS (from attrs): {fps_str}",
        "",
        "Transitions:",
    ]

    print(f"\nFragment {fragment_id}: {n_frames} frames, {n_valid} valid ({valid_frac:.1%}), {n_transitions_total} transitions")

    for t_num, pivot_idx in enumerate(pivot_indices[:n_transitions_rendered]):
        direction = "v2n" if valid[pivot_idx - 1] and not valid[pivot_idx] else "n2v"
        frame_at_pivot = int(frame_numbers[pivot_idx])
        time_into = pivot_idx / fps if fps else float("nan")
        time_str = f"{time_into:.2f} s" if fps else "n/a"
        summary_lines.append(f"  [{t_num+1:03d}] frame={frame_at_pivot}  dir={direction}  t={time_str}")

        lo = max(0, pivot_idx - context_frames)
        hi = min(n_frames - 1, pivot_idx + context_frames)
        strip = _build_strip(
            list(range(lo, hi + 1)), frame_numbers, skeletons, valid,
            coord_x, coord_y, masked_path, pivot_idx, crop_size, no_crop,
        )
        filename = f"transition_{t_num+1:03d}_frame_{frame_at_pivot}_{direction}.png"
        cv2.imwrite(str(output_dir / filename), strip)
        print(f"  Saved: {filename}")

    if n_transitions_total > max_transitions:
        note = (
            f"\n[Note: {n_transitions_total - max_transitions} additional transitions not rendered"
            f" (--max-transitions={max_transitions})]"
        )
        summary_lines.append(note)
        print(note)

    # Auto-fall-back: produce sample strip when there are no transitions
    effective_sample = sample_frames
    if n_transitions_total == 0 and sample_frames is None:
        effective_sample = 10
        print(f"  No transitions — auto-sampling {effective_sample} evenly-spaced frames.")

    if effective_sample is not None:
        n_s = min(effective_sample, n_frames)
        indices = list(np.unique(np.linspace(0, n_frames - 1, n_s, dtype=int)))
        strip = _build_strip(
            indices, frame_numbers, skeletons, valid,
            coord_x, coord_y, masked_path, None, crop_size, no_crop,
        )
        cv2.imwrite(str(output_dir / "sample_frames.png"), strip)
        print(f"  Saved: sample_frames.png ({len(indices)} frames)")

    summary_path = output_dir / "summary.txt"
    summary_path.write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
    print(f"\nSummary written to: {summary_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Inspect Tierpsy skeleton extraction failures.")
    p.add_argument("--results-dir", required=True, help="Path to a per-run Tierpsy results folder")
    p.add_argument("--list-fragments", action="store_true", help="Print fragment table and exit")
    p.add_argument("--id-map", action="store_true", help="Render labeled fragment ID map image and exit")
    p.add_argument("--fragment-id", type=int, help="worm_index_joined value to inspect")
    p.add_argument("--output-dir", help="Where to write PNGs (default: <results-dir>/inspect_fragment_<id>/)")
    p.add_argument("--context-frames", type=int, default=3, help="Frames before/after each transition (default: 3)")
    p.add_argument("--max-transitions", type=int, default=20, help="Cap on transitions rendered (default: 20)")
    p.add_argument("--crop-size", type=int, default=600, help="Panel size in pixels, centered on worm centroid (default: 600)")
    p.add_argument("--no-crop", action="store_true", help="Resize full frame instead of cropping around worm")
    p.add_argument("--sample-frames", type=int, metavar="N", help="Also render N evenly-spaced frames as sample_frames.png")
    return p.parse_args()


def main():
    args = parse_args()
    results_dir = Path(args.results_dir)
    if not results_dir.is_dir():
        print(f"Not a directory: {results_dir}", file=sys.stderr)
        sys.exit(1)

    if args.list_fragments:
        list_fragments(results_dir)
        return

    if args.id_map:
        render_id_map(results_dir)
        return

    if args.fragment_id is None:
        print("Provide --fragment-id <N>, --list-fragments, or --id-map.", file=sys.stderr)
        sys.exit(1)

    output_dir = (
        Path(args.output_dir)
        if args.output_dir
        else results_dir / f"inspect_fragment_{args.fragment_id}"
    )

    inspect_fragment(
        results_dir=results_dir,
        fragment_id=args.fragment_id,
        output_dir=output_dir,
        context_frames=args.context_frames,
        max_transitions=args.max_transitions,
        crop_size=args.crop_size,
        no_crop=args.no_crop,
        sample_frames=args.sample_frames,
    )


if __name__ == "__main__":
    main()
