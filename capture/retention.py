"""
Retention daemon: evict acked files when disk is low or files age out.
Run as: python -m capture.retention [--dry-run]
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
GRACE_HOURS = float(os.environ.get("CELEGANS_RETENTION_GRACE_HOURS", "1"))
MIN_FREE_GB = float(os.environ.get("CELEGANS_RETENTION_MIN_FREE_GB", "5"))
TARGET_FREE_GB = float(os.environ.get("CELEGANS_RETENTION_TARGET_FREE_GB", "10"))
MAX_AGE_DAYS = float(os.environ.get("CELEGANS_RETENTION_MAX_AGE_DAYS", "30"))


def _disk_free_gb() -> float:
    return shutil.disk_usage(str(DATA_ROOT)).free / 1e9


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


def _collect_eligible(now: datetime):
    """Return list of (file_path, acked_at, reason) for trashable files."""
    grace_cutoff = now - timedelta(hours=GRACE_HOURS)
    age_cutoff = now - timedelta(days=MAX_AGE_DAYS)
    eligible = []

    for search_root in [DATA_ROOT / "sessions", DATA_ROOT / "freecapture"]:
        if not search_root.exists():
            continue
        for p in search_root.rglob("*"):
            if not _is_data_file(p):
                continue
            acked_path = p.parent / (p.name + ".acked")
            if not acked_path.exists():
                continue
            acked_mtime = datetime.fromtimestamp(acked_path.stat().st_mtime, tz=timezone.utc)
            file_mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
            if acked_mtime <= grace_cutoff:
                eligible.append((p, acked_mtime, "space pressure"))
            elif file_mtime <= age_cutoff:
                eligible.append((p, acked_mtime, "max age"))

    eligible.sort(key=lambda x: x[1])
    return eligible


def _trash_file(src: Path, dry_run: bool) -> int:
    """Move src + siblings (.sha256, .acked, thumbnail) to .trash/. Returns bytes freed."""
    rel = src.relative_to(DATA_ROOT)
    dest = DATA_ROOT / ".trash" / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        dest = dest.with_name(f"{dest.stem}_{ts}{dest.suffix}")

    size = src.stat().st_size
    if not dry_run:
        shutil.move(str(src), str(dest))

    for suffix in (".sha256", ".acked"):
        sib = src.parent / (src.name + suffix)
        if sib.exists():
            sib_dest = dest.parent / (dest.name + suffix)
            if not dry_run:
                shutil.move(str(sib), str(sib_dest))

    thumb_src = src.parent / ".thumbs" / (src.stem + ".jpg")
    if thumb_src.exists():
        thumb_dest = dest.parent / ".thumbs" / (dest.stem + ".jpg")
        if not dry_run:
            thumb_dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(thumb_src), str(thumb_dest))
            except OSError:
                pass

    return size


def main():
    parser = argparse.ArgumentParser(description="Retention daemon for celegans-imaging")
    parser.add_argument("--dry-run", action="store_true", help="List eligible files, move nothing")
    args = parser.parse_args()
    dry_run = args.dry_run

    now = datetime.now(timezone.utc)
    free_gb = _disk_free_gb()
    log.info("Disk free: %.2f GB (min=%.1f, target=%.1f)", free_gb, MIN_FREE_GB, TARGET_FREE_GB)

    # Collect eligible before early-exit check so we can log age violations
    eligible = _collect_eligible(now)
    has_age_violation = any(r == "max age" for _, _, r in eligible)

    if free_gb >= MIN_FREE_GB and not has_age_violation:
        log.info("Nothing to do — disk %.2f GB free, no files exceed %g days", free_gb, MAX_AGE_DAYS)
        _touch_last_run(dry_run)
        return

    if dry_run:
        log.info("DRY RUN — no files will be moved")

    trashed_count = 0
    trashed_bytes = 0

    for file_path, acked_at, reason in eligible:
        current_free = _disk_free_gb()
        if reason != "max age" and current_free >= TARGET_FREE_GB:
            break
        size = file_path.stat().st_size
        rel = file_path.relative_to(DATA_ROOT)
        log.info(
            "%s %s (%.1f MB, acked %s, reason: %s)",
            "Would trash" if dry_run else "Trashing",
            rel,
            size / 1e6,
            acked_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            reason,
        )
        if not dry_run:
            trashed_bytes += _trash_file(file_path, dry_run=False)
        else:
            trashed_bytes += size
        trashed_count += 1

    if not dry_run:
        final_free = _disk_free_gb()
        log.info(
            "Done — %d file(s) trashed, %.1f MB freed, disk now %.2f GB free",
            trashed_count, trashed_bytes / 1e6, final_free,
        )
    else:
        log.info(
            "Dry run complete — %d file(s) eligible, %.1f MB would be freed",
            trashed_count, trashed_bytes / 1e6,
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
