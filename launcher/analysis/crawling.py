"""
Crawling analysis agent. Parallel to the Motility pipeline (analysis/motility.py).

Started as a copy of the motility pipeline but has diverged substantially: it
uses different Tierpsy parameters (crawling_params.json), its own position-based
fragment linker (crawling_fragment_grouping, not the motility grouping engine),
and a different output schema (per-worm / per-condition tables with body-length-
normalized, activity, and velocity-arrow columns). The shared ffmpeg/AVI step
remains. The Tierpsy docker subprocess call is heavily instrumented here for
diagnostics (see _run_tierpsy_instrumented).

Thread boundary mirrors SyncAgent/SyncStatus and MotilityAgent/MotilityStatus.

Write contract — worker thread ONLY: call status.update() / status.mark_completed()
Read contract  — UI thread ONLY: call status.snapshot() / status.pop_completed()

Never touch Tk widgets from this thread.
"""
import copy
import hashlib
import json
import logging
import re
import shlex
import shutil
import traceback
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import paths
from analysis import engine as engine_mod
from analysis.stage_tracker import StageTracker, tierpsy_phase

log = logging.getLogger(__name__)

_ANALYSIS_PREFIX = "_crawling_analysis"
_CACHE_DIR = "_wormscan_cache"

# Diagnostic Tierpsy timeout for the crawling pipeline (bumped from 600s → 3600s).
_TIERPSY_TIMEOUT_S = 3600

# Keys in motility_params.json that are consumed by our post-Tierpsy code and
# must never be written into the per-video JSON that Tierpsy validates.
_WORMSCAN_ONLY_KEYS: frozenset[str] = frozenset({"head_angle_prominence"})

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _resolve_video_path(video: Path, selected_folder: Path) -> tuple[str, str]:
    """
    Return (condition, plate) for a video relative to selected_folder.

    Depth 0  root/video.mp4                  → condition="default",  plate=video.stem
    Depth 1  root/plate/video.mp4             → condition="default",  plate=parent.name
    Depth 2+ root/condition/plate/video.mp4   → condition=grandparent.name, plate=parent.name
    """
    try:
        rel = video.relative_to(selected_folder)
    except ValueError:
        return "default", video.stem
    depth = len(rel.parts) - 1
    if depth == 0:
        return "default", video.stem
    elif depth == 1:
        return "default", video.parent.name
    else:
        return video.parent.parent.name, video.parent.name


def _cache_dir_for(video: Path) -> Path:
    """Return the per-video cache directory next to the original MP4."""
    return video.parent / _CACHE_DIR / video.stem


def _hdf5_cache_valid(hdf5_path: Path) -> bool:
    """Return True if hdf5_path exists and contains /trajectories_data."""
    if not hdf5_path.exists():
        return False
    try:
        import h5py
        with h5py.File(str(hdf5_path), "r") as fh:
            return "trajectories_data" in fh
    except Exception:
        return False


_CACHE_STAMP = ".wormscan-cache.json"


def _params_fingerprint(pipeline: str, params: dict,
                        flat_field: bool = False) -> str:
    """Identify the Tierpsy run that produced a cache entry.

    Covers BOTH the parameter set and which pipeline ran, because motility and
    crawling share `_wormscan_cache/<stem>/` and their params diverge on
    mask_min_area, traj_min_area, traj_max_allowed_dist, traj_max_frames_gap
    and filt_min_displacement. Without the pipeline tag, running one pipeline
    then the other on the same folder made the second silently reuse the
    first's tracking, and which result you got depended on the order you ran
    them in.

    expected_fps is excluded (patched per video from the probed fps, so it is
    a property of the video, not of the settings), and so are the WormScan-only
    keys, which are consumed after Tierpsy and therefore do not invalidate its
    output.
    """
    payload = {k: v for k, v in (params or {}).items()
               if k != "expected_fps" and k not in _WORMSCAN_ONLY_KEYS}
    blob = json.dumps({"pipeline": pipeline, "params": payload,
                       "flat_field": bool(flat_field)},
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _cache_stamp_check(cache_dir: Path, pipeline: str,
                       want: str) -> tuple[bool, str]:
    """(reusable, why not). Anything unreadable or unstamped is NOT reusable."""
    stamp = cache_dir / _CACHE_STAMP
    if not stamp.is_file():
        return False, ("it was written before the cache recorded its "
                       "parameters, so it cannot be matched against these")
    try:
        got = json.loads(stamp.read_text(encoding="utf-8"))
    except Exception:
        return False, "its cache fingerprint could not be read"
    if got.get("fingerprint") == want:
        return True, ""
    prev = str(got.get("pipeline") or "unknown")
    if prev != pipeline:
        return False, f"it was produced by the {prev} pipeline, not {pipeline}"
    return False, "the Tierpsy parameters have changed since it was written"


def _write_cache_stamp(cache_dir: Path, pipeline: str, want: str) -> None:
    """Stamp a cache entry only after Tierpsy actually produced its HDF5."""
    try:
        (cache_dir / _CACHE_STAMP).write_text(json.dumps({
            "pipeline": pipeline,
            "fingerprint": want,
            "written_at": datetime.now().isoformat(timespec="seconds"),
        }, indent=2), encoding="utf-8")
    except OSError:
        # A cache that cannot be stamped is simply re-run next time.
        log.warning("Could not write cache stamp in %s", cache_dir)


# ---------------------------------------------------------------------------
# Instrumented Tierpsy docker call (crawling-only diagnostics)
# ---------------------------------------------------------------------------

def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KB"
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:.1f} MB"
    return f"{n / 1024 ** 3:.2f} GB"


def _print_output_tree(root: Path, prefix: str = "") -> None:
    """Print a recursive listing of the Tierpsy output dir, with file sizes."""
    print(f"{prefix}Tierpsy output dir listing: {root}", flush=True)
    if not root.exists():
        print(f"{prefix}  (directory does not exist)", flush=True)
        return
    entries = sorted(root.rglob("*"), key=lambda p: p.as_posix())
    if not entries:
        print(f"{prefix}  (empty)", flush=True)
        return
    for p in entries:
        try:
            rel = p.relative_to(root).as_posix()
            if p.is_dir():
                print(f"{prefix}  {rel}/", flush=True)
            else:
                size = p.stat().st_size
                print(f"{prefix}  {rel}  ({_fmt_size(size)}, {size} bytes)", flush=True)
        except OSError as exc:
            print(f"{prefix}  {p}  (stat error: {exc})", flush=True)




def _run_tierpsy_instrumented(
    video_avi: Path,
    json_file: Path,
    image: str,
    output_dir: Path,
    engine: object = None,
    timeout_s: int = _TIERPSY_TIMEOUT_S,
    tag: str = "",
    on_stage: Optional[Callable[[str], None]] = None,
) -> tuple[str, str]:
    """
    Run Tierpsy on a single video via Docker, with diagnostic instrumentation.
    Returns (stdout, stderr); stderr is folded into stdout because we stream a
    combined stream line-by-line. Raises RuntimeError on non-zero exit or timeout.

    Diagnostics (crawling pipeline only):
      - Prints the exact docker command before running.
      - Streams docker stdout+stderr to the console in real time (line by line).
      - On any exit (success, failure, timeout) prints a recursive listing of the
        Tierpsy output directory for this video, with file sizes.

    tierpsy_process is batch-oriented: it scans --video_dir_root for files
    matching --pattern_include rather than accepting a single --video_file.
    We mount the video's parent as /data and pass the basename as the pattern
    so only this one file is processed.
    """
    # One definition of the Tierpsy invocation, shared with
    # docker_utils.run_tierpsy. It used to be duplicated here, which meant
    # every engine fix had to be made twice and the copies drifted.
    if engine is None:
        engine = engine_mod.Engine(
            command="docker", kind="docker", version="(not detected)")
    cmd = engine_mod.build_tierpsy_cmd(engine, video_avi, json_file, image)

    cpfx = f"[{tag}] " if tag else "[crawling] "
    print(cpfx + "=" * 60, flush=True)
    print(cpfx + "Running Tierpsy docker command:", flush=True)
    print(cpfx + "  " + " ".join(shlex.quote(c) for c in cmd), flush=True)
    print(cpfx + f"timeout: {timeout_s}s", flush=True)
    print(cpfx + "=" * 60, flush=True)

    captured: list[str] = []
    timed_out = {"value": False}

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # fold stderr into the same stream for ordered streaming
        text=True,
        bufsize=1,
        creationflags=_NO_WINDOW,
    )

    def _kill_on_timeout() -> None:
        timed_out["value"] = True
        try:
            proc.kill()
        except Exception:
            pass

    timer = threading.Timer(timeout_s, _kill_on_timeout)
    timer.start()
    try:
        assert proc.stdout is not None
        last_stage = ""
        for line in proc.stdout:
            captured.append(line)
            print(f"{cpfx}[tierpsy] " + line.rstrip("\n"), flush=True)
            if on_stage is not None:
                phase = tierpsy_phase(line)
                if phase and phase != last_stage:
                    last_stage = phase
                    try:
                        on_stage(phase)
                    except Exception:                          # noqa: BLE001
                        pass       # a status update never breaks a Tierpsy run
        proc.wait()
    finally:
        timer.cancel()

    returncode = proc.returncode
    combined = "".join(captured)

    print(cpfx + "-" * 60, flush=True)
    print(
        cpfx + f"Tierpsy exited: returncode={returncode}"
        f"{' (TIMED OUT)' if timed_out['value'] else ''}",
        flush=True,
    )
    _print_output_tree(output_dir, prefix=cpfx)
    print(cpfx + "-" * 60, flush=True)

    if timed_out["value"]:
        raise RuntimeError(f"Tierpsy timed out after {timeout_s}s")
    if returncode != 0:
        raise RuntimeError(
            f"Tierpsy exited {returncode}:\n{combined.strip()[-1000:]}"
        )
    return combined, ""



def _drop_stale_tierpsy(cache_dir: Path, plog) -> bool:
    """Remove a cache entry's Tierpsy outputs so Tierpsy really re-runs.

    Tierpsy SKIPS any stage whose output file already exists. So on a stamp
    mismatch the old code logged "re-running Tierpsy", ran it, got nothing done
    because MaskedVideos/ and Results/ were still there, and then stamped the
    entry with the CURRENT fingerprint — certifying old results as new. It
    happened on 27 Aug: eight videos were re-stamped as flat-field-corrected
    while holding tracking from 19 Aug, and the only surviving evidence was
    that the stamp was newer than the HDF5 beside it.

    Only the pipeline's own cache directory is touched; the source video is
    never in it. Returns True if anything was removed.
    """
    removed = False
    for sub in ("MaskedVideos", "Results"):
        d = cache_dir / sub
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            removed = removed or not d.exists()
    if removed:
        plog("[CACHE] Removed the stale Tierpsy output so it is genuinely "
             "recomputed; without this Tierpsy skips and the entry is "
             "re-stamped over old results.")
    return removed


def _drop_stale_avi(avi: Path, flat_field: bool) -> bool:
    """
    Delete a cached AVI that was produced under a different flat-field setting.

    convert_to_avi() skips when the AVI already exists, which is right when the
    AVI is still valid and wrong the moment the correction is switched on or
    off: Tierpsy would faithfully re-run on the previous, differently-processed
    video and the change would appear to do nothing.

    The flat-field code path always caches its field beside the AVI, so the
    presence of that file is the record of how this AVI was made. Returns True
    if the AVI was removed.
    """
    if not avi.exists():
        return False
    try:
        from analysis import flatfield
    except Exception:
        return False

    # First: is it even a finished video? A run that was cancelled or crashed
    # mid-transcode leaves a headerless, index-less AVI behind, and the
    # existence check in convert_to_avi would happily reuse it forever.
    reason = ""
    try:
        flatfield.verify_avi(avi)
    except Exception as exc:
        reason = str(exc)
    if not reason and flatfield.field_path(avi).exists() == bool(flat_field):
        return False
    try:
        avi.unlink()
        return True
    except OSError as exc:
        if reason:
            # Refusing to continue is the point. Returning False here would
            # send us into convert_to_avi, which skips when the file exists —
            # so the run would quietly proceed on the broken video we just
            # identified.
            raise RuntimeError(
                f"{avi.name} is unusable ({reason}) and could not be deleted "
                f"({exc}). Remove its _wormscan_cache folder and re-run.")
        return False

# ---------------------------------------------------------------------------
# Per-video worker (runs on a ThreadPoolExecutor worker thread)
# ---------------------------------------------------------------------------


def _pv_prefix(condition: str, plate: str, timepoint_h=None) -> str:
    """Filename stem for this video's per_video artefacts.

    "<condition>__<plate>" alone is NOT unique in a timecourse: "601 0J" /
    "plate 01" names one video on every imaging day, so five days of sidecars
    and renders wrote over each other and the folder ended up holding whichever
    day finished last, silently and under a name that claimed otherwise. The
    timepoint is part of the identity, so it is part of the name.
    """
    stem = f"{condition}__{plate}"
    if timepoint_h is None:
        return stem
    return f"{stem}__t{float(timepoint_h):g}h"


def _process_one_video_crawling(
    video: Path,
    folder: Path,
    *,
    image: str,
    engine: object,
    timeout_s: int,
    params_template: dict,
    head_angle_prominence: float,
    threshold_s: float,
    min_span_s: float,
    clear_cache: bool,
    flat_field: bool,
    want_tracked: bool,
    want_sidebyside: bool,
    want_path_traces: bool,
    per_video_dir: Path,
    ffmpeg_threads: Optional[int],
    cancel_event: threading.Event,
    timepoint_h: Optional[float] = None,
    report_stage: Optional[Callable[[str], None]] = None,
) -> dict:
    """
    Process a single video end-to-end (probe → transcode → Tierpsy → metrics →
    optional renders) and return a result dict.

    Runs on a worker thread. Writes ONLY to per-video paths. Never touches the
    shared status object or any Tk widget. Log lines are buffered into the
    returned "logbuf" list and flushed contiguously by the collecting thread.
    The instrumented Tierpsy call still streams to the console live (tagged with
    the video stem) so several containers' output remains attributable.
    """
    from analysis.ffmpeg_utils import probe_fps, convert_to_avi
    from analysis.crawling_metrics import compute_crawling_metrics
    import json as _json

    t0 = time.monotonic()
    logbuf: list[str] = []

    def plog(msg: str) -> None:
        logbuf.append(msg)

    # The log is buffered and flushed only when a video finishes, which is
    # right for the log and useless for a progress window: on a 25-minute
    # video it means 25 minutes of nothing. stage() is the live channel — one
    # short phrase, overwritten, never queued.
    def stage(msg: str) -> None:
        if report_stage is not None:
            try:
                report_stage(msg)
            except Exception:                                  # noqa: BLE001
                pass          # a status update is never worth failing a video

    condition, plate = _resolve_video_path(video, folder)
    thread_name = threading.current_thread().name
    plog(f"\n--- {video.name} ({condition}) ---")

    if cancel_event.is_set():
        plog("Skipped (run cancelled before start)")
        return {"video": video, "condition": condition, "plate": plate,
                "worm_rows": [], "status_str": "cancelled",
                "logbuf": logbuf, "elapsed_s": 0.0, "cancelled": True}

    plog(f"START on {thread_name}")

    fps = 0.0
    worm_rows: list[dict] = []
    status_str = "ok"
    hdf5_path: Optional[Path] = None
    cache_dir = _cache_dir_for(video)

    try:
        stage("Probing the video")
        fps = probe_fps(video)
        plog(f"fps: {fps:.3f}")

        avi = cache_dir / (video.stem + ".avi")
        candidate_hdf5 = cache_dir / "Results" / (video.stem + "_featuresN.hdf5")
        cache_fp = _params_fingerprint("crawling", params_template, flat_field)
        stamp_ok, stamp_why = _cache_stamp_check(cache_dir, "crawling", cache_fp)
        cache_hit = (not clear_cache and _hdf5_cache_valid(candidate_hdf5)
                     and stamp_ok)
        if (not clear_cache and not stamp_ok
                and _hdf5_cache_valid(candidate_hdf5)):
            plog(f"[CACHE MISS] A cached result exists but {stamp_why} — "
                 "re-running Tierpsy.")
            _drop_stale_tierpsy(cache_dir, plog)

        if cache_hit:
            hdf5_path = candidate_hdf5
            needs_avi = (want_tracked or want_sidebyside or want_path_traces)
            stage("Reusing cached tracking")
            if avi.exists():
                plog(f"[CACHE HIT] Skipping Tierpsy; AVI present: {video.name}")
            elif needs_avi:
                plog(f"[CACHE HIT] Skipping Tierpsy; converting AVI for rendering: {video.name}")
                stage("Flat-field + transcode")
                cache_dir.mkdir(parents=True, exist_ok=True)
                convert_to_avi(video, avi, threads=ffmpeg_threads,
                           flat_field=flat_field)
                plog(f"AVI ready: {avi}")
            else:
                plog(f"[CACHE HIT] Skipping Tierpsy + AVI: {video.name}")
        else:
            cache_dir.mkdir(parents=True, exist_ok=True)
            if _drop_stale_avi(avi, flat_field):
                plog("[CACHE] Dropped a cached AVI made under a different "
                     "flat-field setting; re-transcoding.")
            stage("Flat-field + transcode"
                  if flat_field else "Transcoding to AVI")
            convert_to_avi(video, avi, threads=ffmpeg_threads,
                           flat_field=flat_field)
            plog(f"AVI ready: {avi}")

            params = copy.deepcopy(params_template)
            if "expected_fps" in params:
                params["expected_fps"] = fps
            for _k in _WORMSCAN_ONLY_KEYS:
                params.pop(_k, None)
            json_file = cache_dir / (video.stem + ".json")
            json_file.write_text(
                json.dumps(params, indent=2), encoding="utf-8"
            )

            stage("Starting Tierpsy")
            stdout, stderr = _run_tierpsy_instrumented(
                avi, json_file,
                image=image,
                output_dir=cache_dir,
                engine=engine,
                timeout_s=timeout_s,
                tag=video.stem,
                on_stage=lambda phrase: stage(f"Tierpsy: {phrase}"),
            )
            plog(f"Tierpsy stdout:\n{stdout}")
            if stderr.strip():
                plog(f"Tierpsy stderr:\n{stderr}")

            results_dir = cache_dir / "Results"
            candidates = list(results_dir.glob(f"{video.stem}_featuresN.hdf5"))
            if not candidates:
                raise FileNotFoundError(
                    f"No _featuresN.hdf5 in {results_dir}"
                )
            hdf5_path = candidates[0]
            _write_cache_stamp(cache_dir, "crawling", cache_fp)

        engine_log: dict = {}
        stage("Linking fragments + measuring")
        worm_rows = compute_crawling_metrics(
            hdf5_path, fps, condition, plate, video.name,
            head_angle_prominence=head_angle_prominence,
            long_threshold_s=threshold_s,
            min_span_s=min_span_s,
            engine_log_out=engine_log,
        )
        plog(f"Grouped worms: {len(worm_rows)}")

        # Surface the shared engine's pre-grouping drop reasons so we
        # can see how many tracks die before the 60s gate vs at it.
        if engine_log:
            _fd = engine_log.get("fragments_dropped_no_skeleton") or 0
            if _fd:
                plog(f"Skeleton floor: dropped {_fd} of "
                     f"{engine_log.get('fragments_in')} fragment(s) under "
                     f"{engine_log.get('min_fragment_skeleton_coverage'):.2f} "
                     f"coverage ({engine_log.get('frames_dropped_no_skeleton')} "
                     "frames) before linking")
            plog(
                f"Linker: fragments={engine_log.get('input_track_count')}"
                f" -> after merge-split={engine_log.get('fragments_after_split')}"
                f" -> tracks={engine_log.get('groups_formed')}"
                f" | links short-gap={engine_log.get('links_made_short_gap')}"
                f" isolated={engine_log.get('links_made_isolated')}"
                f" | refused occupied={engine_log.get('links_refused_occupied')}"
                f" | merge episodes={engine_log.get('merge_episodes')}"
                f" (frames dropped={engine_log.get('merge_frames_dropped')})"
            )
            sidecar = per_video_dir / f"{_pv_prefix(condition, plate, timepoint_h)}_analysis_log.json"
            sidecar.write_text(
                _json.dumps({"video": video.name,
                             "timepoint_h": timepoint_h,
                             **engine_log}, indent=2),
                encoding="utf-8",
            )

        # Map every member Tierpsy id of a filter-passing grouped worm
        # to its stable grouped worm_index, and collect that worm's
        # reversal frames. Members absent from the map were filtered
        # out and the renders mark them faintly (motility-style).
        worm_index_map: dict = {}
        reversal_frames_by_worm: dict[int, list] = {}
        arrow_data_by_worm: dict[int, dict] = {}
        for r in worm_rows:
            if not r.get("passed_filter"):
                continue
            gi = int(r["worm_index"])
            # Frame-windowed, so a fragment split at a collision is drawn under
            # the right track number in each half. Falls back to the whole-
            # fragment form when member_spans is absent.
            spans = r.get("member_spans") or []
            if spans:
                for tid, f0, f1 in spans:
                    worm_index_map.setdefault(int(tid), []).append(
                        (int(f0), int(f1), gi))
            else:
                for mid in str(r.get("member_tierpsy_ids", "")).split(";"):
                    mid = mid.strip()
                    if mid:
                        worm_index_map[int(mid)] = gi
            reversal_frames_by_worm[gi] = r.get("reversal_frames") or []
            # Velocity-arrow overlay payload (dense per-frame centroid/velocity
            # arrays + event frames) — see crawling_metrics renderer-only keys.
            arrow_data_by_worm[gi] = {
                "f0": r.get("arrow_f0"),
                "x": r.get("arrow_x"),
                "y": r.get("arrow_y"),
                "vx": r.get("arrow_vx"),
                "vy": r.get("arrow_vy"),
                "reversal_event_frames": r.get("arrow_reversal_event_frames") or [],
                "turn_event_frames": r.get("arrow_turn_event_frames") or [],
            }
        n_kept = sum(1 for r in worm_rows if r.get("passed_filter"))
        plog(
            f"Tracks passing filter (length >= {min_span_s:.1f}s): "
            f"{n_kept}/{len(worm_rows)}"
        )

        if want_tracked or want_sidebyside or want_path_traces:
            stage("Rendering diagnostic video")
            from analysis.render_video import render_tracked, render_sidebyside
            from analysis.crawling_render import render_path_traces
            skeletons_hdf5 = cache_dir / "Results" / f"{video.stem}_skeletons.hdf5"
            masked_hdf5 = cache_dir / "MaskedVideos" / f"{video.stem}.hdf5"
            prefix = _pv_prefix(condition, plate, timepoint_h)
            if want_tracked and skeletons_hdf5.exists() and avi.exists():
                render_tracked(
                    avi, skeletons_hdf5,
                    per_video_dir / f"{prefix}_tracked.mp4", fps,
                    worm_index_map=worm_index_map,
                    arrow_data=arrow_data_by_worm,
                )
            if want_sidebyside and masked_hdf5.exists() and skeletons_hdf5.exists() and avi.exists():
                render_sidebyside(
                    avi, masked_hdf5, skeletons_hdf5,
                    per_video_dir / f"{prefix}_sidebyside.mp4", fps,
                    worm_index_map=worm_index_map,
                )
            if want_path_traces and skeletons_hdf5.exists() and avi.exists():
                render_path_traces(
                    avi, skeletons_hdf5,
                    per_video_dir / f"{prefix}_path_traces.mp4", fps,
                    worm_index_map=worm_index_map,
                    reversal_frames=reversal_frames_by_worm,
                    arrow_data=arrow_data_by_worm,
                )

    except Exception as exc:
        status_str = str(exc)[:200]
        plog(f"ERROR: {exc}")
        log.exception("Error processing %s", video.name)

    elapsed = time.monotonic() - t0
    plog(f"FINISH on {thread_name} in {elapsed:.1f}s "
         f"({'ok' if status_str == 'ok' else 'FAILED'})")
    # Clear this video's phase whatever happened above, including the error
    # path: a failed video that leaves "Tierpsy: Calculating skeletons" in the
    # dialog makes a stalled run look like a working one.
    stage("")

    return {
        "video": video,
        "condition": condition,
        "plate": plate,
        "worm_rows": worm_rows,
        "status_str": status_str,
        "logbuf": logbuf,
        "elapsed_s": elapsed,
        "cancelled": False,
    }


# ---------------------------------------------------------------------------
# Shared status snapshot (read-only, passed to UI thread)
# ---------------------------------------------------------------------------

@dataclass
class CrawlingSnapshot:
    color: str
    label: str
    running: bool
    current_index: int
    total: int
    current_basename: str
    current_stage: str
    # What the pool is doing RIGHT NOW, e.g. "3x Calculating skeletons ·
    # 2x Compressing video · 1x Transcoding". Empty when nothing is running.
    # Separate from current_stage, which counts finished videos: a count tells
    # you how far along a 20-hour run is and says nothing about whether it is
    # still moving, which is the question anyone actually watching it has.
    stage_detail: str = ""


# ---------------------------------------------------------------------------
# Shared status object
# ---------------------------------------------------------------------------

class CrawlingStatus:
    """
    Shared state between the crawling worker thread and the UI thread.

    Write contract — worker thread ONLY: call update() or mark_completed().
    Read contract  — UI thread ONLY: call snapshot() or pop_completed().
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._color = "gray"
        self._label = "Idle"
        self._running = False
        self._current_index = 0
        self._total = 0
        self._current_basename = ""
        self._current_stage = ""
        # Live per-video phases. Its own lock, so worker threads never contend
        # with this object's — see analysis/stage_tracker.py.
        self.stages = StageTracker()
        self._completed_result: Optional[dict] = None

    def update(
        self,
        *,
        color: str,
        label: str,
        running: bool = False,
        current_index: int = 0,
        total: int = 0,
        current_basename: str = "",
        current_stage: str = "",
    ) -> None:
        with self._lock:
            self._color = color
            self._label = label
            self._running = running
            self._current_index = current_index
            self._total = total
            self._current_basename = current_basename
            self._current_stage = current_stage

    def mark_failed(self, error: str, out_dir=None) -> None:
        """A run that died, surfaced through the SAME channel as a success.

        Without this a crash set the dot red, wrote a short label and stopped.
        From the outside that is indistinguishable from a run that quietly did
        nothing, and the UI's failure notice (which has existed all along)
        never fired because nothing ever put a result in the queue for it.
        Development already did this; the other three did not.
        """
        with self._lock:
            self._color = "red"
            self._label = "Analysis failed — see log"
            self._running = False
            self._current_stage = ""
            self.stages.clear()
            self._completed_result = {
                "failed": True,
                "error": error,
                "n_ok": 0,
                "n_fail": 0,
                "out_dir": out_dir,
                "note": "",
            }

    def mark_completed(self, n_ok: int, n_fail: int, out_dir: Path) -> None:
        with self._lock:
            self._color = "green"
            self._label = f"Analysis complete: {n_ok}/{n_ok + n_fail} videos"
            self._running = False
            self._current_stage = ""
            self.stages.clear()
            self._completed_result = {
                "failed": False,
                "n_ok": n_ok,
                "n_fail": n_fail,
                "out_dir": out_dir,
            }

    def snapshot(self) -> CrawlingSnapshot:
        with self._lock:
            return CrawlingSnapshot(
                color=self._color,
                label=self._label,
                running=self._running,
                current_index=self._current_index,
                total=self._total,
                current_basename=self._current_basename,
                current_stage=self._current_stage,
                stage_detail=self.stages.summary(),
            )

    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def pop_completed(self) -> Optional[dict]:
        """Return and clear the completion result, or None if not yet complete."""
        with self._lock:
            result = self._completed_result
            self._completed_result = None
            return result


# ---------------------------------------------------------------------------
# Crawling agent
# ---------------------------------------------------------------------------

class CrawlingAgent(threading.Thread):
    """Background thread for crawling analysis. Idle until start_analysis() is called."""

    def __init__(self, settings: object, status: CrawlingStatus) -> None:
        super().__init__(daemon=True, name="CrawlingAgent")
        self._lock = threading.Lock()
        self._settings = settings
        self.status = status
        self._stop = threading.Event()
        self._cancel = threading.Event()
        self._wake = threading.Event()
        self._plans: Optional[list] = None
        self._threshold_s: float = 5.0
        self._min_span_s: float = 10.0
        self._force_reanalyze: bool = False
        self._clear_cache: bool = False
        self._want_tracked: bool = False
        self._want_sidebyside: bool = False
        self._want_path_traces: bool = False
        self._params_template: dict = {}
        self._load_params()

    def _load_params(self) -> None:
        params_path = paths.crawling_params()
        try:
            self._params_template = json.loads(params_path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.error("Failed to load crawling_params.json: %s", exc)

    def update_settings(self, settings: object) -> None:
        with self._lock:
            self._settings = settings

    def _get_settings(self) -> object:
        with self._lock:
            return self._settings

    def stop(self) -> None:
        self._stop.set()
        self._cancel.set()
        self._wake.set()

    def cancel(self) -> None:
        """UI thread: cancel the current run. Does not stop the thread."""
        self._cancel.set()

    def start_analysis(
        self,
        plans: list,
        threshold_s: float = 5.0,
        clear_cache: bool = False,
        want_tracked: bool = False,
        want_sidebyside: bool = False,
        want_path_traces: bool = False,
        min_span_s: float = 10.0,
        force_reanalyze: bool = False,
    ) -> None:
        """UI thread: trigger a run over one or more folders.

        ``plans`` is a list of survival.FolderPlan (folder + resolved
        timepoint), already checked for errors by the caller. A bare Path is
        accepted for convenience and treated as a single folder at 0 h.
        """
        if not isinstance(plans, (list, tuple)):
            from survival import FolderPlan
            plans = [FolderPlan(folder=Path(plans), hours=0.0,
                                method="single folder", detail="single folder")]
        with self._lock:
            self._plans = list(plans)
            self._threshold_s = threshold_s
            self._min_span_s = min_span_s
            self._clear_cache = clear_cache
            self._want_tracked = want_tracked
            self._want_sidebyside = want_sidebyside
            self._want_path_traces = want_path_traces
            self._force_reanalyze = force_reanalyze
        self.status.update(
            running=True,
            total=0,
            current_basename="",
            current_stage="Starting…",
            color="yellow",
            label="Starting analysis…",
        )
        self._cancel.clear()
        self._wake.set()

    def run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait()
            self._wake.clear()
            if self._stop.is_set():
                break
            with self._lock:
                plans = self._plans
                threshold_s = self._threshold_s
                min_span_s = self._min_span_s
                clear_cache = self._clear_cache
                want_tracked = self._want_tracked
                want_sidebyside = self._want_sidebyside
                want_path_traces = self._want_path_traces
                force_reanalyze = self._force_reanalyze
                self._plans = None
            if plans:
                self._cancel.clear()
                try:
                    self._run_analysis(
                        plans, threshold_s, clear_cache,
                        want_tracked, want_sidebyside, want_path_traces,
                        min_span_s, force_reanalyze,
                    )
                except Exception as exc:
                    log.exception("CrawlingAgent crashed")
                    # Report through the SAME channel as a success, and put the
                    # traceback in the run's own log.txt so that "see log"
                    # points at something that actually explains it.
                    out_dir = getattr(self, "_last_out_dir", None)
                    if out_dir is not None:
                        try:
                            with open(Path(out_dir) / "log.txt", "a",
                                      encoding="utf-8") as fh:
                                fh.write("\n" + "=" * 60
                                         + "\nANALYSIS CRASHED\n"
                                         + traceback.format_exc() + "\n")
                        except Exception:                     # noqa: BLE001
                            pass
                    self.status.mark_failed(
                        f"{type(exc).__name__}: {exc}", out_dir)

    def _run_analysis(
        self,
        plans: list,
        threshold_s: float,
        clear_cache: bool,
        want_tracked: bool = False,
        want_sidebyside: bool = False,
        want_path_traces: bool = False,
        min_span_s: float = 30.0,
        force_reanalyze: bool = False,
    ) -> None:
        """Analyse one or more folders as one run.

        ``plans`` is a list of survival.FolderPlan — each a folder plus its
        resolved timepoint in hours. One folder is the ordinary case and behaves
        as it always did; several make a timecourse, and every per-worm row is
        stamped with its folder's timepoint so the aggregation and the figures
        can use time as an axis.

        Folders already analysed under identical settings are reused rather than
        re-analysed (analysis.run_cache), which is the whole point of being able
        to add a day and re-run.
        """
        from analysis.ffmpeg_utils import find_videos
        from analysis.crawling_metrics import (
            aggregate_per_condition, PER_WORM_COLS, reuse_post_settings,
        )
        from analysis import run_cache
        from analysis.concurrency import resolve_workers, ffmpeg_threads_per_worker
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import pandas as pd

        s = self._get_settings()
        t_start = time.monotonic()
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        # Everything lands under the FIRST folder — with several folders
        # there is no neutral place to put it (same rule as Development).
        folder = Path(plans[0].folder)
        out_dir = folder / f"{_ANALYSIS_PREFIX}_{timestamp}"
        out_dir.mkdir(parents=True, exist_ok=True)
        # the crash handler in run() needs this to point the
        # user at the folder and to append the traceback
        self._last_out_dir = out_dir
        per_video_dir = out_dir / "per_video"
        per_video_dir.mkdir(exist_ok=True)
        log_path = out_dir / "log.txt"

        with open(log_path, "w", encoding="utf-8") as lf:

            def write_log(msg: str) -> None:
                """Append one line to log.txt, and never raise.

                The workbook and the report layer are written AFTER this
                ``with`` block has closed ``lf`` (see the end of this method),
                so a naive lf.write() there raises ValueError on a closed file.
                That is not a hypothetical: it aborted a real run mid-report and
                cost it its summary CSV and overview figure. Reopen in append
                mode when the handle has gone, and swallow anything that still
                fails, because losing an analysis to a log line is never the
                right trade.
                """
                log.info("[crawling] %s", msg)
                try:
                    if lf.closed:
                        with open(log_path, "a", encoding="utf-8") as fh:
                            fh.write(msg + "\n")
                    else:
                        lf.write(msg + "\n")
                        lf.flush()
                except Exception:                             # noqa: BLE001
                    pass

            videos_by_folder = {Path(pl.folder): find_videos(Path(pl.folder))
                                for pl in plans}
            total = sum(len(v) for v in videos_by_folder.values())
            multi = len(plans) > 1

            write_log(f"Run: {timestamp}")
            if multi:
                write_log(f"Folders: {len(plans)} (timecourse)")
                for pl in plans:
                    write_log(f"  {pl.hours:g} h  {pl.folder}  "
                              f"({len(videos_by_folder[Path(pl.folder)])} videos)"
                              f"  [{pl.detail}]")
            else:
                write_log(f"Folder: {folder}")
            write_log(f"Videos found: {total}")
            write_log(f"Min track length: {min_span_s:.1f}s")

            flat_field = bool(getattr(s, "flat_field_correction", True))
            want_renders = bool(want_tracked or want_sidebyside or want_path_traces)
            digest = run_cache.settings_digest(
                "crawling", self._params_template, flat_field,
                reuse_post_settings(min_span_s, threshold_s))
            reuse = run_cache.plan_reuse(
                list(videos_by_folder), videos_by_folder, digest,
                pipeline="crawling", prefix=_ANALYSIS_PREFIX,
                want_renders=want_renders,
                force=bool(force_reanalyze or clear_cache),
                write_log=write_log)
            write_log(f"Reuse: {reuse.n_reused}/{reuse.n_folders} folder(s) "
                      f"already analysed with these settings")
            for line in reuse.lines():
                write_log(line)

            self.status.update(
                color="yellow",
                label="Discovering videos…",
                running=True,
                current_index=0,
                total=total,
                current_basename="",
                current_stage="Discovering videos…",
            )

            from analysis.docker_utils import resolve_engine, resolve_image
            engine = resolve_engine(s) or engine_mod.Engine(
                command=getattr(s, "docker_command", "docker"),
                kind="docker", version="(not detected)")
            image = resolve_image(s)
            write_log(f"Container engine: {engine}")
            write_log(f"Tierpsy image: {image}")
            write_log(paths.describe())
            head_angle_prominence = float(
                self._params_template.get("head_angle_prominence", 0.30))
            all_worm_rows: list[dict] = []
            n_ok = 0
            n_fail = 0
            folder_meta: list[dict] = []

            workers, cpus, mem_gb = resolve_workers(
                getattr(s, "concurrent_videos", "auto"), engine
            )
            ff_threads = ffmpeg_threads_per_worker(workers)
            write_log(
                f"Concurrency: {workers} worker(s) "
                f"({engine.kind} sees {cpus} cpu, {mem_gb:.1f} GB; setting="
                f"{getattr(s, 'concurrent_videos', 'auto')}); "
                f"ffmpeg threads/worker={ff_threads}"
            )

            done = 0

            def _publish_progress(last_name: str) -> None:
                self.status.update(
                    color="yellow",
                    label=(f"{last_name} ({done}/{total})" if last_name
                           else "Analysing…"),
                    running=True,
                    current_index=done,
                    total=total,
                    current_basename=last_name,
                    current_stage=f"{done}/{total} done",
                )

            _publish_progress("")

            for pl in plans:
                if self._cancel.is_set() or self._stop.is_set():
                    write_log("Run cancelled before the remaining folders")
                    break
                pfolder = Path(pl.folder)
                videos = videos_by_folder[pfolder]
                tp = float(pl.hours) if pl.hours is not None else None
                if multi:
                    write_log(f"\n{'=' * 60}\nFOLDER {pfolder.name} "
                              f"({tp:g} h) — {len(videos)} video(s)\n{'=' * 60}")

                cache = reuse.caches.get(pfolder)
                if cache is not None and cache.hit:
                    rows = run_cache.read_rows(cache.rows_csv, pfolder)
                    for r in rows:
                        r["timepoint_h"] = tp
                        r["source_folder"] = str(pfolder)
                    all_worm_rows.extend(rows)
                    n_ok += len(videos)
                    done += len(videos)
                    _publish_progress(pfolder.name)
                    write_log(f"REUSED {len(rows)} track(s) from "
                              f"{cache.source_dir.name if cache.source_dir else '?'} "
                              f"— folder not re-analysed")
                    folder_meta.append({
                        "folder": str(pfolder), "timepoint_h": tp,
                        "videos": run_cache.video_fingerprints(videos),
                        "n_rows": len(rows), "n_videos_ok": len(videos),
                        "n_videos_failed": 0})
                    continue

                if clear_cache:
                    write_log("Clearing cache folders…")
                    for cache_dir in pfolder.rglob(_CACHE_DIR):
                        if cache_dir.is_dir():
                            shutil.rmtree(cache_dir, ignore_errors=True)
                            write_log(f"  Removed: {cache_dir}")

                f_ok = f_fail = 0
                f_rows: list[dict] = []
                if videos:
                    with ThreadPoolExecutor(
                        max_workers=workers, thread_name_prefix="crawling"
                    ) as ex:
                        futures = {}
                        for video in videos:
                            if self._cancel.is_set() or self._stop.is_set():
                                write_log("Run cancelled before submitting "
                                          "remaining videos")
                                break
                            fut = ex.submit(
                                _process_one_video_crawling,
                                video, pfolder,
                                image=image,
                                engine=engine,
                                timeout_s=_TIERPSY_TIMEOUT_S,
                                params_template=self._params_template,
                                head_angle_prominence=head_angle_prominence,
                                threshold_s=threshold_s,
                                min_span_s=min_span_s,
                                clear_cache=clear_cache,
                                flat_field=flat_field,
                                want_tracked=want_tracked,
                                want_sidebyside=want_sidebyside,
                                want_path_traces=want_path_traces,
                                per_video_dir=per_video_dir,
                                ffmpeg_threads=ff_threads,
                                cancel_event=self._cancel,
                                timepoint_h=tp,
                                report_stage=self.status.stages.reporter(video.name),
                            )
                            futures[fut] = video

                        # Collect on the agent thread: flush each video's
                        # buffered log block contiguously (in completion order,
                        # for liveness) and advance the bar as each finishes.
                        # Data rows are NOT accumulated here — see the ordered
                        # pass below.
                        results_by_video: dict[Path, dict] = {}
                        for fut in as_completed(futures):
                            video = futures[fut]
                            try:
                                result = fut.result()
                            except Exception as exc:
                                # One video's failure must not abort the batch.
                                write_log(f"\n--- {video.name} ---")
                                write_log(f"ERROR (worker crashed): {exc}")
                                log.exception("Worker crashed for %s", video.name)
                                results_by_video[video] = {"crashed": True}
                                done += 1
                                _publish_progress(video.name)
                                continue

                            for line in result["logbuf"]:
                                write_log(line)
                            results_by_video[video] = result
                            done += 1
                            _publish_progress(result["video"].name)

                        # Accumulate in the original discovery order so the
                        # outputs (incl. unsorted per_worm sheet row order) are
                        # byte-identical to a serial run.
                        for video in videos:
                            result = results_by_video.get(video)
                            if result is None:  # never submitted (cancel)
                                continue
                            if result.get("crashed"):
                                f_fail += 1
                                continue
                            if result.get("cancelled"):
                                continue
                            f_rows.extend(result["worm_rows"])
                            if result["status_str"] == "ok":
                                f_ok += 1
                            else:
                                f_fail += 1

                for r in f_rows:
                    r["timepoint_h"] = tp
                    r["source_folder"] = str(pfolder)
                all_worm_rows.extend(f_rows)
                n_ok += f_ok
                n_fail += f_fail
                folder_meta.append({
                    "folder": str(pfolder), "timepoint_h": tp,
                    "videos": run_cache.video_fingerprints(videos),
                    "n_rows": len(f_rows), "n_videos_ok": f_ok,
                    "n_videos_failed": f_fail})

            # The reusable artifact for the next run, written before the
            # workbook so a failure in the report layer cannot cost it.
            run_cache.write_rows(out_dir / run_cache.ROWS_NAME, all_worm_rows,
                                 list(PER_WORM_COLS), write_log)
            run_cache.write_manifest(
                out_dir, pipeline="crawling", digest=digest,
                folders=folder_meta, has_renders=want_renders,
                write_log=write_log)
            timepoints = sorted({r.get("timepoint_h") for r in all_worm_rows
                                 if r.get("timepoint_h") is not None})
            if len(timepoints) > 1:
                write_log(f"Timecourse: {len(timepoints)} timepoints "
                          f"({', '.join(f'{t:g} h' for t in timepoints)})")

        # ---- Build output: per_worm + per_condition sheets, CSV mirrors per_condition ----
        # The sheet carries the same columns as per_worm_rows.csv, timecourse
        # ones included. It used to project onto PER_WORM_COLS alone, so
        # source_folder and timepoint_h — the two columns that say WHICH DAY a
        # worm came from — existed in the CSV and were absent from the workbook
        # everyone actually opens. Mirrors run_cache.write_rows deliberately:
        # the two views of one table must not differ in their columns.
        sheet_cols = list(PER_WORM_COLS)
        for extra in ("source_folder", "timepoint_h"):
            if any(extra in r for r in all_worm_rows) and extra not in sheet_cols:
                sheet_cols.append(extra)
        per_worm_df = (pd.DataFrame(all_worm_rows, columns=sheet_cols).round(4)
                       if all_worm_rows
                       else pd.DataFrame(columns=sheet_cols))
        by_tp = len(timepoints) > 1
        per_condition_rows = aggregate_per_condition(
            all_worm_rows, min_span_s=min_span_s, by_timepoint=by_tp)
        per_condition_df = (pd.DataFrame(per_condition_rows).round(4)
                            if per_condition_rows
                            else pd.DataFrame(columns=["condition", "n_worms"]))

        with pd.ExcelWriter(out_dir / "crawling_results.xlsx", engine="openpyxl") as xw:
            per_worm_df.to_excel(xw, sheet_name="per_worm", index=False)
            # per_condition pools WORMS across plates — kept exactly as it was,
            # so older numbers stay reproducible. condition_summary, added by
            # the report layer below, aggregates PLATES instead and is what the
            # figures and the explorer use. The workbook README explains which
            # answers which question.
            per_condition_df.to_excel(xw, sheet_name="per_condition", index=False)
            try:
                import assay_reports
                from analysis.crawling_metrics import (
                    MIN_FRAGMENT_SKELETON_COVERAGE)
                assay_reports.crawling_report(
                    xw.book, all_worm_rows, out_dir, write_log,
                    min_span_s=min_span_s, by_timepoint=by_tp,
                    min_fragment_coverage=MIN_FRAGMENT_SKELETON_COVERAGE)
            except Exception as exc:                              # noqa: BLE001
                log.warning("crawling: report layer failed", exc_info=True)
                write_log(f"WARNING: the plate/condition sheets, the figures and "
                          f"the explorer could not be written ({exc}). Every "
                          "per-worm output of this run is unaffected.")

        per_condition_df.to_csv(out_dir / "crawling_summary.csv", index=False)

        # One multi-panel overview figure (box/bar per metric by condition).
        try:
            from analysis.crawling_plots import make_crawling_overview_png
            make_crawling_overview_png(
                all_worm_rows, out_dir / "overview.png",
                min_span_s=min_span_s,
                elapsed_s=time.monotonic() - t_start,
            )
        except Exception:
            log.warning("crawling: overview figure failed", exc_info=True)

        log.info(
            "Crawling analysis complete: %d ok, %d failed. Results: %s",
            n_ok, n_fail, out_dir,
        )
        self.status.mark_completed(n_ok, n_fail, out_dir)
