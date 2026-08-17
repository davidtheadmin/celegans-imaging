"""
Motility analysis agent. Thread boundary mirrors SyncAgent/SyncStatus.

Write contract — worker thread ONLY: call status.update() / status.mark_completed()
Read contract  — UI thread ONLY: call status.snapshot() / status.pop_completed()

Never touch Tk widgets from this thread.
"""
import copy
import json
import logging
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import paths

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


# ---------------------------------------------------------------------------
# Per-video worker (runs on a ThreadPoolExecutor worker thread)
# ---------------------------------------------------------------------------

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
    want_tracked: bool,
    want_curvature: bool,
    want_sidebyside: bool,
    want_per_worm_traces: bool,
    per_video_dir: Path,
    ffmpeg_threads: Optional[int],
    cancel_event: threading.Event,
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
        fps = probe_fps(video)
        plog(f"fps: {fps:.3f}")

        video_duration_s = probe_duration(video)

        avi = cache_dir / (video.stem + ".avi")
        candidate_hdf5 = cache_dir / "Results" / (video.stem + "_featuresN.hdf5")
        cache_hit = not clear_cache and _hdf5_cache_valid(candidate_hdf5)

        if cache_hit:
            hdf5_path = candidate_hdf5
            needs_avi = (want_tracked or want_curvature
                         or want_sidebyside or want_per_worm_traces)
            if avi.exists():
                plog(f"[CACHE HIT] Skipping Tierpsy; AVI present: {video.name}")
            elif needs_avi:
                plog(f"[CACHE HIT] Skipping Tierpsy; converting AVI for rendering: {video.name}")
                cache_dir.mkdir(parents=True, exist_ok=True)
                convert_to_avi(video, avi, threads=ffmpeg_threads)
                plog(f"AVI ready: {avi}")
            else:
                plog(f"[CACHE HIT] Skipping Tierpsy + AVI: {video.name}")
        else:
            cache_dir.mkdir(parents=True, exist_ok=True)
            convert_to_avi(video, avi, threads=ffmpeg_threads)
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

            stdout, stderr = run_tierpsy(
                avi, json_file,
                image=image,
                engine=engine,
                timeout_s=timeout_s,
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

        log_sidecar = per_video_dir / f"{condition}__{plate}_analysis_log.json"
        log_sidecar.write_text(
            _json.dumps({"video": video.name, **analysis_log}, indent=2),
            encoding="utf-8",
        )

        if fragment_rows and hdf5_path:
            plot_path = per_video_dir / f"{condition}__{plate}.png"
            make_video_summary_png(fragment_rows, hdf5_path, fps, plot_path,
                                   head_angle_prominence)

        if want_tracked or want_curvature or want_sidebyside or want_per_worm_traces:
            from analysis.render_video import (
                render_tracked, render_curvature, render_sidebyside,
                render_per_worm_trace,
            )
            skeletons_hdf5 = cache_dir / "Results" / f"{video.stem}_skeletons.hdf5"
            masked_hdf5 = cache_dir / "MaskedVideos" / f"{video.stem}.hdf5"
            prefix = f"{condition}__{plate}"

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

    def mark_completed(self, n_ok: int, n_fail: int, out_dir: Path) -> None:
        with self._lock:
            self._color = "green"
            self._label = f"Analysis complete: {n_ok}/{n_ok + n_fail} videos"
            self._running = False
            self._current_stage = ""
            self._completed_result = {
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
        self._folder: Optional[Path] = None
        self._threshold_s: float = 5.0
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
        folder: Path,
        threshold_s: float = 5.0,
        clear_cache: bool = False,
        want_tracked: bool = False,
        want_curvature: bool = False,
        want_sidebyside: bool = False,
        want_per_worm_traces: bool = False,
    ) -> None:
        """UI thread: trigger an analysis run on the given folder."""
        with self._lock:
            self._folder = folder
            self._threshold_s = threshold_s
            self._clear_cache = clear_cache
            self._want_tracked = want_tracked
            self._want_curvature = want_curvature
            self._want_sidebyside = want_sidebyside
            self._want_per_worm_traces = want_per_worm_traces
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
                folder = self._folder
                threshold_s = self._threshold_s
                clear_cache = self._clear_cache
                want_tracked = self._want_tracked
                want_curvature = self._want_curvature
                want_sidebyside = self._want_sidebyside
                want_per_worm_traces = self._want_per_worm_traces
                self._folder = None
            if folder is not None:
                self._cancel.clear()
                try:
                    self._run_analysis(
                        folder, threshold_s, clear_cache,
                        want_tracked, want_curvature, want_sidebyside,
                        want_per_worm_traces,
                    )
                except Exception:
                    log.exception("MotilityAgent crashed")
                    self.status.update(
                        color="red",
                        label="Analysis crashed — see log",
                        running=False,
                    )

    def _run_analysis(
        self,
        folder: Path,
        threshold_s: float,
        clear_cache: bool,
        want_tracked: bool = False,
        want_curvature: bool = False,
        want_sidebyside: bool = False,
        want_per_worm_traces: bool = False,
    ) -> None:
        from analysis.ffmpeg_utils import find_videos
        from analysis.analysis_csv import build_summary_row
        from analysis.plots import make_overview_png
        from analysis.concurrency import resolve_workers, ffmpeg_threads_per_worker
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import pandas as pd

        s = self._get_settings()
        t_start = time.monotonic()
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        out_dir = folder / f"{_ANALYSIS_PREFIX}_{timestamp}"
        out_dir.mkdir(parents=True, exist_ok=True)
        per_video_dir = out_dir / "per_video"
        per_video_dir.mkdir(exist_ok=True)
        log_path = out_dir / "log.txt"

        with open(log_path, "w", encoding="utf-8") as lf:

            def write_log(msg: str) -> None:
                lf.write(msg + "\n")
                lf.flush()
                log.info("[motility] %s", msg)

            if clear_cache:
                write_log("Clearing cache folders…")
                for cache in folder.rglob(_CACHE_DIR):
                    if cache.is_dir():
                        shutil.rmtree(cache, ignore_errors=True)
                        write_log(f"  Removed: {cache}")

            videos = find_videos(folder)
            total = len(videos)
            write_log(f"Run: {timestamp}")
            write_log(f"Folder: {folder}")
            write_log(f"Videos found: {total}")
            write_log(f"Long threshold: {threshold_s}s")

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
            from analysis.engine import Engine as _Engine
            engine = resolve_engine(s) or _Engine(
                command=getattr(s, "docker_command", "docker"),
                kind="docker", version="(not detected)")
            image = resolve_image(s)
            write_log(f"Container engine: {engine}")
            write_log(f"Tierpsy image: {image}")
            write_log(paths.describe())
            head_angle_prominence = float(self._params_template.get("head_angle_prominence", 0.30))
            all_fragment_rows: list[dict] = []
            summary_rows: list[dict] = []

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

            def _publish_progress(done: int, last_name: str) -> None:
                self.status.update(
                    color="yellow",
                    label=f"{last_name} ({done}/{total})" if last_name else "Analysing…",
                    running=True,
                    current_index=done,
                    total=total,
                    current_basename=last_name,
                    current_stage=f"{done}/{total} done",
                )

            _publish_progress(0, "")

            done = 0
            if videos:
                with ThreadPoolExecutor(
                    max_workers=workers, thread_name_prefix="motility"
                ) as ex:
                    futures = {}
                    for video in videos:
                        if self._cancel.is_set() or self._stop.is_set():
                            write_log("Run cancelled before submitting remaining videos")
                            break
                        fut = ex.submit(
                            _process_one_video_motility,
                            video, folder,
                            image=image,
                            engine=engine,
                            timeout_s=s.analysis_video_timeout_s,
                            params_template=self._params_template,
                            head_angle_prominence=head_angle_prominence,
                            threshold_s=threshold_s,
                            clear_cache=clear_cache,
                            want_tracked=want_tracked,
                            want_curvature=want_curvature,
                            want_sidebyside=want_sidebyside,
                            want_per_worm_traces=want_per_worm_traces,
                            per_video_dir=per_video_dir,
                            ffmpeg_threads=ff_threads,
                            cancel_event=self._cancel,
                        )
                        futures[fut] = video

                    # Collect on the agent thread: flush each video's buffered
                    # log block contiguously (in completion order, for liveness)
                    # and advance the bar as each finishes. Data rows are NOT
                    # accumulated here — see the ordered pass below.
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
                            results_by_video[video] = {"crashed": True,
                                                       "exc": str(exc)[:200]}
                            done += 1
                            _publish_progress(done, video.name)
                            continue

                        for line in result["logbuf"]:
                            write_log(line)
                        results_by_video[video] = result
                        done += 1
                        _publish_progress(done, result["video"].name)

                    # Accumulate in the original discovery order so the outputs
                    # (incl. unsorted _summary sheet / CSV row order and tie
                    # breaks in sorted sheets) are byte-identical to a serial run.
                    for video in videos:
                        result = results_by_video.get(video)
                        if result is None:  # never submitted (cancel)
                            continue
                        if result.get("crashed"):
                            cond, plate = _resolve_video_path(video, folder)
                            summary_rows.append(
                                build_summary_row([], cond, plate, 0.0, 0.0,
                                                  result["exc"])
                            )
                            continue
                        if result.get("cancelled"):
                            continue
                        all_fragment_rows.extend(result["fragment_rows"])
                        if result["summary_row"] is not None:
                            summary_rows.append(result["summary_row"])

        # Write per-condition Excel workbook + summary CSV
        _sheet_cols = [
            "plate", "worm_index", "repr_tierpsy_id", "group_id", "frames", "duration_s",
            "bpm", "bend_interval_cv", "is_long", "fps_used", "group_classification",
            "curl_count", "fragment_count", "valid_frac", "displacement_px", "coverage_pct",
            "length_cv", "solidity_median", "speed_median_abs",
        ]
        all_df = (pd.DataFrame(all_fragment_rows) if all_fragment_rows
                  else pd.DataFrame(columns=["condition"] + _sheet_cols))
        with pd.ExcelWriter(out_dir / "motility_results.xlsx", engine="openpyxl") as xw:
            name_seen: dict[str, int] = {}
            for cond in (sorted(all_df["condition"].unique()) if not all_df.empty else []):
                cdf = (all_df[all_df["condition"] == cond][_sheet_cols]
                       .sort_values("duration_s", ascending=False))
                cdf.to_excel(xw, sheet_name=_safe_sheet_name(cond, name_seen), index=False)
            pd.DataFrame(summary_rows).to_excel(xw, sheet_name="_summary", index=False)

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
