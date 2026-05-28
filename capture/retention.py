"""
Retention daemon: free disk by permanently deleting acked files when the card
is low or files age out, and auto-purge the .trash recycle bin.
Run as: python -m capture.retention [--dry-run] [--verbose]

Safety model: every file this module deletes is either (a) acked — a verified
copy exists on the laptop — or (b) already in .trash because the user deleted
it. Both are safe to remove outright.
"""
import argparse
import logging
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

DATA_ROOT = Path(os.environ.get("CELEGANS_DATA_ROOT", "/home/pi/celegans-data"))
# Must match EXPERIMENTS_DIR / PICTURES_DIR / VIDEOS_DIR in capture/app/config.py
EXPERIMENTS_DIR = "experiments"
PICTURES_DIR = "pictures"
VIDEOS_DIR = "videos"
MIN_FREE_GB = float(os.environ.get("CELEGANS_RETENTION_MIN_FREE_GB", "5"))
TARGET_FREE_GB = float(os.environ.get("CELEGANS_RETENTION_TARGET_FREE_GB", "10"))
MAX_AGE_DAYS = float(os.environ.get("CELEGANS_RETENTION_MAX_AGE_DAYS", "30"))
TRASH_MAX_AGE_DAYS = float(os.environ.get("CELEGANS_RETENTION_TRASH_MAX_AGE_DAYS", "7"))


def _disk_free_gb(data_root: Path = DATA_ROOT) -> float:
    return shutil.disk_usage(str(data_root)).free / 1e9


def _is_data_file(p: Path) -> bool:
    if not p.is_file():
        return False
    if p.name.startswith("."):
        return False
    if ".thumbs" in p.parts:
        return False
    if p.suffix in {".sha256", ".acked"}:
        return False
    return True


def _acked_data_files(data_root: Path, verbose: bool = False):
    """Return [(path, acked_mtime, file_mtime)] for every data file under
    experiments/pictures/videos that has a .acked sidecar (verified copy on the
    laptop → safe to delete). Sorted by path for stable, readable logging."""
    out = []
    for sub in (EXPERIMENTS_DIR, PICTURES_DIR, VIDEOS_DIR):
        root = data_root / sub
        if not root.exists():
            continue
        for p in sorted(root.rglob("*")):
            if not _is_data_file(p):
                continue
            acked_path = p.parent / (p.name + ".acked")
            if not acked_path.exists():
                if verbose:
                    log.info("  SKIP (no .acked) %s", p.relative_to(data_root))
                continue
            acked_mtime = datetime.fromtimestamp(acked_path.stat().st_mtime, tz=timezone.utc)
            file_mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
            out.append((p, acked_mtime, file_mtime))
    return out


def _delete_file(src: Path) -> int:
    """Permanently remove the data file plus its .sha256, .acked and thumbnail.
    Returns the data file's size in bytes (sidecars are negligible)."""
    size = src.stat().st_size if src.exists() else 0
    for suffix in ("", ".sha256", ".acked"):
        p = src if suffix == "" else src.parent / (src.name + suffix)
        if p.exists():
            try:
                p.unlink()
            except OSError as e:
                log.warning("Could not remove %s: %s", p, e)
    thumb = src.parent / ".thumbs" / (src.stem + ".jpg")
    if thumb.exists():
        try:
            thumb.unlink()
        except OSError:
            pass
    return size


def _prune_empty_dirs(root: Path) -> None:
    """Remove empty subdirectories under root (deepest first); keep root itself."""
    if not root.exists():
        return
    dirs = [p for p in root.rglob("*") if p.is_dir()]
    dirs.sort(key=lambda p: len(p.parts), reverse=True)
    for d in dirs:
        try:
            if not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass


def purge_expired_trash(data_root: Path, max_age_days: float, *,
                        dry_run: bool = False, verbose: bool = False):
    """Permanently remove anything under <data_root>/.trash whose mtime is older
    than max_age_days, then prune empty dirs. Returns (files_removed, bytes_freed).
    Trash mtimes are stamped to deletion time by capture_ops.trash_file."""
    data_root = Path(data_root)
    trash_root = data_root / ".trash"
    if not trash_root.exists():
        return 0, 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    count = 0
    freed = 0
    for p in sorted(trash_root.rglob("*")):
        if not p.is_file():
            continue
        mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
        if mtime > cutoff:
            if verbose:
                log.info("  KEEP (trash within %.0fd) %s", max_age_days, p.relative_to(data_root))
            continue
        size = p.stat().st_size
        log.info(
            "%s [trash expired] %s (%.1f MB, mtime %s)",
            "Would purge" if dry_run else "Purging",
            p.relative_to(data_root),
            size / 1e6,
            mtime.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        if not dry_run:
            try:
                p.unlink()
            except OSError as e:
                log.warning("Could not remove %s: %s", p, e)
                continue
        count += 1
        freed += size

    if not dry_run:
        _prune_empty_dirs(trash_root)
    return count, freed


def reclaim(target_free_gb: float, *, data_root: Path = DATA_ROOT,
            dry_run: bool = False, verbose: bool = False):
    """Free disk by deleting (oldest-first within each tier), stopping as soon as
    free disk reaches target_free_gb. Returns (files_removed, bytes_freed).

        tier 0: acked data files older than MAX_AGE_DAYS — always removed
                (age violations), regardless of disk state.
        tier 1: ALL remaining .trash contents (user already chose to delete these).
        tier 2: remaining acked data files, oldest-acked-first.

    In --dry-run nothing is deleted, so free disk never rises; the listing then
    shows every file that *would* be removed to reach the target.
    """
    data_root = Path(data_root)
    age_cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)

    files_removed = 0
    bytes_freed = 0

    acked = _acked_data_files(data_root, verbose=verbose)
    age_violations = sorted((t for t in acked if t[2] <= age_cutoff), key=lambda t: t[1])
    young_acked = sorted((t for t in acked if t[2] > age_cutoff), key=lambda t: t[1])

    # --- tier 0: age violations — always removed, regardless of disk -------
    for path, acked_mtime, _file_mtime in age_violations:
        size = path.stat().st_size
        log.info(
            "%s [tier0 max-age] %s (%.1f MB, acked %s)",
            "Would remove" if dry_run else "Removing",
            path.relative_to(data_root), size / 1e6,
            acked_mtime.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        bytes_freed += size if dry_run else _delete_file(path)
        files_removed += 1

    if _disk_free_gb(data_root) >= target_free_gb:
        return files_removed, bytes_freed

    # --- tier 1: ALL remaining .trash contents, oldest-first ---------------
    trash_root = data_root / ".trash"
    if trash_root.exists():
        trash_files = sorted((p for p in trash_root.rglob("*") if p.is_file()),
                             key=lambda p: p.stat().st_mtime)
        for p in trash_files:
            if _disk_free_gb(data_root) >= target_free_gb:
                break
            size = p.stat().st_size
            log.info(
                "%s [tier1 trash] %s (%.1f MB)",
                "Would remove" if dry_run else "Removing",
                p.relative_to(data_root), size / 1e6,
            )
            if not dry_run:
                try:
                    p.unlink()
                except OSError as e:
                    log.warning("Could not remove %s: %s", p, e)
                    continue
            bytes_freed += size
            files_removed += 1
        if not dry_run:
            _prune_empty_dirs(trash_root)

    if _disk_free_gb(data_root) >= target_free_gb:
        return files_removed, bytes_freed

    # --- tier 2: remaining acked data files, oldest-acked-first ------------
    for path, acked_mtime, _file_mtime in young_acked:
        if _disk_free_gb(data_root) >= target_free_gb:
            break
        size = path.stat().st_size
        log.info(
            "%s [tier2 acked] %s (%.1f MB, acked %s)",
            "Would remove" if dry_run else "Removing",
            path.relative_to(data_root), size / 1e6,
            acked_mtime.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        bytes_freed += size if dry_run else _delete_file(path)
        files_removed += 1

    return files_removed, bytes_freed


def main():
    parser = argparse.ArgumentParser(description="Retention daemon for celegans-imaging")
    parser.add_argument("--dry-run", action="store_true", help="List what would be removed, delete nothing")
    parser.add_argument("--verbose", action="store_true", help="Log every file examined with reasoning")
    args = parser.parse_args()
    dry_run = args.dry_run
    verbose = args.verbose

    # 1. Always purge the recycle bin of anything past its lifetime first.
    purged_n, purged_b = purge_expired_trash(
        DATA_ROOT, TRASH_MAX_AGE_DAYS, dry_run=dry_run, verbose=verbose
    )
    if purged_n:
        log.info("Recycle bin: %s %d expired file(s) (~%.1f MB, older than %gd)",
                 "would purge" if dry_run else "purged", purged_n, purged_b / 1e6, TRASH_MAX_AGE_DAYS)

    # 2. Assess actual disk + age violations.
    free_before = _disk_free_gb(DATA_ROOT)
    log.info(
        "Disk free: %.2f GB (min=%.1f, target=%.1f, max_age=%gd, trash_max_age=%gd)",
        free_before, MIN_FREE_GB, TARGET_FREE_GB, MAX_AGE_DAYS, TRASH_MAX_AGE_DAYS,
    )

    age_cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    has_age_violation = any(fm <= age_cutoff for _, _, fm in _acked_data_files(DATA_ROOT, verbose=verbose))

    if free_before >= MIN_FREE_GB and not has_age_violation:
        log.info("Nothing to do — disk %.2f GB free, no files exceed %g days", free_before, MAX_AGE_DAYS)
        _touch_last_run(dry_run)
        return

    if dry_run:
        log.info("DRY RUN — nothing will be deleted")

    # Reclaim to whichever threshold is higher (handles MIN > TARGET in testing).
    files_removed, bytes_freed = reclaim(
        max(TARGET_FREE_GB, MIN_FREE_GB), data_root=DATA_ROOT, dry_run=dry_run, verbose=verbose
    )
    free_after = _disk_free_gb(DATA_ROOT)

    if dry_run:
        log.info(
            "Dry run complete — would remove %d file(s) (~%.1f MB); disk free unchanged at %.2f GB",
            files_removed, bytes_freed / 1e6, free_after,
        )
    else:
        log.info(
            "Done — removed %d file(s), ~%.1f MB; disk free %.2f GB -> %.2f GB",
            files_removed, bytes_freed / 1e6, free_before, free_after,
        )

    _touch_last_run(dry_run)


def _touch_last_run(dry_run: bool) -> None:
    if not dry_run:
        marker = DATA_ROOT / ".retention-last-run"
        try:
            marker.touch()
        except OSError as e:
            log.warning("Could not touch %s: %s", marker, e)


if __name__ == "__main__":
    main()
