"""Endpoints for the scheduled OpenIntent refresh artifact."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from ..config import Settings
from ..deps import settings

router = APIRouter(prefix="/openintent", tags=["openintent"])


def _refresher(request: Request):
    refresher = getattr(request.app.state, "refresher", None)
    if refresher is None:
        raise HTTPException(
            status_code=404,
            detail="OpenIntent refresh is disabled (set OPENINTENT_REFRESH_ENABLED=true)",
        )
    return refresher


@router.get("/status")
def status(request: Request):
    r = _refresher(request)
    return {
        "enabled": True,
        "output_zip": str(r.output_zip),
        "zip_available": r.output_zip.exists(),
        "last_run": r.last_run,
    }


@router.post("/refresh")
async def refresh_now(request: Request):
    r = _refresher(request)
    return await r.run_once()


@router.get("/latest.zip")
def latest_zip(request: Request):
    r = _refresher(request)
    if not r.output_zip.exists():
        raise HTTPException(status_code=404, detail="no zip generated yet")
    return FileResponse(
        r.output_zip, media_type="application/zip", filename="hamina-live.zip"
    )
