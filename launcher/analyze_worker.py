"""
analyze_worker.py — laptop side of the "Analyze on laptop" button.

Long-polls the Pi capture service for a full-res frame captured by the web-UI
button, runs the vision-venv staging model over it via infer_stage.py, and
auto-opens the annotated PNG plus the counts txt. The laptop stays a pure HTTP
client: it never listens for inbound connections, it only polls GET
/analyze/next on the Pi.

The heavy inference runs in the vision venv (3.12 + ultralytics); this 3.13 side
only shells out to it, exactly as survival.py does for batch staging. Follows the
same start()/stop()/join() thread lifecycle as the other launcher agents.
"""
import datetime
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import paths
from typing import Optional

import requests

import config

log = logging.getLogger(__name__)

# Vision venv + CLI live next to this file under launcher/vision/. Derived from
# __file__ so the worker follows the repo wherever it is checked out.
_VISION_DIR = Path(__file__).parent / "vision"
# Resolved rather than hardcoded -- an installed copy keeps this venv under
# the install root to stay inside Windows MAX_PATH. See launcher/paths.py.
_VENV_PY = paths.vision_python()
_INFER = _VISION_DIR / "infer_stage.py"

# Fixed output root; each press writes a fresh timestamped run dir. Never a
# reused filename: Windows Photos locks the open image, so overwriting the
# previous annotated.png on the next press would fail with a sharing violation.
_OUT_ROOT = (
    Path(os.environ.get("USERPROFILE", Path.home()))
    / "Documents" / "WormScan" / "analyze_last"
)
_KEEP_RUNS = 10           # prune older run dirs on each press

_NEXT_TIMEOUT_S = 30      # server long-polls 25s; give the client a little slack
_RECONNECT_SLEEP_S = 2    # backoff after a connection error / non-200

# The launcher runs under pythonw.exe, which has no console of its own. Spawning
# python.exe without this makes Windows allocate one, so pressing "Analyze on
# laptop" in the browser popped an empty black terminal on the laptop for the
# length of the run. stdout and stderr are already captured below, so that
# console never had anything in it to read.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


# ---------------------------------------------------------------------------
# Status — the same worker-writes / UI-reads contract as the other agents
# ---------------------------------------------------------------------------

@dataclass
class AnalyzeSnapshot:
    busy: bool
    label: str


class AnalyzeStatus:
    """Shared state between the analyze worker and the UI thread.

    Write contract — worker thread ONLY: start_job() / finish_job().
    Read contract  — UI thread ONLY:     snapshot() / pop_finished().

    This worker used to have no status object at all, which is why the only
    sign that the button had done anything was a console window appearing. The
    UI now has something to show instead.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy = False
        self._label = ""
        self._finished: Optional[dict] = None

    def start_job(self, label: str) -> None:
        with self._lock:
            self._busy = True
            self._label = label

    def set_label(self, label: str) -> None:
        with self._lock:
            self._label = label

    def finish_job(self, ok: bool, run_dir: Optional[Path] = None,
                   error: str = "") -> None:
        with self._lock:
            self._busy = False
            self._label = ""
            self._finished = {"ok": ok, "run_dir": run_dir, "error": error}

    def snapshot(self) -> AnalyzeSnapshot:
        with self._lock:
            return AnalyzeSnapshot(busy=self._busy, label=self._label)

    def pop_finished(self) -> Optional[dict]:
        with self._lock:
            out = self._finished
            self._finished = None
            return out


def _prune_runs(root: Path, keep: int) -> None:
    """Keep the `keep` most recent run dirs, delete the rest. Names are
    YYYYmmdd_HHMMSS so lexical sort is chronological."""
    try:
        runs = sorted(d for d in root.iterdir() if d.is_dir())
    except OSError:
        return
    for old in runs[:-keep]:
        shutil.rmtree(old, ignore_errors=True)


def _run_inference(frame_path: Path, run_dir: Path,
                   class_conf: dict | None = None,
                   count_eggs: bool | None = None) -> tuple[bool, str]:
    """Run the staging model over one frame. Returns (ok, error_text)."""
    annotated = run_dir / "annotated.png"
    counts = run_dir / "counts.txt"
    cmd = [str(_VENV_PY), str(_INFER), str(frame_path),
           "--draw", str(annotated), "--counts", str(counts)]
    # count_eggs is None when the Pi did not send the header (older service):
    # say nothing and let stage_conf.json decide, rather than forcing a default
    # the user never chose.
    if count_eggs is True:
        cmd += ["--count-eggs"]
    elif count_eggs is False:
        cmd += ["--exclude-classes", "egg"]
    # No --conf and no --class-conf means infer_stage.py falls back to
    # vision/stage_conf.json — the same per-class thresholds the Worm Survival
    # batch run starts from, so the button and the pipeline never disagree by
    # accident. We only pass thresholds when the user has actually moved the
    # sliders in the analysis dialog, so the button follows their tuning too.
    if class_conf:
        cmd += ["--class-conf", json.dumps(class_conf)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True,
                       creationflags=_NO_WINDOW)
    except subprocess.CalledProcessError as exc:
        log.error("infer_stage failed (rc=%s):\n%s", exc.returncode, exc.stderr)
        tail = (exc.stderr or "").strip().splitlines()
        return False, (tail[-1] if tail else f"inference exited {exc.returncode}")
    except OSError as exc:
        log.error("could not start infer_stage: %s", exc)
        return False, str(exc)
    for out in (annotated, counts):
        try:
            os.startfile(str(out))  # Windows: open in the default app
        except OSError as exc:
            log.error("could not open %s: %s", out, exc)
    return True, ""


class AnalyzeWorker(threading.Thread):
    """Long-polls the Pi for "Analyze on laptop" frames and runs the staging
    model over each. Idle-blocks in the poll between presses. Started once at
    launch; stop()/join() in the shutdown block like the other agents."""

    def __init__(self, settings: object,
                 status: Optional[AnalyzeStatus] = None) -> None:
        super().__init__(daemon=True, name="AnalyzeWorker")
        self._settings = settings
        # Optional so an older call site still constructs; the UI passes one.
        self.status = status or AnalyzeStatus()
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        url = self._settings.pi_url.rstrip("/") + "/analyze/next"
        headers = {"X-Auth-Token": self._settings.token}
        log.info("analyze worker started; polling %s", url)
        # Flag checked before every poll, so stop() takes effect within one
        # in-flight request (never mid-request; the daemon exits at process end).
        while not self._stop.is_set():
            try:
                resp = requests.get(url, headers=headers, timeout=_NEXT_TIMEOUT_S)
            except requests.RequestException as exc:
                log.debug("analyze poll error: %s", exc)
                self._stop.wait(_RECONNECT_SLEEP_S)
                continue
            if resp.status_code == 204:
                continue  # long-poll idle timeout; poll again immediately
            if resp.status_code != 200:
                log.warning("analyze/next -> %s; backing off", resp.status_code)
                self._stop.wait(_RECONNECT_SLEEP_S)
                continue
            self._handle_frame(resp)
        log.info("analyze worker stopped")

    def _handle_frame(self, resp) -> None:
        job_id = resp.headers.get("X-Job-Id", "?")
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = _OUT_ROOT / stamp
        run_dir.mkdir(parents=True, exist_ok=True)
        _prune_runs(_OUT_ROOT, _KEEP_RUNS)
        frame = run_dir / "frame.tif"
        frame.write_bytes(resp.content)
        log.info("analyze job %s: %d bytes -> %s", job_id, len(resp.content), run_dir)
        # Missing header -> None -> stage_conf.json default (see _run_inference).
        raw = resp.headers.get("X-Count-Eggs")
        count_eggs = None if raw is None else (raw == "1")
        self.status.start_job("Loading the staging model…")
        try:
            ok, err = _run_inference(frame, run_dir, self._class_conf(),
                                     count_eggs)
        except Exception as exc:                     # never kill the poll loop
            log.exception("analyze job %s crashed", job_id)
            ok, err = False, f"{type(exc).__name__}: {exc}"
        self.status.finish_job(ok, run_dir, err)

    def _class_conf(self) -> dict | None:
        """Per-class thresholds for this frame, or None for the shared defaults.

        Re-read from config.json rather than from self._settings: this worker is
        handed the Settings object once at launch and is not on the
        _on_settings_saved propagation list, so its copy goes stale the moment
        the user touches the sliders. One small file read per button press, and
        it is the same file config.load() reads everywhere else — no second
        source of truth. A read failure just falls back to stage_conf.json.
        """
        try:
            return config.load().survival_class_conf or None
        except Exception as exc:
            log.debug("could not re-read config for analyze thresholds: %s", exc)
            return None
