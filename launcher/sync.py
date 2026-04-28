"""
Sync agent: polls /manifest and mirrors unacked files to the local mirror folder.

Thread boundary contract
------------------------
SyncStatus is the ONLY channel between the sync thread and the UI thread.

  Write contract — sync thread ONLY  : call status.update() to set fields atomically.
  Read contract  — UI thread ONLY    : call status.snapshot() to get a consistent copy.

Never call widget methods from the sync thread. Tk is single-threaded and will
corrupt internal state or hang in non-obvious ways if touched from outside the
main thread. The UI polls status.snapshot() via root.after() instead.
"""
import hashlib
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)

# Must match EXPERIMENTS_DIR / PICTURES_DIR / VIDEOS_DIR in capture/app/config.py
_EXPERIMENTS_DIR = "experiments"
_PICTURES_DIR = "pictures"
_VIDEOS_DIR = "videos"

_UNSAFE_CHARS = str.maketrans(r'\/:*?"<>|', "_________")
_MAX_COMPONENT = 200


def _sanitize(text: str) -> str:
    """Make text safe for use as a filesystem path component."""
    out = text.translate(_UNSAFE_CHARS).strip(". ")[:_MAX_COMPONENT]
    return out or "_"


def _build_name_maps(
    sessions: list[dict],
) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """
    Build collision-aware friendly name mappings for sessions and conditions.

    Returns:
        session_dirs  — {session_id: friendly_experiment_dir_name}
        cond_dirs     — {session_id: {condition_id: friendly_condition_dir_name}}
    """
    # Count sanitized experiment names to detect collisions
    name_counts: dict[str, int] = {}
    for sm in sessions:
        key = _sanitize(sm.get("experiment_name") or sm["session_id"])
        name_counts[key] = name_counts.get(key, 0) + 1

    session_dirs: dict[str, str] = {}
    cond_dirs: dict[str, dict[str, str]] = {}

    for sm in sessions:
        sid = sm["session_id"]
        base = _sanitize(sm.get("experiment_name") or sid)
        if name_counts[base] > 1:
            session_dirs[sid] = f"{base} ({sid[:6]})"
        else:
            session_dirs[sid] = base

        # Build condition dirs within this session
        # Gather all (condition_id, condition_name) pairs seen in files
        cond_names: dict[str, str] = {}
        for entry in sm.get("files", []):
            cid = entry.get("plate_id", "").rsplit("_", 2)[0] if entry.get("plate_id") else ""
            cname = entry.get("condition_name") or cid
            if cid and cid not in cond_names:
                cond_names[cid] = cname

        cname_counts: dict[str, int] = {}
        for cid, cname in cond_names.items():
            key = _sanitize(cname)
            cname_counts[key] = cname_counts.get(key, 0) + 1

        this_cond: dict[str, str] = {}
        for cid, cname in cond_names.items():
            base_c = _sanitize(cname)
            if cname_counts[base_c] > 1:
                this_cond[cid] = f"{base_c} ({cid[:6]})"
            else:
                this_cond[cid] = base_c
        cond_dirs[sid] = this_cond

    return session_dirs, cond_dirs


# ---------------------------------------------------------------------------
# Shared status object
# ---------------------------------------------------------------------------

class SyncStatus:
    """
    Shared state between the sync thread and the UI thread.

    Write contract — sync thread ONLY: call update() to set all fields atomically.
    Read contract  — UI thread ONLY: call snapshot() to get a consistent copy.

    Never read _private attributes directly from either thread.
    The internal threading.Lock is held only for a brief dict copy — never
    across network I/O or file I/O, so it cannot cause deadlocks.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._color = "gray"
        self._label = "Starting"
        self._last_sync: Optional[str] = None
        self._files_mirrored = 0
        self._bytes_mirrored = 0
        self._clock_msg: str = ""
        self._clock_msg_expires: float = 0.0

    def update(
        self,
        *,
        color: str,
        label: str,
        last_sync: Optional[str] = None,
        files_mirrored: Optional[int] = None,
        bytes_mirrored: Optional[int] = None,
    ) -> None:
        """Sync thread: atomically update status fields."""
        with self._lock:
            self._color = color
            self._label = label
            if last_sync is not None:
                self._last_sync = last_sync
            if files_mirrored is not None:
                self._files_mirrored = files_mirrored
            if bytes_mirrored is not None:
                self._bytes_mirrored = bytes_mirrored

    def set_clock_msg(self, msg: str, duration_s: float = 5.0) -> None:
        """Sync thread: set an ephemeral clock-sync status message."""
        with self._lock:
            self._clock_msg = msg
            self._clock_msg_expires = time.monotonic() + duration_s

    def get_clock_msg(self) -> Optional[str]:
        """UI thread: return the clock message if still within its display window."""
        with self._lock:
            if self._clock_msg and time.monotonic() < self._clock_msg_expires:
                return self._clock_msg
            return None

    def snapshot(self) -> tuple[str, str, Optional[str], int, int]:
        """UI thread: return a consistent (color, label, last_sync, files, bytes) tuple."""
        with self._lock:
            return (
                self._color,
                self._label,
                self._last_sync,
                self._files_mirrored,
                self._bytes_mirrored,
            )


# ---------------------------------------------------------------------------
# File-level helpers
# ---------------------------------------------------------------------------

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _cleanup_partials(mirror_root: Path) -> None:
    """
    Remove leftover .partial files from a previous run.

    Partial filenames have the form  <target_name>.<sha256_first_8>.partial
    so we can identify the matching target and verify integrity without the
    manifest.  Two cases arise — both result in the .partial being deleted
    and the target file being left untouched:

    1. Orphan partial — download was incomplete or killed mid-write.
       Target does not exist or its sha256 doesn't match the embedded prefix.
       Action: delete .partial.

    2. Completed partial — download finished and sha256 matched, but the
       process crashed before the atomic rename removed the .partial.
       Target exists and its sha256 prefix matches the embedded prefix.
       Action: delete .partial (target is already correct).
    """
    if not mirror_root.exists():
        return
    for partial in list(mirror_root.rglob("*.partial")):
        stem = partial.name[: -len(".partial")]          # "IMG_001.png.abcd1234"
        name_and_prefix = stem.rsplit(".", 1)
        if len(name_and_prefix) == 2:
            target_name, expected_prefix = name_and_prefix
            target = partial.parent / target_name
            if target.exists() and len(expected_prefix) == 8:
                try:
                    if _sha256_file(target)[:8] == expected_prefix:
                        log.info(
                            "Startup cleanup: completed partial, target intact — %s",
                            partial.name,
                        )
                        partial.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
        log.info("Startup cleanup: orphan partial — %s", partial.name)
        partial.unlink(missing_ok=True)


def _download_file(url: str, dest: Path, expected_sha256: str, token: str) -> bool:
    """
    Download url to dest, verifying sha256 before committing the file.

    The temp file is named  <dest.name>.<sha256_first_8>.partial  so that:
    - A partial download never corrupts a previously good file at dest.
    - _cleanup_partials() can identify and safely remove leftovers on restart.

    Returns True on success.  On any failure the .partial is removed and the
    caller should retry on the next tick.
    """
    partial = dest.parent / f"{dest.name}.{expected_sha256[:8]}.partial"
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        resp = requests.get(
            url,
            headers={"X-Auth-Token": token},
            stream=True,
            timeout=(10, 120),   # (connect_timeout, read_between_chunks_timeout)
        )
        resp.raise_for_status()
        h = hashlib.sha256()
        with open(partial, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=65536):
                fh.write(chunk)
                h.update(chunk)
        actual = h.hexdigest()
        if actual != expected_sha256:
            partial.unlink(missing_ok=True)
            log.warning(
                "SHA256 mismatch for %s: expected %.12s… got %.12s…",
                dest.name, expected_sha256, actual,
            )
            return False
        partial.replace(dest)    # os.replace — atomic on Windows
        return True
    except Exception as exc:
        partial.unlink(missing_ok=True)
        log.warning("Download failed for %s: %s", dest, exc)
        return False


def _ack_file(
    pi_url: str, endpoint: str, relative_path: str, sha256: str, token: str
) -> bool:
    try:
        resp = requests.post(
            f"{pi_url}{endpoint}",
            json={"relative_path": relative_path, "sha256": sha256},
            headers={"X-Auth-Token": token},
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:
        log.warning("Ack failed for %s: %s", relative_path, exc)
        return False


# ---------------------------------------------------------------------------
# Sync agent
# ---------------------------------------------------------------------------

class SyncAgent(threading.Thread):
    """
    Background thread that polls /manifest and mirrors unacked files to disk.

    The sync thread communicates with the UI exclusively through SyncStatus.
    It MUST NOT touch any Tk widget — see module docstring.
    """

    def __init__(self, settings: object, status: SyncStatus) -> None:
        super().__init__(daemon=True, name="SyncAgent")
        self._lock = threading.Lock()
        self._settings = settings
        self.status = status
        self._stop = threading.Event()
        self._total_files = 0
        self._total_bytes = 0

    def update_settings(self, settings: object) -> None:
        """UI thread: swap in updated settings; takes effect on the next tick."""
        with self._lock:
            self._settings = settings

    def _get_settings(self) -> object:
        with self._lock:
            return self._settings

    def stop(self) -> None:
        self._stop.set()

    def _do_clock_sync(self, s: object) -> None:
        """POST /clock-sync and update the ephemeral status message. Never raises."""
        client_iso = datetime.now(timezone.utc).isoformat()
        try:
            resp = requests.post(
                f"{s.pi_url}/clock-sync",
                json={"client_iso": client_iso},
                headers={"X-Auth-Token": s.token},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            offset = data.get("offset_seconds", 0)
            if abs(offset) > 1:
                msg = f"Pi clock synced (offset: {offset:+d}s)"
            else:
                msg = "Pi clock OK"
            log.info("Clock sync: %s", msg)
            self.status.set_clock_msg(msg, duration_s=5.0)
        except Exception as exc:
            msg = f"Clock sync failed: {exc}"
            log.warning(msg)
            self.status.set_clock_msg(msg, duration_s=5.0)

    def run(self) -> None:
        s = self._get_settings()
        _cleanup_partials(Path(s.mirror_root))
        self._do_clock_sync(s)
        while not self._stop.is_set():
            s = self._get_settings()
            self._tick(s)
            self._stop.wait(s.poll_interval_s)

    # ------------------------------------------------------------------

    def _tick(self, s: object) -> None:
        self.status.update(color="yellow", label="Syncing…")
        try:
            resp = requests.get(
                f"{s.pi_url}/manifest",
                headers={"X-Auth-Token": s.token},
                timeout=15,
            )
            resp.raise_for_status()
            manifest = resp.json()
        except Exception as exc:
            log.warning("Manifest fetch failed: %s", exc)
            self.status.update(color="red", label="Pi unreachable")
            return

        mirror = Path(s.mirror_root)
        tick_files = tick_bytes = 0

        for entry in manifest.get("pictures", {}).get("files", []):
            f, b = self._sync_pictures(entry, s, mirror)
            tick_files += f
            tick_bytes += b

        for entry in manifest.get("videos", {}).get("files", []):
            f, b = self._sync_videos(entry, s, mirror)
            tick_files += f
            tick_bytes += b

        sessions = manifest.get("sessions", [])
        session_dirs, cond_dirs = _build_name_maps(sessions)

        for session_m in sessions:
            sid = session_m["session_id"]
            exp_dir = session_dirs.get(sid, sid)
            this_cond = cond_dirs.get(sid, {})
            for entry in session_m.get("files", []):
                f, b = self._sync_session(entry, sid, exp_dir, this_cond, s, mirror)
                tick_files += f
                tick_bytes += b

        self._total_files += tick_files
        self._total_bytes += tick_bytes
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.status.update(
            color="green",
            label="Synced",
            last_sync=now,
            files_mirrored=self._total_files,
            bytes_mirrored=self._total_bytes,
        )

    def _sync_pictures(
        self, entry: dict, s: object, mirror: Path
    ) -> tuple[int, int]:
        if entry.get("acked"):
            return 0, 0
        rel: str = entry.get("relative_path", "")
        sha256: str = entry.get("sha256") or ""
        if not sha256:
            log.warning("Picture entry missing sha256: %r — skipping", rel)
            return 0, 0

        # relative_path format: "YYYY-MM-DD/filename"
        local = mirror / _PICTURES_DIR / Path(rel)
        need_download = not (local.exists() and _sha256_file(local) == sha256)

        if need_download:
            parts = rel.split("/", 1)
            if len(parts) != 2:
                log.warning("Unexpected picture relative_path %r — skipping", rel)
                return 0, 0
            date, filename = parts
            url = f"{s.pi_url}/capture/free/files/{date}/{filename}"
            if not _download_file(url, local, sha256, s.token):
                return 0, 0

        size = local.stat().st_size if need_download else 0
        if _ack_file(s.pi_url, "/capture/free/files/ack", rel, sha256, s.token):
            return 1, size
        return 0, 0

    def _sync_videos(
        self, entry: dict, s: object, mirror: Path
    ) -> tuple[int, int]:
        if entry.get("acked"):
            return 0, 0
        rel: str = entry.get("relative_path", "")
        sha256: str = entry.get("sha256") or ""
        if not sha256:
            log.warning("Video entry missing sha256: %r — skipping", rel)
            return 0, 0

        # relative_path format: "YYYY-MM-DD/filename"
        local = mirror / _VIDEOS_DIR / Path(rel)
        need_download = not (local.exists() and _sha256_file(local) == sha256)

        if need_download:
            parts = rel.split("/", 1)
            if len(parts) != 2:
                log.warning("Unexpected video relative_path %r — skipping", rel)
                return 0, 0
            date, filename = parts
            url = f"{s.pi_url}/capture/free/videos/{date}/{filename}"
            if not _download_file(url, local, sha256, s.token):
                return 0, 0

        size = local.stat().st_size if need_download else 0
        if _ack_file(s.pi_url, "/capture/free/videos/ack", rel, sha256, s.token):
            return 1, size
        return 0, 0

    def _sync_session(
        self,
        entry: dict,
        sid: str,
        exp_dir: str,
        cond_dir_map: dict[str, str],
        s: object,
        mirror: Path,
    ) -> tuple[int, int]:
        if entry.get("acked"):
            return 0, 0
        rel: str = entry.get("relative_path", "")
        sha256: str = entry.get("sha256") or ""
        plate_id: str = entry.get("plate_id") or ""
        if not sha256 or not plate_id:
            log.warning(
                "Session %s entry %r missing sha256 or plate_id — skipping", sid, rel
            )
            return 0, 0

        parts = rel.split("/")
        if len(parts) < 3 or parts[0] != "plates":
            log.warning("Unexpected session relative_path %r — skipping", rel)
            return 0, 0
        filename = parts[-1]

        # Build friendly local path from manifest metadata when available,
        # falling back to sid/rel for legacy entries.
        condition_name = entry.get("condition_name") or ""
        plate_label = entry.get("plate_label") or ""
        if condition_name and plate_label:
            # plate_id format: "{condition_id}_{name}_{NN:02d}"
            # rsplit with maxsplit=2 reliably extracts condition_id regardless of
            # underscores within condition_id or name.
            cid = plate_id.rsplit("_", 2)[0] if "_" in plate_id else plate_id
            cond_dir = cond_dir_map.get(cid) or _sanitize(condition_name)
            local = mirror / "experiments" / exp_dir / cond_dir / _sanitize(plate_label) / filename
        else:
            local = mirror / _EXPERIMENTS_DIR / sid / Path(rel)

        need_download = not (local.exists() and _sha256_file(local) == sha256)

        if need_download:
            url = f"{s.pi_url}/sessions/{sid}/plates/{plate_id}/files/{filename}"
            if not _download_file(url, local, sha256, s.token):
                return 0, 0

        size = local.stat().st_size if need_download else 0
        if _ack_file(s.pi_url, f"/sessions/{sid}/files/ack", rel, sha256, s.token):
            return 1, size
        return 0, 0
