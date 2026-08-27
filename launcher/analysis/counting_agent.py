"""
Counting analysis agent. Parallel to the Motility / Crawling pipelines
(analysis/motility.py, analysis/crawling.py) but for clonogenic colony counting
on single-well stills. The per-image algorithm lives in analysis/counting.py and
is treated as a black box here — this module only wraps it in the same
agent/status/cancel/progress contract the UI already knows.

Thread boundary mirrors MotilityAgent/MotilityStatus and CrawlingAgent/
CrawlingStatus exactly:

Write contract — worker thread ONLY: call status.update() / status.mark_completed()
Read contract  — UI thread ONLY: call status.snapshot() / status.pop_completed()

Never touch Tk widgets from this thread.

Heavy deps (cv2, numpy, pandas, scikit-image, tifffile) are imported lazily
inside _run_analysis, so importing this module at launcher start-up is cheap and
safe even before those packages are installed — the same convention motility.py
and crawling.py follow. counting_preflight() reports a friendly error if they
are missing.
"""
import logging
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional
from analysis.stage_tracker import StageTracker

log = logging.getLogger(__name__)

# Mirror the discovery extension set / depth from analysis/counting.py without
# importing it (keeps this module import-safe before the heavy deps exist).
_IMAGE_EXTS = (".tif", ".tiff", ".png", ".jpg", ".jpeg")
_MAX_DEPTH = 3


# ---------------------------------------------------------------------------
# Pre-flight (no Docker / ffmpeg — counting is pure-Python image analysis)
# ---------------------------------------------------------------------------

def _count_images(folder: Path, max_depth: int = _MAX_DEPTH) -> int:
    """Count image files up to max_depth levels deep, skipping _/. dirs.
    Self-contained (no counting.py import) so it works before deps install."""
    count = 0

    def _recurse(path: Path, depth: int) -> None:
        nonlocal count
        if depth > max_depth:
            return
        try:
            for child in path.iterdir():
                if child.is_file() and child.suffix.lower() in _IMAGE_EXTS:
                    count += 1
                elif child.is_dir() and not (
                    child.name.startswith("_") or child.name.startswith(".")
                ):
                    _recurse(child, depth + 1)
        except PermissionError:
            pass

    _recurse(folder, 1)
    return count


def counting_preflight(folder: Path) -> list[str]:
    """Counting pre-flight. Returns human-readable error messages; empty = OK.

    Same return contract as docker_utils.run_preflight, but checks only what
    counting needs: at least one image, and the analysis dependencies importable.
    """
    errors: list[str] = []

    if _count_images(folder) == 0:
        errors.append(
            "No images found (.tif/.tiff/.png/.jpg/.jpeg, checked up to 3 levels deep)"
        )

    missing = []
    for mod in ("skimage", "tifffile", "imagecodecs"):
        try:
            __import__(mod)
        except ImportError:
            missing.append("scikit-image" if mod == "skimage" else mod)
    if missing:
        errors.append(
            "Missing analysis packages: " + ", ".join(missing) + ".\n"
            "Install them into the launcher venv:\n"
            "    pip install -r launcher/requirements.txt"
        )

    return errors


# ---------------------------------------------------------------------------
# Shared status snapshot (read-only, passed to UI thread)
# ---------------------------------------------------------------------------

@dataclass
class CountingSnapshot:
    color: str
    label: str
    running: bool
    current_index: int
    total: int
    current_basename: str
    current_stage: str
    # What the run is doing right now; see analysis/stage_tracker.py.
    stage_detail: str = ""


# ---------------------------------------------------------------------------
# Shared status object
# ---------------------------------------------------------------------------

class CountingStatus:
    """
    Shared state between the counting worker thread and the UI thread.

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
        # Live phases; its own lock (analysis/stage_tracker.py).
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
            self._label = f"Analysis complete: {n_ok}/{n_ok + n_fail} plates"
            self._running = False
            self._current_stage = ""
            self.stages.clear()
            self._completed_result = {
                "failed": False,
                "n_ok": n_ok,
                "n_fail": n_fail,
                "out_dir": out_dir,
            }

    def snapshot(self) -> CountingSnapshot:
        with self._lock:
            return CountingSnapshot(
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
# Counting agent
# ---------------------------------------------------------------------------

class CountingAgent(threading.Thread):
    """Background thread for colony counting. Idle until start_analysis() is called."""

    def __init__(self, settings: object, status: CountingStatus) -> None:
        super().__init__(daemon=True, name="CountingAgent")
        self._lock = threading.Lock()
        self._settings = settings
        self.status = status
        self._stop = threading.Event()
        self._cancel = threading.Event()
        self._wake = threading.Event()
        self._folder: Optional[Path] = None
        self._well_mode: str = "aim"
        self._split_sensitivity: float = 3.0
        self._min_colony_um: float = 200.0
        self._sensitivity: float = 5.0
        self._smooth_um: float = 0.0
        self._threshold_mode: str = "otsu"
        self._od_threshold: float = 0.05

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
        split_sensitivity: float = 3.0,
        min_colony_um: float = 200.0,
        sensitivity: float = 5.0,
        smooth_um: float = 0.0,
        threshold_mode: str = "otsu",
        od_threshold: float = 0.05,
        well_mode: str = "aim",
    ) -> None:
        """UI thread: trigger a counting run on the given folder."""
        with self._lock:
            self._folder = folder
            self._well_mode = well_mode
            self._split_sensitivity = split_sensitivity
            self._min_colony_um = min_colony_um
            self._sensitivity = sensitivity
            self._smooth_um = smooth_um
            self._threshold_mode = threshold_mode
            self._od_threshold = od_threshold
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
                split_sensitivity = self._split_sensitivity
                min_colony_um = self._min_colony_um
                sensitivity = self._sensitivity
                smooth_um = self._smooth_um
                threshold_mode = self._threshold_mode
                od_threshold = self._od_threshold
                well_mode = self._well_mode
                self._folder = None
            if folder is not None:
                self._cancel.clear()
                try:
                    self._run_analysis(folder, split_sensitivity, min_colony_um,
                                       sensitivity, smooth_um,
                                       threshold_mode, od_threshold, well_mode)
                except Exception as exc:
                    log.exception("CountingAgent crashed")
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
        folder: Path,
        split_sensitivity: float,
        min_colony_um: float,
        sensitivity: float = 5.0,
        smooth_um: float = 0.0,
        threshold_mode: str = "otsu",
        od_threshold: float = 0.05,
        well_mode: str = "aim",
    ) -> None:
        # Lazy import: the heavy pipeline (cv2/numpy/pandas/skimage) is only
        # pulled in once a run actually starts — mirrors motility/crawling.
        from analysis.counting import (
            CountingOptions, find_images, process_image, write_outputs,
            _options_note,
            threshold_scale, _ANALYSIS_PREFIX,
        )

        opts = CountingOptions(
            split_sensitivity=split_sensitivity,
            min_colony_um=min_colony_um,
            sensitivity=sensitivity,
            smooth_um=smooth_um,
            threshold=threshold_mode,
            od_threshold=od_threshold,
            well_mode=well_mode,
        )

        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        out_dir = folder / f"{_ANALYSIS_PREFIX}_{timestamp}"
        out_dir.mkdir(parents=True, exist_ok=True)
        # the crash handler in run() needs this to point the
        # user at the folder and to append the traceback
        self._last_out_dir = out_dir
        log_path = out_dir / "log.txt"

        with open(log_path, "w", encoding="utf-8") as lf:

            def write_log(msg: str) -> None:
                lf.write(msg + "\n")
                lf.flush()
                log.info("[counting] %s", msg)

            images = find_images(folder, opts.max_depth)
            total = len(images)
            write_log(f"Run: {timestamp}")
            write_log(f"Folder: {folder}")
            write_log(f"Images found: {total}")
            write_log(f"Split sensitivity: {split_sensitivity:.2f}")
            write_log(f"Min colony diameter: {min_colony_um:.0f} um")
            if well_mode == "aim":
                write_log(
                    f"Well: the capture aim circle — centred, radius "
                    f"{opts.aim_circle_frac:g} x the short side of the frame. "
                    f"Not detected in the image, so it cannot slip, and it is "
                    f"the same circle on every plate in the run."
                )
            else:
                write_log("Well: detected per image (older behaviour); a plate "
                          "whose rim cannot be fitted is skipped entirely.")
            if threshold_mode == "fixed":
                write_log(
                    f"Threshold: FIXED at {od_threshold:.4f} OD for every plate "
                    f"(comparable across conditions; detection sensitivity "
                    f"does not apply)"
                )
            else:
                write_log(f"Threshold: {threshold_mode} (derived per plate)")
                write_log(
                    f"Detection sensitivity: {sensitivity:.1f}/10 "
                    f"(threshold x{threshold_scale(sensitivity):.2f})"
                )
            write_log(
                "Colony smoothing: "
                + (f"{smooth_um:.0f} um" if smooth_um > 0 else "off")
            )

            self.status.update(
                color="yellow",
                label="Discovering images…",
                running=True,
                current_index=0,
                total=total,
                current_basename="",
                current_stage="Discovering images…",
            )

            all_colony_rows: list[dict] = []
            plate_rows: list[dict] = []

            for i, path in enumerate(images):
                if self._cancel.is_set() or self._stop.is_set():
                    write_log("Run cancelled before processing remaining images")
                    break
                self.status.update(
                    color="yellow",
                    label=f"{path.name} ({i + 1}/{total})",
                    running=True,
                    current_index=i,
                    total=total,
                    current_basename=path.name,
                    current_stage=f"{i + 1}/{total} done",
                )
                try:
                    colony_rows, plate_row = process_image(
                        path, folder, opts, out_dir, write_log,
                        stage=self.status.stages.reporter(path.name),
                    )
                except Exception as exc:
                    # One corrupt TIFF used to escape the loop, escape
                    # _run_analysis, and abort the run before write_outputs -
                    # so a bad plate 19 of 24 threw away the 18 good ones and
                    # wrote no xlsx at all. Skip the image, keep the batch.
                    log.exception("Counting failed on %s", path.name)
                    write_log(f"ERROR {path.name}: {exc}  (skipped)")
                    continue
                finally:
                    # Also on the skip path: an image that failed half way
                    # through would otherwise leave its last phase on screen,
                    # and a stalled run and a working one would look alike.
                    self.status.stages.clear()
                all_colony_rows.extend(colony_rows)
                if plate_row is not None:
                    plate_rows.append(plate_row)

            self.status.update(
                color="yellow",
                label="Writing results…",
                running=True,
                current_index=total,
                total=total,
                current_basename="",
                current_stage="Writing results…",
            )
            n_plates, n_colonies = write_outputs(
                out_dir, all_colony_rows, plate_rows, write_log,
                options_note=_options_note(opts))
            n_fail = total - n_plates

            write_log(
                f"done: {n_plates} plate(s), {n_colonies} colony(ies), "
                f"{n_fail} skipped -> {out_dir}"
            )
            log.info(
                "Counting analysis complete: %d ok, %d skipped. Results: %s",
                n_plates, n_fail, out_dir,
            )
            self.status.mark_completed(n_plates, n_fail, out_dir)
