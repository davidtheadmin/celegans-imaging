import subprocess
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from ..auth import require_token

router = APIRouter()

_FIVE_YEARS_S = 5 * 365.25 * 24 * 3600


class ClockSyncRequest(BaseModel):
    client_iso: str


@router.post("/clock-sync", dependencies=[Depends(require_token)])
async def clock_sync(req: ClockSyncRequest):
    try:
        client_time = datetime.fromisoformat(req.client_iso)
        if client_time.tzinfo is None:
            client_time = client_time.replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(400, f"Invalid ISO timestamp: {req.client_iso!r}")

    pi_time = datetime.now(timezone.utc)
    offset_s = (client_time - pi_time).total_seconds()

    if abs(offset_s) > _FIVE_YEARS_S:
        raise HTTPException(
            400,
            f"Supplied time deviates {offset_s / 86400:.0f} days from Pi clock — "
            "refusing to set (likely a wrong client timezone or epoch)."
        )

    old_time_iso = pi_time.isoformat()
    # Reformat to a form that `date -s` accepts unambiguously
    date_str = client_time.strftime("%Y-%m-%dT%H:%M:%S%z")

    result = subprocess.run(
        ["sudo", "-n", "date", "-s", date_str],
        capture_output=True,
        timeout=10,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        hint = (
            "Add to /etc/sudoers.d/celegans-date:\n"
            "  pi ALL=(ALL) NOPASSWD: /bin/date"
            if "sudoers" in stderr.lower() or "password" in stderr.lower()
            else ""
        )
        raise HTTPException(
            500,
            f"date -s failed (rc={result.returncode}): {stderr}"
            + (f"\n{hint}" if hint else ""),
        )

    return {
        "old_time": old_time_iso,
        "new_time": req.client_iso,
        "offset_seconds": int(offset_s),
    }


@router.post("/shutdown", dependencies=[Depends(require_token)], status_code=202)
async def shutdown():
    subprocess.Popen(["sudo", "/sbin/shutdown", "-h", "now"])
    return Response(status_code=202)
