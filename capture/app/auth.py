import secrets
from fastapi import Header, Query, HTTPException, Depends
from typing import Optional
from .config import settings


async def require_token(
    x_auth_token: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
) -> None:
    provided = x_auth_token or token
    if not provided or not secrets.compare_digest(provided, settings.TOKEN):
        raise HTTPException(status_code=401, detail="Unauthorized")
