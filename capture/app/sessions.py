import hashlib
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

log = logging.getLogger(__name__)

from .config import settings
from .models import CreatePlateRequest, CreateSessionRequest, Plate, Session


def _sessions_dir() -> Path:
    return Path(settings.DATA_ROOT) / settings.EXPERIMENTS_DIR


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

    # Stamp the active spatial calibration (metadata only; no behaviour depends on
    # it yet). Tolerant of any failure — a missing calibration must not block
    # session creation.
    try:
        from .camera import camera_manager, FULL_W, VIDEO_W
        cal = camera_manager.get_calibrations()
        active_label = cal.get("active")
        if active_label:
            entry = next(
                (c for c in cal["calibrations"] if c["label"] == active_label), None
            )
            if entry:
                fov = float(entry["fov_cm"])
                session.calibration = {
                    "label": entry["label"],
                    "fov_cm": fov,
                    "um_per_px_full": fov * 10000 / FULL_W,
                    "um_per_px_video": fov * 10000 / VIDEO_W,
                    "stamped_at": now.isoformat(),
                }
    except Exception:
        log.debug("calibration stamping skipped", exc_info=True)

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
    from fastapi import HTTPException
    session = get_session(session_id)
    count = max(1, min(50, req.replicates))
    now = datetime.now(timezone.utc)

    for i in range(count):
        pnum = req.plate_number + i
        # Enforce uniqueness per (condition_id, name, plate_number)
        for existing in session.plates:
            if (existing.condition_id == req.condition_id
                    and existing.name == req.name
                    and existing.plate_number == pnum):
                raise HTTPException(
                    409,
                    f"Plate {req.condition_id}/{req.name} #{pnum:02d} already exists"
                )
        # New id format includes name to stay unique across conditions
        plate_id = f"{req.condition_id}_{req.name}_{pnum:02d}"
        plate = Plate(
            id=plate_id,
            condition_id=req.condition_id,
            name=req.name,
            condition_name=req.condition_name,
            plate_number=pnum,
            created_at=now.isoformat(),
        )
        plate_dir = _session_dir(session_id) / "plates" / plate.folder_name
        plate_dir.mkdir(parents=True, exist_ok=True)
        session.plates.append(plate)

    _write_manifest(session)
    return session


def delete_session(session_id: str) -> None:
    from fastapi import HTTPException
    if not _manifest_path(session_id).exists():
        raise HTTPException(404, "Session not found")
    session_dir = _session_dir(session_id)
    trash_dest = Path(settings.DATA_ROOT) / ".trash" / settings.EXPERIMENTS_DIR / session_id
    if trash_dest.exists():
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        trash_dest = trash_dest.with_name(f"{session_id}_{ts}")
    trash_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(session_dir), str(trash_dest))


def delete_condition(session_id: str, condition_id: str,
                     name: Optional[str] = None) -> Session:
    from fastapi import HTTPException
    session = get_session(session_id)

    def matches(p):
        return p.condition_id == condition_id and (name is None or p.name == name)

    targets = [p for p in session.plates if matches(p)]
    if not targets:
        raise HTTPException(404, "Condition not found or has no plates")
    if name is None:
        distinct_names = {p.name for p in targets}
        if len(distinct_names) > 1:
            log.warning(
                "delete_condition called without name for condition_id %r in "
                "session %s; deleting %d distinct names: %s",
                condition_id, session_id, len(distinct_names), sorted(distinct_names),
            )
    for plate in targets:
        plate_dir = get_plate_dir(session_id, plate.folder_name)
        trash_dest = (
            Path(settings.DATA_ROOT) / ".trash" / settings.EXPERIMENTS_DIR
            / session_id / "plates" / plate.folder_name
        )
        trash_dest.parent.mkdir(parents=True, exist_ok=True)
        if trash_dest.exists():
            ts = datetime.now().strftime("%Y%m%dT%H%M%S")
            trash_dest = trash_dest.with_name(f"{trash_dest.name}_{ts}")
        if plate_dir.exists():
            shutil.move(str(plate_dir), str(trash_dest))
    session.plates = [p for p in session.plates if not matches(p)]
    _write_manifest(session)
    return session


def rename_condition(session_id: str, condition_id: str, name: str,
                     strain_label, treatment_label) -> Session:
    """Update display labels across every plate matching (condition_id, name).
    If a label is empty/None, set the field to None (reverts to default)."""
    from fastapi import HTTPException
    session = get_session(session_id)

    targets = [p for p in session.plates
               if p.condition_id == condition_id and p.name == name]
    if not targets:
        raise HTTPException(404, "Condition not found or has no plates")

    new_strain = strain_label or None
    new_treatment = treatment_label or None
    for plate in targets:
        plate.condition_name = new_strain
        plate.treatment_label = new_treatment

    _write_manifest(session)
    return session


def reorder_conditions(session_id: str, order) -> Session:
    """`order` is a list of (condition_id, name) tuples in desired order.
    Reorder session.plates to group plates by condition in that order;
    within each condition, preserve plate_number ordering. Plates whose
    (condition_id, name) isn't in `order` go at the end in current
    relative order (be tolerant — never drop data)."""
    session = get_session(session_id)

    order_index = {(cid, name): i for i, (cid, name) in enumerate(order)}

    # Group plates by condition, recording current first-seen order for fallback.
    groups: dict = {}
    group_order: list = []
    for plate in session.plates:
        key = (plate.condition_id, plate.name)
        if key not in groups:
            groups[key] = []
            group_order.append(key)
        groups[key].append(plate)

    def sort_key(key):
        # Known conditions first, in `order` position; unknown ones after,
        # in current relative order.
        if key in order_index:
            return (0, order_index[key])
        return (1, group_order.index(key))

    new_plates = []
    for key in sorted(group_order, key=sort_key):
        new_plates.extend(sorted(groups[key], key=lambda p: p.plate_number))

    session.plates = new_plates
    _write_manifest(session)
    return session


def delete_plate(session_id: str, plate_id: str) -> Session:
    from fastapi import HTTPException
    session = get_session(session_id)
    plate = next((p for p in session.plates if p.id == plate_id), None)
    if plate is None:
        raise HTTPException(404, "Plate not found")

    plate_dir = get_plate_dir(session_id, plate.folder_name)
    trash_dest = (
        Path(settings.DATA_ROOT) / ".trash" / settings.EXPERIMENTS_DIR / session_id / "plates" / plate.folder_name
    )
    trash_dest.parent.mkdir(parents=True, exist_ok=True)
    if trash_dest.exists():
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        trash_dest = trash_dest.with_name(f"{trash_dest.name}_{ts}")
    if plate_dir.exists():
        shutil.move(str(plate_dir), str(trash_dest))

    session.plates = [p for p in session.plates if p.id != plate_id]
    _write_manifest(session)
    return session
