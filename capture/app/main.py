import asyncio
import shutil
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .auth import require_token
from .camera import camera_manager
from .config import settings
from .models import CreatePlateRequest, CreateSessionRequest, Session
from . import sessions as session_store
from .routers import camera_ctrl, free_capture, manifest, plate_capture, preview, system

_STATUS_CACHE_TTL = 30.0
_unsynced_cache: Optional[dict] = None
_unsynced_cache_at: float = 0.0


def _compute_unsynced() -> dict:
    data_root = Path(settings.DATA_ROOT)
    count = 0
    total_bytes = 0
    oldest_age_s: Optional[float] = None
    now = time.time()

    for search_root in [data_root / settings.EXPERIMENTS_DIR, data_root / settings.PICTURES_DIR, data_root / settings.VIDEOS_DIR]:
        if not search_root.exists():
            continue
        for p in search_root.rglob("*"):
            if not p.is_file():
                continue
            if p.name.startswith("."):
                continue
            if ".thumbs" in p.parts:
                continue
            if p.suffix in {".sha256", ".acked"}:
                continue
            if (p.parent / (p.name + ".acked")).exists():
                continue
            st = p.stat()
            count += 1
            total_bytes += st.st_size
            age = now - st.st_mtime
            if oldest_age_s is None or age > oldest_age_s:
                oldest_age_s = age

    return {
        "unsynced_file_count": count,
        "unsynced_total_bytes": total_bytes,
        "oldest_unsynced_age_seconds": int(oldest_age_s) if oldest_age_s is not None else None,
    }


async def _get_unsynced() -> dict:
    global _unsynced_cache, _unsynced_cache_at
    if _unsynced_cache is None or (time.monotonic() - _unsynced_cache_at) > _STATUS_CACHE_TTL:
        _unsynced_cache = await asyncio.to_thread(_compute_unsynced)
        _unsynced_cache_at = time.monotonic()
    return _unsynced_cache


@asynccontextmanager
async def lifespan(app: FastAPI):
    camera_manager.start()
    yield
    camera_manager.stop()


app = FastAPI(title="C. elegans Imaging Station", lifespan=lifespan)

app.include_router(preview.router)
app.include_router(camera_ctrl.router)
app.include_router(free_capture.router)
app.include_router(plate_capture.router)
app.include_router(manifest.router)
app.include_router(system.router)


@app.get("/health")
async def health():
    return {"status": "ok", "schema_version": 1}


@app.get("/status", dependencies=[Depends(require_token)])
async def status():
    usage = shutil.disk_usage(settings.DATA_ROOT)
    unsynced = await _get_unsynced()

    last_run_at = None
    marker = Path(settings.DATA_ROOT) / ".retention-last-run"
    if marker.exists():
        last_run_at = datetime.fromtimestamp(
            marker.stat().st_mtime, tz=timezone.utc
        ).isoformat()

    return {
        "disk_free_gb": round(usage.free / 1e9, 2),
        "disk_total_gb": round(usage.total / 1e9, 2),
        "data_root": settings.DATA_ROOT,
        "camera_ready": camera_manager.ready,
        "ae_locked": camera_manager.ae_locked,
        **unsynced,
        "last_retention_run_at": last_run_at,
    }


@app.post("/sessions", dependencies=[Depends(require_token)])
async def create_session(req: CreateSessionRequest) -> Session:
    return session_store.create_session(req)


@app.get("/sessions", dependencies=[Depends(require_token)])
async def list_sessions() -> List[Session]:
    return session_store.list_sessions()


@app.get("/sessions/{session_id}", dependencies=[Depends(require_token)])
async def get_session(session_id: str) -> Session:
    return session_store.get_session(session_id)


@app.post("/sessions/{session_id}/plates", dependencies=[Depends(require_token)])
async def add_plate(session_id: str, req: CreatePlateRequest) -> Session:
    return session_store.add_plate(session_id, req)


@app.delete("/sessions/{session_id}", dependencies=[Depends(require_token)])
async def delete_session(session_id: str):
    await asyncio.to_thread(session_store.delete_session, session_id)
    return {"status": "trashed", "session_id": session_id}


@app.delete("/sessions/{session_id}/conditions/{condition_id}", dependencies=[Depends(require_token)])
async def delete_condition(session_id: str, condition_id: str,
                           name: Optional[str] = None) -> Session:
    return await asyncio.to_thread(
        session_store.delete_condition, session_id, condition_id, name
    )


class ConditionRef(BaseModel):
    condition_id: str
    name: str


class ReorderConditionsRequest(BaseModel):
    order: List[ConditionRef]


@app.post("/sessions/{session_id}/conditions/reorder", dependencies=[Depends(require_token)])
async def reorder_conditions(session_id: str, req: ReorderConditionsRequest) -> Session:
    order = [(c.condition_id, c.name) for c in req.order]
    return await asyncio.to_thread(session_store.reorder_conditions, session_id, order)


class RenameConditionRequest(BaseModel):
    strain_label: Optional[str] = None
    treatment_label: Optional[str] = None


@app.patch("/sessions/{session_id}/conditions/{condition_id}", dependencies=[Depends(require_token)])
async def rename_condition(session_id: str, condition_id: str, req: RenameConditionRequest,
                           name: str) -> Session:
    return await asyncio.to_thread(
        session_store.rename_condition, session_id, condition_id, name,
        req.strain_label, req.treatment_label,
    )


app.mount("/", StaticFiles(directory="app/static", html=True), name="static")
