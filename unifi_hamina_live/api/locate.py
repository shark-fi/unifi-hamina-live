"""Sensor ingest and located transmitters.

The only endpoint in this service that accepts data. Everything else projects a
snapshot the collector fetched, and the browser extension states the guarantee
plainly: "Read-only — GETs only, no writes, ever." That stops being true here,
which is why ingest is off unless switched on, refuses to run without a token,
and lives on its own router rather than being folded into the neutral API.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from ..config import Settings
from ..deps import settings, snapshot
from ..models import Snapshot

log = logging.getLogger("unifi_hamina_live.locate")

# No prefix: sensors post to <collector>/report, matching the standalone
# locator they already speak to. Pointing rssi_sensor.py at the bridge is then
# a change of --collector and nothing else.
router = APIRouter(tags=["sensors"])


def _service(request: Request):
    svc = getattr(request.app.state, "locate", None)
    if svc is None:
        raise HTTPException(status_code=404, detail={
            "error": "sensor support is off. Set SENSORS_ENABLED=true, "
                     "SENSOR_TOKEN, and SENSOR_CONFIG_PATH."})
    return svc


def require_sensor_token(
    x_sensor_token: str | None = Header(default=None),
    cfg: Settings = Depends(settings),
) -> None:
    """Reject a report that does not present the configured token.

    Unlike the Meraki facade, an empty token is NOT "run open". This endpoint
    mutates state that ends up drawn on a floor plan: anyone able to post here
    can put an AP wherever they like, and the result is indistinguishable from
    a real measurement. Startup refuses an empty token; this is the second
    line, for a token cleared at runtime.
    """
    expected = (cfg.sensor_token or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail={
            "error": "SENSOR_TOKEN is not set; refusing to accept reports"})
    if (x_sensor_token or "").strip() != expected:
        raise HTTPException(status_code=401, detail={"error": "bad sensor token"})


@router.post("/report", dependencies=[Depends(require_sensor_token)])
async def report(request: Request):
    """Accept one sensor's batch: ``{"sensor_id": ..., "detections": [...]}``."""
    svc = _service(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail={"error": "body is not JSON"})
    sensor_id = str((body or {}).get("sensor_id") or "")
    detections = (body or {}).get("detections") or []
    if not isinstance(detections, list):
        raise HTTPException(status_code=400,
                            detail={"error": "'detections' must be a list"})
    kept = svc.store.ingest(sensor_id, detections)
    if not kept and sensor_id not in svc.store.sensors:
        # Silently accepting an unknown sensor is how a typo becomes an
        # afternoon: reports flow, 200s come back, and no position ever appears.
        raise HTTPException(status_code=400, detail={
            "error": "unknown sensor_id %r; known: %s"
                     % (sensor_id, ", ".join(sorted(svc.store.sensors)))})
    return {"ok": True, "ingested": kept}


@router.get("/api/located")
def located(request: Request, snap: Snapshot = Depends(snapshot)):
    """Estimated positions, in the floor plan's own pixel coordinates.

    Deliberately NOT merged into /api/access-points. These are inferred from
    signal strength and carry metres of error; a placement read from the
    console is surveyed. Rendering them through the same field would make the
    two indistinguishable to every consumer downstream.
    """
    return _service(request).positions(snap)
