"""Vendor-neutral REST API — clean JSON projections of the live snapshot.

Unauthenticated by design (intended to sit behind your own network / the live
dashboard). Use the Meraki-compatible facade under /api/v1 for API-key access.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Query

from ..deps import collector, snapshot
from ..models import AccessPoint, Client, FloorPlan, Site, Snapshot
from ..unifi.collector import Collector

router = APIRouter(prefix="/api", tags=["neutral"])


@router.get("/health")
def health(snap: Snapshot = Depends(snapshot)):
    age = time.time() - snap.generated_at if snap.generated_at else None
    return {
        "ok": snap.ok,
        "error": snap.error,
        "generated_at": snap.generated_at,
        "age_seconds": round(age, 1) if age is not None else None,
        "sites": len(snap.sites),
        "access_points": len(snap.access_points),
        "clients": len(snap.clients),
    }


@router.get("/sites", response_model=list[Site])
def sites(snap: Snapshot = Depends(snapshot)):
    return snap.sites


@router.get("/access-points", response_model=list[AccessPoint])
def access_points(
    site: str | None = Query(default=None, description="Filter by UniFi site id."),
    snap: Snapshot = Depends(snapshot),
):
    if site:
        return snap.aps_for_site(site)
    return snap.access_points


@router.get("/access-points/{serial}", response_model=AccessPoint)
def access_point(serial: str, snap: Snapshot = Depends(snapshot)):
    ap = snap.ap_by_serial(serial)
    if ap is None:
        raise HTTPException(status_code=404, detail="access point not found")
    return ap


@router.get("/floorplans", response_model=list[FloorPlan])
def floorplans(
    site: str | None = Query(default=None, description="Filter by UniFi site id."),
    snap: Snapshot = Depends(snapshot),
):
    """Floor plans discovered from classic Maps / InnerSpace. AP positions
    (floorplan_id, x, y) live on each access point — see /api/access-points."""
    if site:
        return snap.floorplans_for_site(site)
    return snap.floorplans


@router.get("/clients", response_model=list[Client])
def clients(
    site: str | None = Query(default=None),
    ap_serial: str | None = Query(default=None, description="Filter by AP serial."),
    snap: Snapshot = Depends(snapshot),
):
    result = snap.clients
    if site:
        result = [c for c in result if c.site_id == site]
    if ap_serial:
        result = [c for c in result if c.ap_serial == ap_serial]
    return result


@router.get("/summary")
def summary(snap: Snapshot = Depends(snapshot)):
    """Per-AP connected-client counts — the 'devices connected to an AP' view."""
    rows = []
    for ap in snap.access_points:
        rows.append(
            {
                "site_id": ap.site_id,
                "name": ap.name,
                "serial": ap.serial,
                "mac": ap.mac,
                "model": ap.model,
                "online": ap.online,
                "num_clients": ap.num_clients,
                "floorplan_id": ap.floorplan_id,
                "x": ap.x,
                "y": ap.y,
                "radios": [
                    {
                        "band": r.band,
                        "channel": r.channel,
                        "channel_width_mhz": r.channel_width_mhz,
                        "tx_power_dbm": r.tx_power_dbm,
                        "num_clients": r.num_clients,
                    }
                    for r in ap.radios
                ],
            }
        )
    return {"generated_at": snap.generated_at, "access_points": rows}


@router.post("/refresh")
async def refresh_now(col: Collector = Depends(collector)):
    """Force an immediate poll (useful for demos / after config changes)."""
    snap = await col.poll_once()
    return {"ok": snap.ok, "error": snap.error, "generated_at": snap.generated_at}
