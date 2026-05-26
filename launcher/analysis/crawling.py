"""
Crawling analysis agent. Parallel to the Motility pipeline (analysis/motility.py).

Initially a near-exact copy of the motility pipeline — same Tierpsy parameters,
same ffmpeg flags, same output format. It will diverge incrementally. The one
intentional difference is the Tierpsy docker subprocess call, which is heavily
instrumented here for diagnostics (see _run_tierpsy_instrumented).

Thread boundary mirrors SyncAgent/SyncStatus and MotilityAgent/MotilityStatus.

Write contract — worker thread ONLY: call status.update() / status.mark_completed()
Read contract  — UI thread ONLY: call status.snapshot() / status.pop_completed()

Never touch Tk widgets from this thread.
"""
import copy
import json
import logging
import shlex
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

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


def _print_output_tree(root: Path) -> None:
    """Print a recursive listing of the Tierpsy output dir, with file sizes."""
    print(f"[crawling] Tierpsy output dir listing: {root}", flush=True)
    if not root.exists():
        print("  (directory does not exist)", flush=True)
        return
    entries = sorted(root.rglob("*"), key=lambda p: p.as_posix())
    if not entries:
        print("  (empty)", flush=True)
        return
    for p in entries:
        try:
            rel = p.relative_to(root).as_posix()
            if p.is_dir():
                print(f"  {rel}/", flush=True)
            else:
                size = p.stat().st_size
                print(f"  {rel}  ({_fmt_size(size)}, {size} bytes)", flush=True)
        except OSError as exc:
            print(f"  {p}  (stat error: {exc})", flush=True)


def _run_tierpsy_instrumented(
    video_avi: Path,
    json_file: Path,
    image: str,
    output_dir: Path,
    docker_cmd: str = "docker",
    timeout_s: int = _TIERPSY_TIMEOUT_S,
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
    parent_posix = video_avi.parent.as_posix()
    cmd = [
        docker_cmd, "run", "--rm",
        "-v", f"{parent_posix}:/data",
        image,
        "tierpsy_process",
        "--video_dir_root",   "/data",
        "--mask_dir_root",    "/data/MaskedVideos",
        "--results_dir_root", "/data/Results",
        "--pattern_include",  video_avi.name,
        "--json_file",        f"/data/{json_file.name}",
        "--max_num_process",  "1",
    ]

    print("=" * 72, flush=True)
    print("[crawling] Running Tierpsy docker command:", flush=True)
    print("  " + " ".join(shlex.quote(c) for c in cmd), flush=True)
    print(f"[crawling] timeout: {timeout_s}s", flush=True)
    print("=" * 72, flush=True)

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
        for line in proc.stdout:
            captured.append(line)
            print("[tierpsy] " + line.rstrip("\n"), flush=True)
        proc.wait()
    finally:
        timer.cancel()

    returncode = proc.returncode
    combined = "".join(captured)

    print("-" * 72, flush=True)
    print(
        f"[crawling] Tierpsy exited: returncode={returncode}"
        f"{' (TIMED OUT)' if timed_out['value'] else ''}",
        flush=True,
    )
    _print_output_tree(output_dir)
    print("-" * 72, flush=True)

    if timed_out["value"]:
        raise RuntimeError(f"Tierpsy timed out after {timeout_s}s")
    if returncode != 0:
        raise RuntimeError(
            f"Tierpsy exited {returncode}:\n{combined.strip()[-1000:]}"
        )
    return combined, ""


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
        self._folder: Optional[Path] = None
        self._threshold_s: float = 5.0
        self._min_track_s: float = 60.0
        self._clear_cache: bool = False
        self._want_tracked: bool = False
        self._want_sidebyside: bool = False
        self._want_path_traces: bool = False
        self._params_template: dict = {}
        self._load_params()

    def _load_params(self) -> None:
        params_path = Path(__file__).parent.parent / "crawling_params.json"
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
        folder: Path,
        threshold_s: float = 5.0,
        clear_cache: bool = False,
        want_tracked: bool = False,
        want_sidebyside: bool = False,
        want_path_traces: bool = False,
        min_track_s: float = 60.0,
    ) -> None:
        """UI thread: trigger an analysis run on the given folder."""
        with self._lock:
            self._folder = folder
            self._threshold_s = threshold_s
            self._min_track_s = min_track_s
            self._clear_cache = clear_cache
            self._want_tracked = want_tracked
            self._want_sidebyside = want_sidebyside
            self._want_path_traces = want_path_traces
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
                min_track_s = self._min_track_s
                clear_cache = self._clear_cache
                want_tracked = self._want_tracked
                want_sidebyside = self._want_sidebyside
                want_path_traces = self._want_path_traces
                self._folder = None
            if folder is not None:
                self._cancel.clear()
                try:
                    self._run_analysis(
                        folder, threshold_s, clear_cache,
                        want_tracked, want_sidebyside, want_path_traces,
                        min_track_s,
                    )
                except Exception:
                    log.exception("CrawlingAgent crashed")
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
        want_sidebyside: bool = False,
        want_path_traces: bool = False,
        min_track_s: float = 60.0,
    ) -> None:
        from analysis.ffmpeg_utils import find_videos, probe_fps, convert_to_avi
        from analysis.crawling_metrics import (
            compute_crawling_metrics, aggregate_per_condition,
            PER_WORM_COLS,
        )
        import pandas as pd

        s = self._get_settings()
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
                log.info("[crawling] %s", msg)

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
            write_log(f"Min track duration: {min_track_s:.1f}s")

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
            all_worm_rows: list[dict] = []
            n_ok = 0
            n_fail = 0

            for i, video in enumerate(videos):
                if self._cancel.is_set() or self._stop.is_set():
                    write_log("Run cancelled by user")
                    break

                condition, plate = _resolve_video_path(video, folder)
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
                worm_rows: list[dict] = []
                status_str = "ok"
                hdf5_path: Optional[Path] = None
                cache_dir = _cache_dir_for(video)

                try:
                    _stage("Probing fps…")
                    fps = probe_fps(video)
                    write_log(f"fps: {fps:.3f}")

                    avi = cache_dir / (video.stem + ".avi")
                    candidate_hdf5 = cache_dir / "Results" / (video.stem + "_featuresN.hdf5")
                    cache_hit = not clear_cache and _hdf5_cache_valid(candidate_hdf5)

                    if cache_hit:
                        hdf5_path = candidate_hdf5
                        needs_avi = (want_tracked or want_sidebyside or want_path_traces)
                        if avi.exists():
                            write_log(f"[CACHE HIT] Skipping Tierpsy; AVI present: {video.name}")
                            _stage("Reading cached features…")
                        elif needs_avi:
                            write_log(f"[CACHE HIT] Skipping Tierpsy; converting AVI for rendering: {video.name}")
                            _stage("Converting to AVI (for rendering)…")
                            cache_dir.mkdir(parents=True, exist_ok=True)
                            convert_to_avi(video, avi)
                            write_log(f"AVI ready: {avi}")
                        else:
                            write_log(f"[CACHE HIT] Skipping Tierpsy + AVI: {video.name}")
                            _stage("Reading cached features…")
                    else:
                        _stage("Converting to AVI…")
                        cache_dir.mkdir(parents=True, exist_ok=True)
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
                        stdout, stderr = _run_tierpsy_instrumented(
                            avi, json_file,
                            image=image,
                            output_dir=cache_dir,
                            docker_cmd=s.docker_command,
                            timeout_s=_TIERPSY_TIMEOUT_S,
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

                    _stage("Computing crawling metrics…")
                    worm_rows = compute_crawling_metrics(
                        hdf5_path, fps, condition, plate, video.name,
                        head_angle_prominence=head_angle_prominence,
                        min_run_s=min_track_s,
                    )
                    write_log(f"Worms tracked: {len(worm_rows)}")

                    # Worms that survive the quality filter — the renders must
                    # show exactly this set (same set for all three modes).
                    kept_ids = {
                        int(r["worm_index_joined"])
                        for r in worm_rows if r.get("passed_filter")
                    }
                    write_log(
                        f"Worms passing filter (>= {min_track_s:.1f}s): "
                        f"{len(kept_ids)}/{len(worm_rows)}"
                    )

                    if want_tracked or want_sidebyside or want_path_traces:
                        from analysis.render_video import render_tracked, render_sidebyside
                        from analysis.crawling_render import render_path_traces
                        skeletons_hdf5 = cache_dir / "Results" / f"{video.stem}_skeletons.hdf5"
                        masked_hdf5 = cache_dir / "MaskedVideos" / f"{video.stem}.hdf5"
                        prefix = f"{condition}__{plate}"
                        if want_tracked and skeletons_hdf5.exists() and avi.exists():
                            _stage("Rendering tracked video…")
                            render_tracked(
                                avi, skeletons_hdf5,
                                per_video_dir / f"{prefix}_tracked.mp4", fps,
                                kept_ids=kept_ids,
                            )
                        if want_sidebyside and masked_hdf5.exists() and skeletons_hdf5.exists() and avi.exists():
                            _stage("Rendering side-by-side video…")
                            render_sidebyside(
                                avi, masked_hdf5, skeletons_hdf5,
                                per_video_dir / f"{prefix}_sidebyside.mp4", fps,
                                kept_ids=kept_ids,
                            )
                        if want_path_traces and skeletons_hdf5.exists() and avi.exists():
                            _stage("Rendering path traces…")
                            render_path_traces(
                                avi, skeletons_hdf5,
                                per_video_dir / f"{prefix}_path_traces.mp4", fps,
                                kept_ids=kept_ids,
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

                all_worm_rows.extend(worm_rows)
                if status_str == "ok":
                    n_ok += 1
                else:
                    n_fail += 1

        # ---- Build output: per_worm + per_condition sheets, CSV mirrors per_condition ----
        per_worm_df = (pd.DataFrame(all_worm_rows, columns=PER_WORM_COLS).round(4)
                       if all_worm_rows
                       else pd.DataFrame(columns=PER_WORM_COLS))
        per_condition_rows = aggregate_per_condition(all_worm_rows, min_run_s=min_track_s)
        per_condition_df = (pd.DataFrame(per_condition_rows).round(4)
                            if per_condition_rows
                            else pd.DataFrame(columns=["condition", "n_worms"]))

        with pd.ExcelWriter(out_dir / "crawling_results.xlsx", engine="openpyxl") as xw:
            per_worm_df.to_excel(xw, sheet_name="per_worm", index=False)
            per_condition_df.to_excel(xw, sheet_name="per_condition", index=False)

        per_condition_df.to_csv(out_dir / "crawling_summary.csv", index=False)

        log.info(
            "Crawling analysis complete: %d ok, %d failed. Results: %s",
            n_ok, n_fail, out_dir,
        )
        self.status.mark_completed(n_ok, n_fail, out_dir)
