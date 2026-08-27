"""
Motility analysis agent. Thread boundary mirrors SyncAgent/SyncStatus.

Write contract — worker thread ONLY: call status.update() / status.mark_completed()
Read contract  — UI thread ONLY: call status.snapshot() / status.pop_completed()

Never touch Tk widgets from this thread.
"""
import copy
import hashlib
import json
import logging
import shutil
import traceback
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import paths
from analysis.stage_tracker import StageTracker

log = logging.getLogger(__name__)

_ANALYSIS_PREFIX = "_analysis"
_CACHE_DIR = "_wormscan_cache"

# Keys in motility_params.json that are consumed by our post-Tierpsy code and
# must never be written into the per-video JSON that Tierpsy validates.
_WORMSCAN_ONLY_KEYS: frozenset[str] = frozenset({"head_angle_prominence"})


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


def _safe_sheet_name(name: str, seen: dict[str, int]) -> str:
    """Return a valid Excel sheet name (≤31 chars), de-duplicated via seen."""
    import re
    safe = re.sub(r'[\[\]:*?/\\]', '_', name)[:31]
    count = seen.get(safe, 0) + 1
    seen[safe] = count
    if count == 1:
        return safe
    suffix = f"_{count}"
    return safe[:31 - len(suffix)] + suffix



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

    "<condition>__<plate>" alone is NOT unique in a timecourse: the same
    condition and plate name recur on every imaging day, so the sidecars, the
    summary PNGs and the renders overwrote each other and the folder kept
    whichever day finished last, under a name that claimed otherwise.
    """
    stem = f"{condition}__{plate}"
    if timepoint_h is None:
        return stem
    return f"{stem}__t{float(timepoint_h):g}h"


def _process_one_video_motility(
    video: Path,
    folder: Path,
    *,
    image: str,
    engine: object,
    timeout_s: int,
    params_template: dict,
    head_angle_prominence: float,
    threshold_s: float,
    clear_cache: bool,
    flat_field: bool,
    want_tracked: bool,
    want_curvature: bool,
    want_sidebyside: bool,
    want_per_worm_traces: bool,
    per_video_dir: Path,
    ffmpeg_threads: Optional[int],
    cancel_event: threading.Event,
    timepoint_h: Optional[float] = None,
    report_stage: Optional[Callable[[str], None]] = None,
) -> dict:
    """
    Process a single video end-to-end (probe → transcode → Tierpsy → read →
    plots → optional renders) and return a result dict.

    Runs on a worker thread. Writes ONLY to per-video paths (its own cache dir
    and per_video_dir files keyed by condition/plate). Never touches the shared
    status object or any Tk widget. Log lines are buffered into the returned
    "logbuf" list and flushed contiguously by the collecting thread.
    """
    from analysis.ffmpeg_utils import probe_fps, probe_duration, convert_to_avi
    from analysis.docker_utils import run_tierpsy
    from analysis.analysis_csv import read_fragments, build_summary_row
    from analysis.plots import make_video_summary_png
    import json as _json

    t0 = time.monotonic()
    logbuf: list[str] = []

    def plog(msg: str) -> None:
        logbuf.append(msg)

    # The live channel, distinct from the log: the log is buffered and flushed
    # when the video finishes, which on a 20-minute video shows nothing at all
    # while it runs. See analysis/stage_tracker.py.
    def stage(msg: str) -> None:
        if report_stage is not None:
            try:
                report_stage(msg)
            except Exception:                                      # noqa: BLE001
                pass

    condition, plate = _resolve_video_path(video, folder)
    thread_name = threading.current_thread().name
    plog(f"\n--- {video.name} ({condition}) ---")

    if cancel_event.is_set():
        plog("Skipped (run cancelled before start)")
        return {"video": video, "condition": condition, "plate": plate,
                "fragment_rows": [], "summary_row": None, "status_str": "cancelled",
                "logbuf": logbuf, "elapsed_s": 0.0, "cancelled": True}

    plog(f"START on {thread_name}")

    fps = 0.0
    video_duration_s = 0.0
    fragment_rows: list[dict] = []
    status_str = "ok"
    hdf5_path: Optional[Path] = None
    cache_dir = _cache_dir_for(video)

    try:
        stage("Probing the video")
        fps = probe_fps(video)
        plog(f"fps: {fps:.3f}")

        video_duration_s = probe_duration(video)

        avi = cache_dir / (video.stem + ".avi")
        candidate_hdf5 = cache_dir / "Results" / (video.stem + "_featuresN.hdf5")
        cache_fp = _params_fingerprint("motility", params_template, flat_field)
        stamp_ok, stamp_why = _cache_stamp_check(cache_dir, "motility", cache_fp)
        cache_hit = (not clear_cache and _hdf5_cache_valid(candidate_hdf5)
                     and stamp_ok)
        if (not clear_cache and not stamp_ok
                and _hdf5_cache_valid(candidate_hdf5)):
            plog(f"[CACHE MISS] A cached result exists but {stamp_why} — "
                 "re-running Tierpsy.")

        if cache_hit:
            hdf5_path = candidate_hdf5
            needs_avi = (want_tracked or want_curvature
                         or want_sidebyside or want_per_worm_traces)
            if avi.exists():
                plog(f"[CACHE HIT] Skipping Tierpsy; AVI present: {video.name}")
            elif needs_avi:
                plog(f"[CACHE HIT] Skipping Tierpsy; converting AVI for rendering: {video.name}")
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
            stage("Flat-field + transcode" if flat_field
                  else "Transcoding to AVI")
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
            stdout, stderr = run_tierpsy(
                avi, json_file,
                image=image,
                engine=engine,
                timeout_s=timeout_s,
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
            _write_cache_stamp(cache_dir, "motility", cache_fp)

        stage("Grouping fragments + counting bends")
        fragment_rows, analysis_log = read_fragments(
            hdf5_path, fps, condition, plate,
            long_threshold_s=threshold_s,
            head_angle_prominence=head_angle_prominence,
        )
        n_long = sum(1 for r in fragment_rows if r["is_long"])
        plog(
            f"Worms: {len(fragment_rows)} total, {n_long} long"
            f" (threshold {threshold_s}s) | "
            f"groups formed: {analysis_log['groups_formed']['total']}"
            f" (curl={analysis_log['groups_formed']['curl']},"
            f" collision={analysis_log['groups_formed']['collision']})"
            f" | dropped: {analysis_log['worms_dropped']['total']}"
        )

        log_sidecar = per_video_dir / f"{_pv_prefix(condition, plate, timepoint_h)}_analysis_log.json"
        log_sidecar.write_text(
            _json.dumps({"video": video.name, **analysis_log}, indent=2),
            encoding="utf-8",
        )

        if fragment_rows and hdf5_path:
            plot_path = per_video_dir / f"{_pv_prefix(condition, plate, timepoint_h)}.png"
            make_video_summary_png(fragment_rows, hdf5_path, fps, plot_path,
                                   head_angle_prominence)

        if want_tracked or want_curvature or want_sidebyside or want_per_worm_traces:
            from analysis.render_video import (
                render_tracked, render_curvature, render_sidebyside,
                render_per_worm_trace,
            )
            skeletons_hdf5 = cache_dir / "Results" / f"{video.stem}_skeletons.hdf5"
            masked_hdf5 = cache_dir / "MaskedVideos" / f"{video.stem}.hdf5"
            prefix = _pv_prefix(condition, plate, timepoint_h)

            # Map every worm_index_joined fragment of a kept worm to
            # its stable worm_index so the renders label/colour by
            # worm_index; fragments absent from the map were filtered
            # out and the renders mark them faintly (Phase 3).
            worm_index_map: dict[int, int] = {}
            for r in fragment_rows:
                wi = r.get("worm_index")
                if wi is None:
                    continue
                members = r.get("member_tierpsy_ids") or [r.get("repr_tierpsy_id")]
                for tid in members:
                    if tid is not None:
                        worm_index_map[int(tid)] = int(wi)

            if want_tracked and skeletons_hdf5.exists() and avi.exists():
                render_tracked(
                    avi, skeletons_hdf5,
                    per_video_dir / f"{prefix}_tracked.mp4", fps,
                    worm_index_map=worm_index_map,
                )
            if want_curvature and skeletons_hdf5.exists() and hdf5_path and avi.exists():
                render_curvature(
                    avi, skeletons_hdf5, hdf5_path,
                    per_video_dir / f"{prefix}_curvature.mp4", fps,
                )
            if want_sidebyside and masked_hdf5.exists() and skeletons_hdf5.exists() and avi.exists():
                render_sidebyside(
                    avi, masked_hdf5, skeletons_hdf5,
                    per_video_dir / f"{prefix}_sidebyside.mp4", fps,
                    worm_index_map=worm_index_map,
                )
            if (want_per_worm_traces and hdf5_path
                    and skeletons_hdf5.exists() and masked_hdf5.exists()):
                from analysis.plots import make_per_worm_trace_png
                long_rows = [r for r in fragment_rows
                             if r.get("is_long")]
                traces_dir = per_video_dir / f"{prefix}_traces"
                traces_dir.mkdir(exist_ok=True)
                for worm_row in long_rows:
                    wi = worm_row.get("repr_tierpsy_id", worm_row["worm_index"])
                    member_ids = worm_row.get("member_tierpsy_ids", [wi])
                    make_per_worm_trace_png(
                        wi, member_ids, hdf5_path, fps, prefix,
                        traces_dir / f"worm_{wi}.png",
                        worm_row["bpm"],
                        worm_row.get("coverage_pct", 0.0),
                        head_angle_prominence,
                    )
                    render_per_worm_trace(
                        masked_hdf5, skeletons_hdf5, hdf5_path,
                        member_ids, wi, fps,
                        traces_dir / f"worm_{wi}.mp4",
                        head_angle_prominence,
                    )

    except Exception as exc:
        status_str = str(exc)[:200]
        plog(f"ERROR: {exc}")
        log.exception("Error processing %s", video.name)

    # Clear this video's phase whatever happened above, error path included: a
    # failed video that leaves a phase behind makes a stalled run look alive.
    stage("")
    elapsed = time.monotonic() - t0
    plog(f"FINISH on {thread_name} in {elapsed:.1f}s "
         f"({'ok' if status_str == 'ok' else 'FAILED'})")

    summary_row = build_summary_row(
        fragment_rows, condition, plate, fps, video_duration_s, status_str,
    )
    return {
        "video": video,
        "condition": condition,
        "plate": plate,
        "fragment_rows": fragment_rows,
        "summary_row": summary_row,
        "status_str": status_str,
        "logbuf": logbuf,
        "elapsed_s": elapsed,
        "cancelled": False,
    }


# ---------------------------------------------------------------------------
# Shared status snapshot (read-only, passed to UI thread)
# ---------------------------------------------------------------------------

@dataclass
class MotilitySnapshot:
    color: str
    label: str
    running: bool
    current_index: int
    total: int
    current_basename: str
    current_stage: str
    # What the pool is doing right now, phases grouped and counted. Empty for
    # a run that reports none; see analysis/stage_tracker.py.
    stage_detail: str = ""


# ---------------------------------------------------------------------------
# Shared status object
# ---------------------------------------------------------------------------

class MotilityStatus:
    """
    Shared state between the motility worker thread and the UI thread.

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
        # Live per-video phases; its own lock (analysis/stage_tracker.py).
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

    def snapshot(self) -> MotilitySnapshot:
        with self._lock:
            return MotilitySnapshot(
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
# Motility agent
# ---------------------------------------------------------------------------

class MotilityAgent(threading.Thread):
    """Background thread for motility analysis. Idle until start_analysis() is called."""

    def __init__(self, settings: object, status: MotilityStatus) -> None:
        super().__init__(daemon=True, name="MotilityAgent")
        self._lock = threading.Lock()
        self._settings = settings
        self.status = status
        self._stop = threading.Event()
        self._cancel = threading.Event()
        self._wake = threading.Event()
        self._plans: Optional[list] = None
        self._threshold_s: float = 5.0
        self._force_reanalyze: bool = False
        self._clear_cache: bool = False
        self._want_tracked: bool = False
        self._want_curvature: bool = False
        self._want_sidebyside: bool = False
        self._want_per_worm_traces: bool = False
        self._params_template: dict = {}
        self._load_params()

    def _load_params(self) -> None:
        params_path = paths.motility_params()
        try:
            self._params_template = json.loads(params_path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.error("Failed to load motility_params.json: %s", exc)

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
        want_curvature: bool = False,
        want_sidebyside: bool = False,
        want_per_worm_traces: bool = False,
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
            self._clear_cache = clear_cache
            self._want_tracked = want_tracked
            self._want_curvature = want_curvature
            self._want_sidebyside = want_sidebyside
            self._want_per_worm_traces = want_per_worm_traces
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
                clear_cache = self._clear_cache
                want_tracked = self._want_tracked
                want_curvature = self._want_curvature
                want_sidebyside = self._want_sidebyside
                want_per_worm_traces = self._want_per_worm_traces
                force_reanalyze = self._force_reanalyze
                self._plans = None
            if plans:
                self._cancel.clear()
                try:
                    self._run_analysis(
                        plans, threshold_s, clear_cache,
                        want_tracked, want_curvature, want_sidebyside,
                        want_per_worm_traces, force_reanalyze,
                    )
                except Exception as exc:
                    log.exception("MotilityAgent crashed")
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
        want_curvature: bool = False,
        want_sidebyside: bool = False,
        want_per_worm_traces: bool = False,
        force_reanalyze: bool = False,
    ) -> None:
        """Analyse one or more folders as one run.

        ``plans`` is a list of survival.FolderPlan — folder plus resolved
        timepoint in hours. One folder behaves exactly as before; several make
        a timecourse, every worm row is stamped with its folder's timepoint,
        and folders already analysed under identical settings are reused
        (analysis.run_cache) instead of re-analysed.
        """
        from analysis.ffmpeg_utils import find_videos
        from analysis.analysis_csv import build_summary_row, reuse_post_settings
        from analysis import run_cache
        from analysis.plots import make_overview_png
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
                log.info("[motility] %s", msg)
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

            flat_field = bool(getattr(s, "flat_field_correction", True))
            want_renders = bool(want_tracked or want_curvature
                                or want_sidebyside or want_per_worm_traces)
            digest = run_cache.settings_digest(
                "motility", self._params_template, flat_field,
                reuse_post_settings(threshold_s))
            reuse = run_cache.plan_reuse(
                list(videos_by_folder), videos_by_folder, digest,
                pipeline="motility", prefix=_ANALYSIS_PREFIX,
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
            all_fragment_rows: list[dict] = []
            summary_rows: list[dict] = []
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
                    all_fragment_rows.extend(rows)
                    done += len(videos)
                    _publish_progress(pfolder.name)
                    write_log(f"REUSED {len(rows)} worm row(s) from "
                              f"{cache.source_dir.name if cache.source_dir else '?'} "
                              f"— folder not re-analysed")
                    # A reused folder contributes no per-video summary rows;
                    # its videos are recorded as ok so the run totals still
                    # describe the whole timecourse.
                    for v in videos:
                        cond, plate = _resolve_video_path(v, pfolder)
                        summary_rows.append({
                            "condition": cond, "plate": plate,
                            "status": "ok (reused)", "timepoint_h": tp})
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

                f_rows: list[dict] = []
                f_summary: list[dict] = []
                if videos:
                    with ThreadPoolExecutor(
                        max_workers=workers, thread_name_prefix="motility"
                    ) as ex:
                        futures = {}
                        for video in videos:
                            if self._cancel.is_set() or self._stop.is_set():
                                write_log("Run cancelled before submitting "
                                          "remaining videos")
                                break
                            fut = ex.submit(
                                _process_one_video_motility,
                                video, pfolder,
                                image=image,
                                engine=engine,
                                timeout_s=s.analysis_video_timeout_s,
                                params_template=self._params_template,
                                head_angle_prominence=head_angle_prominence,
                                threshold_s=threshold_s,
                                clear_cache=clear_cache,
                                flat_field=flat_field,
                                want_tracked=want_tracked,
                                want_curvature=want_curvature,
                                want_sidebyside=want_sidebyside,
                                want_per_worm_traces=want_per_worm_traces,
                                per_video_dir=per_video_dir,
                                ffmpeg_threads=ff_threads,
                                timepoint_h=tp,
                                report_stage=self.status.stages.reporter(video.name),
                                cancel_event=self._cancel,
                            )
                            futures[fut] = video

                        results_by_video: dict[Path, dict] = {}
                        for fut in as_completed(futures):
                            video = futures[fut]
                            try:
                                result = fut.result()
                            except Exception as exc:
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

                        # Ordered pass, so outputs match a serial run byte for byte.
                        for video in videos:
                            result = results_by_video.get(video)
                            if result is None:
                                continue
                            if result.get("crashed"):
                                cond, plate = _resolve_video_path(video, pfolder)
                                f_summary.append(
                                    build_summary_row([], cond, plate, 0.0, 0.0,
                                                      "worker crashed"))
                                continue
                            if result.get("cancelled"):
                                continue
                            f_rows.extend(result["fragment_rows"])
                            if result.get("summary_row"):
                                f_summary.append(result["summary_row"])

                for r in f_rows:
                    r["timepoint_h"] = tp
                    r["source_folder"] = str(pfolder)
                for r in f_summary:
                    r["timepoint_h"] = tp
                all_fragment_rows.extend(f_rows)
                summary_rows.extend(f_summary)
                folder_meta.append({
                    "folder": str(pfolder), "timepoint_h": tp,
                    "videos": run_cache.video_fingerprints(videos),
                    "n_rows": len(f_rows),
                    "n_videos_ok": sum(1 for r in f_summary
                                       if str(r.get("status", "")).startswith("ok")),
                    "n_videos_failed": sum(1 for r in f_summary
                                           if not str(r.get("status", "")).startswith("ok"))})

            _mot_cols = sorted({k for r in all_fragment_rows for k in r}) \
                if all_fragment_rows else []
            run_cache.write_rows(out_dir / run_cache.ROWS_NAME,
                                 all_fragment_rows, _mot_cols, write_log)
            run_cache.write_manifest(
                out_dir, pipeline="motility", digest=digest,
                folders=folder_meta, has_renders=want_renders,
                write_log=write_log)
            timepoints = sorted({r.get("timepoint_h") for r in all_fragment_rows
                                 if r.get("timepoint_h") is not None})
            if len(timepoints) > 1:
                write_log(f"Timecourse: {len(timepoints)} timepoints "
                          f"({', '.join(f'{t:g} h' for t in timepoints)})")

        # Write per-condition Excel workbook + summary CSV
        _sheet_cols = [
            "plate", "worm_index", "repr_tierpsy_id", "group_id", "frames", "duration_s",
            "bpm", "bend_interval_cv", "is_long", "fps_used", "group_classification",
            "curl_count", "fragment_count", "valid_frac", "displacement_px", "coverage_pct",
            "length_cv", "solidity_median", "speed_median_abs",
        ]
        # In a timecourse "plate 01" of one condition names a different plate on
        # every imaging day. Without the timepoint beside it these sheets hold
        # one indistinguishable row per day and no reader can tell which day a
        # number came from — the same collision the crawling sheets had.
        if any("timepoint_h" in r for r in all_fragment_rows):
            _sheet_cols.insert(0, "timepoint_h")
        if any("source_folder" in r for r in all_fragment_rows):
            _sheet_cols.append("source_folder")
        all_df = (pd.DataFrame(all_fragment_rows) if all_fragment_rows
                  else pd.DataFrame(columns=["condition"] + _sheet_cols))
        with pd.ExcelWriter(out_dir / "motility_results.xlsx", engine="openpyxl") as xw:
            name_seen: dict[str, int] = {}
            for cond in (sorted(all_df["condition"].unique()) if not all_df.empty else []):
                cdf = (all_df[all_df["condition"] == cond][_sheet_cols]
                       .sort_values("duration_s", ascending=False))
                cdf.to_excel(xw, sheet_name=_safe_sheet_name(cond, name_seen), index=False)
            pd.DataFrame(summary_rows).to_excel(xw, sheet_name="_summary", index=False)
            # Plate and condition layer, the two figures and the explorer.
            # Into the SAME workbook, under names that collide with nothing
            # above. Never fails the run: a day's imaging is not worth losing
            # to a bug in a reporting layer.
            try:
                import assay_reports
                assay_reports.motility_report(
                    xw.book, all_fragment_rows, out_dir, write_log,
                    long_threshold_s=threshold_s,
                    by_timepoint=len(timepoints) > 1)
            except Exception as exc:                              # noqa: BLE001
                log.warning("motility: report layer failed", exc_info=True)
                write_log(f"WARNING: the plate/condition sheets, the figures and "
                          f"the explorer could not be written ({exc}). Every "
                          "per-worm and per-video output of this run is "
                          "unaffected.")

        pd.DataFrame(summary_rows).to_csv(
            out_dir / "motility_summary.csv", index=False
        )

        elapsed_s = time.monotonic() - t_start
        make_overview_png(summary_rows, out_dir / "overview.png", elapsed_s)

        n_ok = sum(1 for r in summary_rows if r["status"] == "ok")
        n_fail = len(summary_rows) - n_ok

        log.info(
            "Motility analysis complete: %d ok, %d failed. Results: %s",
            n_ok, n_fail, out_dir,
        )
        self.status.mark_completed(n_ok, n_fail, out_dir)
