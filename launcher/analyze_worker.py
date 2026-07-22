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
import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path

import requests

log = logging.getLogger(__name__)

# Vision venv + CLI live next to this file under launcher/vision/. Derived from
# __file__ so the worker follows the repo wherever it is checked out.
_VISION_DIR = Path(__file__).parent / "vision"
_VENV_PY = _VISION_DIR / ".venv-vision" / "Scripts" / "python.exe"
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


def _prune_runs(root: Path, keep: int) -> None:
    """Keep the `keep` most recent run dirs, delete the rest. Names are
    YYYYmmdd_HHMMSS so lexical sort is chronological."""
    try:
        runs = sorted(d for d in root.iterdir() if d.is_dir())
    except OSError:
        return
    for old in runs[:-keep]:
        shutil.rmtree(old, ignore_errors=True)


def _run_inference(frame_path: Path, run_dir: Path) -> None:
    annotated = run_dir / "annotated.png"
    counts = run_dir / "counts.txt"
    try:
        subprocess.run(
            [str(_VENV_PY), str(_INFER), str(frame_path),
             "--draw", str(annotated), "--counts", str(counts)],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as exc:
        log.error("infer_stage failed (rc=%s):\n%s", exc.returncode, exc.stderr)
        return
    for out in (annotated, counts):
        try:
            os.startfile(str(out))  # Windows: open in the default app
        except OSError as exc:
            log.error("could not open %s: %s", out, exc)


class AnalyzeWorker(threading.Thread):
    """Long-polls the Pi for "Analyze on laptop" frames and runs the staging
    model over each. Idle-blocks in the poll between presses. Started once at
    launch; stop()/join() in the shutdown block like the other agents."""

    def __init__(self, settings: object) -> None:
        super().__init__(daemon=True, name="AnalyzeWorker")
        self._settings = settings
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
        _run_inference(frame, run_dir)
