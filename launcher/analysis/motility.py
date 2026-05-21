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

log = logging.getLogger(__name__)

_ANALYSIS_DIR = "_analysis"
_CACHE_DIR = "_wormscan_cache"

# Keys in motility_params.json that are consumed by our post-Tierpsy code and
# must never be written into the per-video JSON that Tierpsy validates.
_WORMSCAN_ONLY_KEYS: frozenset[str] = frozenset({"head_angle_prominence"})


def _condition_for(video: Path, selected_folder: Path) -> str:
    return "unlabeled" if video.parent == selected_folder else video.parent.name


def _cache_dir_for(video: Path) -> Path:
    """Return the per-video cache directory next to the original MP4."""
    return video.parent / _CACHE_DIR / video.stem


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
        params_path = Path(__file__).parent.parent / "motility_params.json"
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
        from analysis.ffmpeg_utils import find_videos, probe_fps, probe_duration, convert_to_avi
        from analysis.docker_utils import run_tierpsy
        from analysis.analysis_csv import read_fragments, build_summary_row
        from analysis.plots import make_video_summary_png, make_overview_png
        import pandas as pd

        s = self._get_settings()
        t_start = time.monotonic()
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        out_dir = folder / _ANALYSIS_DIR / timestamp
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

            image = f"{s.tierpsy_image}:{s.tierpsy_image_tag}"
            head_angle_prominence = float(self._params_template.get("head_angle_prominence", 0.30))
            all_fragment_rows: list[dict] = []
            summary_rows: list[dict] = []

            for i, video in enumerate(videos):
                if self._cancel.is_set() or self._stop.is_set():
                    write_log("Run cancelled by user")
                    break

                condition = _condition_for(video, folder)
                plate = video.stem
                short_label = f"{video.name} ({i + 1}/{total})"

                def _stage(stage: str) -> None:
                    self.status.update(
                        color="yellow",
                        label=short_label,
                        running=True,
                        current_index=i,
                        total=total,
                        current_basename=video.name,
                        current_stage=stage,
                    )

                write_log(f"\n--- {video.name} ({condition}) ---")

                fps = 0.0
                video_duration_s = 0.0
                fragment_rows: list[dict] = []
                status_str = "ok"
                hdf5_path: Optional[Path] = None
                cache_dir = _cache_dir_for(video)

                try:
                    _stage("Probing fps…")
                    fps = probe_fps(video)
                    write_log(f"fps: {fps:.3f}")

                    video_duration_s = probe_duration(video)

                    _stage("Converting to AVI…")
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    avi = cache_dir / (video.stem + ".avi")
                    convert_to_avi(video, avi)
                    write_log(f"AVI ready: {avi}")

                    params = copy.deepcopy(self._params_template)
                    if "expected_fps" in params:
                        params["expected_fps"] = fps
                    for _k in _WORMSCAN_ONLY_KEYS:
                        params.pop(_k, None)
                    json_file = cache_dir / (video.stem + ".json")
                    json_file.write_text(
                        json.dumps(params, indent=2), encoding="utf-8"
                    )

                    _stage("Running Tierpsy…")
                    stdout, stderr = run_tierpsy(
                        avi, json_file,
                        image=image,
                        docker_cmd=s.docker_command,
                        timeout_s=s.analysis_video_timeout_s,
                    )
                    write_log(f"Tierpsy stdout:\n{stdout}")
                    if stderr.strip():
                        write_log(f"Tierpsy stderr:\n{stderr}")

                    _stage("Reading features…")
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
                    write_log(
                        f"Worms: {len(fragment_rows)} total, {n_long} long"
                        f" (threshold {threshold_s}s) | "
                        f"groups formed: {analysis_log['groups_formed']['total']}"
                        f" (curl={analysis_log['groups_formed']['curl']},"
                        f" collision={analysis_log['groups_formed']['collision']})"
                        f" | dropped: {analysis_log['worms_dropped']['total']}"
                    )

                    import json as _json
                    log_sidecar = per_video_dir / f"{condition}__{plate}_analysis_log.json"
                    log_sidecar.write_text(
                        _json.dumps({"video": video.name, **analysis_log}, indent=2),
                        encoding="utf-8",
                    )

                    _stage("Writing plots…")
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
                        if want_tracked and skeletons_hdf5.exists():
                            _stage("Rendering tracked video…")
                            render_tracked(
                                avi, skeletons_hdf5,
                                per_video_dir / f"{prefix}_tracked.mp4", fps,
                            )
                        if want_curvature and skeletons_hdf5.exists() and hdf5_path:
                            _stage("Rendering curvature video…")
                            render_curvature(
                                avi, skeletons_hdf5, hdf5_path,
                                per_video_dir / f"{prefix}_curvature.mp4", fps,
                            )
                        if want_sidebyside and masked_hdf5.exists() and skeletons_hdf5.exists():
                            _stage("Rendering side-by-side video…")
                            render_sidebyside(
                                avi, masked_hdf5, skeletons_hdf5,
                                per_video_dir / f"{prefix}_sidebyside.mp4", fps,
                            )
                        if (want_per_worm_traces and hdf5_path
                                and skeletons_hdf5.exists() and masked_hdf5.exists()):
                            from analysis.plots import make_per_worm_trace_png
                            full_track_rows = [r for r in fragment_rows
                                               if r.get("is_full_track")]
                            n_full = len(full_track_rows)
                            traces_dir = per_video_dir / f"{prefix}_traces"
                            traces_dir.mkdir(exist_ok=True)
                            for j, worm_row in enumerate(full_track_rows):
                                wi = worm_row.get("repr_tierpsy_id", worm_row["worm_index"])
                                _stage(f"Rendering per-worm traces ({j + 1}/{n_full})…")
                                make_per_worm_trace_png(
                                    wi, hdf5_path, fps, prefix,
                                    traces_dir / f"worm_{wi}.png",
                                    worm_row["bpm"],
                                    worm_row.get("coverage_pct", 0.0),
                                    head_angle_prominence,
                                )
                                render_per_worm_trace(
                                    masked_hdf5, skeletons_hdf5, hdf5_path,
                                    wi, fps,
                                    traces_dir / f"worm_{wi}.mp4",
                                    head_angle_prominence,
                                )

                except Exception as exc:
                    status_str = str(exc)[:200]
                    write_log(f"ERROR: {exc}")
                    log.exception("Error processing %s", video.name)

                # Advance the bar to show this video is done
                self.status.update(
                    color="yellow",
                    label=short_label,
                    running=True,
                    current_index=i + 1,
                    total=total,
                    current_basename=video.name,
                    current_stage="",
                )

                all_fragment_rows.extend(fragment_rows)
                summary_rows.append(
                    build_summary_row(
                        fragment_rows, condition, plate, fps,
                        video_duration_s, status_str,
                    )
                )

        # Write per-condition Excel workbook + summary CSV
        _sheet_cols = [
            "plate", "worm_index", "frames", "duration_s", "bpm", "is_long", "fps_used",
            "group_classification", "curl_count", "fragment_count", "valid_frac",
            "displacement_px", "coverage_pct",
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
