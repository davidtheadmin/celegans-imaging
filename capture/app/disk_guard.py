"""Capture-time storage guard.

Called at the top of every capture endpoint, BEFORE any camera work, so that
reclamation never runs while holding the camera lock. If the card is below the
free-space floor, it first tries to reclaim space (deleting acked / expired-trash
files); if it still can't, it refuses the capture with HTTP 507 so the launcher
can surface a clear message instead of writing a partial/failed frame.
"""
import shutil
import sys
from pathlib import Path

from fastapi import HTTPException


def ensure_capture_space(data_root, min_free_gb: float) -> None:
    free = shutil.disk_usage(str(data_root)).free / 1e9
    if free >= min_free_gb:
        return

    # Lazy import: defers retention's logging.basicConfig, and ensures the
    # capture/ dir is importable as a top-level package regardless of how the
    # service was launched (the running service has no `capture` package on its
    # path — `capture` already resolves to capture.py — so import `retention`).
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # .../capture
    from retention import reclaim

    reclaim(min_free_gb, data_root=data_root)
    free = shutil.disk_usage(str(data_root)).free / 1e9
    if free < min_free_gb:
        raise HTTPException(
            507,
            "Insufficient storage: card is full of un-synced data. "
            "Sync to the laptop, then retry.",
        )
