import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from .config import settings
from .models import CreatePlateRequest, CreateSessionRequest, Plate, Session


def _sessions_dir() -> Path:
    return Path(settings.DATA_ROOT) / "sessions"


def _session_dir(session_id: str) -> Path:
    return _sessions_dir() / session_id


def _manifest_path(session_id: str) -> Path:
    return _session_dir(session_id) / "session.json"


def _write_manifest(session: Session) -> None:
    path = _manifest_path(session.id)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(session.model_dump_json(indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read_manifest(session_id: str) -> Session:
    data = json.loads(_manifest_path(session_id).read_text(encoding="utf-8"))
    return Session.model_validate(data)


def list_sessions() -> List[Session]:
    base = _sessions_dir()
    if not base.exists():
        return []
    sessions = []
    for entry in sorted(base.iterdir()):
        manifest = entry / "session.json"
        if manifest.exists():
            try:
                sessions.append(_read_manifest(entry.name))
            except Exception:
                pass
    return sessions


def get_session(session_id: str) -> Session:
    from fastapi import HTTPException
    if not _manifest_path(session_id).exists():
        raise HTTPException(status_code=404, detail="Session not found")
    return _read_manifest(session_id)


def create_session(req: CreateSessionRequest) -> Session:
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%dT%H%M%S")
    hash_suffix = hashlib.sha256(f"{ts}{req.name}".encode()).hexdigest()[:6]
    session_id = f"{ts}_{hash_suffix}"

    session = Session(
        id=session_id,
        name=req.name,
        assay_mode=req.assay_mode,
        assay_config=req.assay_config,
        created_at=now.isoformat(),
        plates=[],
    )

    session_dir = _session_dir(session_id)
    (session_dir / "plates").mkdir(parents=True, exist_ok=True)
    _write_manifest(session)
    return session


def get_plate(session_id: str, plate_id: str):
    from fastapi import HTTPException
    from typing import Tuple
    session = get_session(session_id)
    for plate in session.plates:
        if plate.id == plate_id:
            return session, plate
    raise HTTPException(404, "Plate not found")


def get_plate_dir(session_id: str, folder_name: str) -> Path:
    return _session_dir(session_id) / "plates" / folder_name


def add_plate(session_id: str, req: CreatePlateRequest) -> Session:
    session = get_session(session_id)

    now = datetime.now(timezone.utc)
    plate_id = f"{req.condition_id}_{req.plate_number:02d}"

    plate = Plate(
        id=plate_id,
        condition_id=req.condition_id,
        name=req.name,
        plate_number=req.plate_number,
        created_at=now.isoformat(),
    )

    plate_dir = _session_dir(session_id) / "plates" / plate.folder_name
    plate_dir.mkdir(parents=True, exist_ok=True)

    session.plates.append(plate)
    _write_manifest(session)
    return session
