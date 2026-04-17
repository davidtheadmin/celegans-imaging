import shutil
from typing import List

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles

from .auth import require_token
from .config import settings
from .models import CreatePlateRequest, CreateSessionRequest, Session
from . import sessions as session_store

app = FastAPI(title="C. elegans Imaging Station")


@app.get("/health")
async def health():
    return {"status": "ok", "schema_version": 1}


@app.get("/status", dependencies=[Depends(require_token)])
async def status():
    usage = shutil.disk_usage(settings.DATA_ROOT)
    return {
        "disk_free_gb": round(usage.free / 1e9, 2),
        "disk_total_gb": round(usage.total / 1e9, 2),
        "data_root": settings.DATA_ROOT,
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


app.mount("/", StaticFiles(directory="app/static", html=True), name="static")
