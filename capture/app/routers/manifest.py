import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..auth import require_token
from ..config import settings
from .. import capture_ops
from .. import sessions as session_store

router = APIRouter()

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _is_manifest_file(p: Path) -> bool:
    if not p.is_file():
        return False
    if p.name.startswith("."):
        return False
    if ".thumbs" in p.parts:
        return False
    if p.suffix in {".sha256", ".acked"}:
        return False
    return True


def _file_entry(path: Path, relative_path: str) -> dict:
    stat = path.stat()
    sha256 = capture_ops.read_sha256(path)
    acked_path = path.parent / (path.name + ".acked")
    if acked_path.exists():
        acked = True
        acked_at = datetime.fromtimestamp(
            acked_path.stat().st_mtime, tz=timezone.utc
        ).isoformat()
    else:
        acked = False
        acked_at = None
    return {
        "relative_path": relative_path,
        "size_bytes": stat.st_size,
        "sha256": sha256,
        "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        "acked": acked,
        "acked_at": acked_at,
    }


def _build_session_manifest(session_id: str) -> dict:
    session = session_store.get_session(session_id)
    session_dir = Path(settings.DATA_ROOT) / settings.EXPERIMENTS_DIR / session_id
    files = []
    for plate in session.plates:
        plate_dir = session_dir / "plates" / plate.folder_name
        if not plate_dir.exists():
            continue
        for p in sorted(plate_dir.rglob("*")):
            if _is_manifest_file(p):
                rel = str(p.relative_to(session_dir)).replace("\\", "/")
                entry = _file_entry(p, rel)
                entry["plate_id"] = plate.id
                files.append(entry)
    total_bytes = sum(f["size_bytes"] for f in files)
    return {
        "session_id": session_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
        "total_files": len(files),
        "total_bytes": total_bytes,
    }


def _build_free_manifest(date_filter: Optional[str] = None) -> dict:
    free_base = Path(settings.DATA_ROOT) / settings.PICTURES_DIR
    files = []
    if date_filter:
        dirs = [free_base / date_filter] if (free_base / date_filter).is_dir() else []
    else:
        dirs = sorted(free_base.iterdir()) if free_base.exists() else []
    for date_dir in dirs:
        if not date_dir.is_dir() or date_dir.name.startswith("."):
            continue
        for p in sorted(date_dir.iterdir()):
            if _is_manifest_file(p):
                rel = str(p.relative_to(free_base)).replace("\\", "/")
                files.append(_file_entry(p, rel))
    total_bytes = sum(f["size_bytes"] for f in files)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
        "total_files": len(files),
        "total_bytes": total_bytes,
    }


@router.get("/sessions/{session_id}/manifest", dependencies=[Depends(require_token)])
async def session_manifest(session_id: str):
    return await asyncio.to_thread(_build_session_manifest, session_id)


@router.get("/capture/free/manifest", dependencies=[Depends(require_token)])
async def free_manifest(date: Optional[str] = Query(default=None)):
    if date is not None and not _DATE_RE.match(date):
        raise HTTPException(400, "Invalid date format, expected YYYY-MM-DD")
    return await asyncio.to_thread(_build_free_manifest, date)


@router.get("/manifest", dependencies=[Depends(require_token)])
async def top_manifest():
    sessions = session_store.list_sessions()
    session_manifests, free = await asyncio.gather(
        asyncio.gather(*[
            asyncio.to_thread(_build_session_manifest, s.id) for s in sessions
        ]),
        asyncio.to_thread(_build_free_manifest, None),
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sessions": list(session_manifests),
        "freecapture": free,
    }


# ------------------------------------------------------------------
# Ack helpers
# ------------------------------------------------------------------

class AckRequest(BaseModel):
    relative_path: str
    sha256: str


def _do_ack(file_path: Path) -> dict:
    """Write .acked marker atomically. Returns {acked, acked_at}."""
    acked_path = file_path.parent / (file_path.name + ".acked")
    acked_path.touch()
    acked_at = datetime.fromtimestamp(
        acked_path.stat().st_mtime, tz=timezone.utc
    ).isoformat()
    return {"acked": True, "acked_at": acked_at}


def _resolve_and_ack(base_dir: Path, relative_path: str, supplied_sha256: str) -> dict:
    # Reject path traversal
    rel = Path(relative_path)
    if rel.is_absolute() or ".." in rel.parts:
        raise HTTPException(400, "Invalid relative_path")
    file_path = (base_dir / rel).resolve()
    if not str(file_path).startswith(str(base_dir.resolve())):
        raise HTTPException(403, "Forbidden")
    if not file_path.is_file():
        raise HTTPException(404, "File not found")
    actual = capture_ops.read_sha256(file_path)
    if actual != supplied_sha256.lower():
        raise HTTPException(
            409,
            f"SHA256 mismatch: supplied {supplied_sha256[:12]}… does not match "
            f"stored {(actual or '')[:12]}…",
        )
    return _do_ack(file_path)


@router.post("/sessions/{session_id}/files/ack", dependencies=[Depends(require_token)])
async def ack_session_file(session_id: str, req: AckRequest):
    # Validate session exists
    session_store.get_session(session_id)
    base = Path(settings.DATA_ROOT) / settings.EXPERIMENTS_DIR / session_id
    return await asyncio.to_thread(
        _resolve_and_ack, base, req.relative_path, req.sha256
    )


@router.post("/capture/free/files/ack", dependencies=[Depends(require_token)])
async def ack_free_file(req: AckRequest):
    base = Path(settings.DATA_ROOT) / settings.PICTURES_DIR
    return await asyncio.to_thread(
        _resolve_and_ack, base, req.relative_path, req.sha256
    )
