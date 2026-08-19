"""Vendor-neutral REST API — clean JSON projections of the live snapshot.

Unauthenticated by design (intended to sit behind your own network / the live
dashboard). Use the Meraki-compatible facade under /api/v1 for API-key access.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Query, Response

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


@router.get("/floorplans/{plan_id}/image")
def floorplan_image(plan_id: str, col: Collector = Depends(collector)):
    """Raw floor-plan image bytes (the same image imported into Hamina), used as
    the backdrop for the live client map. 404 until the collector has fetched
    it. Images are cached in memory, so this is cheap to poll."""
    blob = col.floor_image(plan_id)
    if not blob:
        raise HTTPException(status_code=404, detail="no image for this floor plan")
    media = "image/jpeg" if blob[:3] == b"\xff\xd8\xff" else "image/png"
    return Response(content=blob, media_type=media,
                    headers={"Cache-Control": "no-cache"})


def _client_brief(c: Client) -> dict:
    return {
        "mac": c.mac,
        "hostname": c.hostname,
        "ip": c.ip,
        "band": c.band,
        "essid": c.essid,
        "signal_dbm": c.signal_dbm if c.signal_dbm is not None else c.rssi,
        "is_guest": c.is_guest,
    }


@router.get("/map")
def live_map(
    floorplan: str | None = Query(default=None, description="Floor-plan id to render."),
    col: Collector = Depends(collector),
    snap: Snapshot = Depends(snapshot),
):
    """One-shot projection for the live client map: the floor-plan list (for the
    picker) plus, for the selected plan, every placed AP with the clients
    currently associated to it. Clients carry no vendor x,y — UniFi (like every
    non-Mist vendor) reports them per-AP — so the UI clusters each AP's clients
    around it."""
    plans = []
    for f in snap.floorplans:
        placed = [
            a for a in snap.access_points
            if a.floorplan_id == f.id and a.x is not None and a.y is not None
        ]
        plans.append({
            "id": f.id, "site_id": f.site_id, "name": f.name,
            "width_px": f.width_px, "height_px": f.height_px,
            "has_image": col.floor_image(f.id) is not None,
            "num_placed_aps": len(placed),
        })
    selected = floorplan
    if selected is None:  # default to the first plan that actually has placed APs
        with_aps = [p for p in plans if p["num_placed_aps"]]
        selected = (with_aps or plans or [{"id": None}])[0]["id"]

    aps_out = []
    if selected is not None:
        for a in snap.access_points:
            if a.floorplan_id != selected or a.x is None or a.y is None:
                continue
            clients = snap.clients_for_ap(a.mac)
            aps_out.append({
                "serial": a.serial, "mac": a.mac, "name": a.name,
                "model": a.model, "online": a.online,
                "x": a.x, "y": a.y,
                "num_clients": a.num_clients,
                "clients": [_client_brief(c) for c in clients],
            })
    return {
        "generated_at": snap.generated_at,
        "floorplans": plans,
        "selected": selected,
        "access_points": aps_out,
    }


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
                "source": ap.source,
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
                        # The real carrier beside the band/channel it is
                        # wearing. "wifi" and nulls for an actual Wi-Fi radio.
                        "technology": r.technology,
                        "carrier_mhz": r.carrier_mhz,
                        "carrier_label": r.carrier_label,
                    }
                    for r in ap.radios
                ],
            }
        )
    return {"generated_at": snap.generated_at, "access_points": rows}


@router.get("/cellular")
def cellular(col: Collector = Depends(collector), snap: Snapshot = Depends(snapshot)):
    """What the LTE/5G side is doing, and what it is pretending to be.

    Exists because every other surface shows a cell as an access point, which is
    the point of the integration and also the thing most likely to mislead
    someone reading it. This is the one endpoint that says plainly: these
    entries are cells, this is the carrier each one really transmits, and this
    is the Wi-Fi channel it is reporting instead.
    """
    source = getattr(col, "cellular", None)
    cells = [a for a in snap.access_points if a.source == "cellular"]
    rows = []
    for ap in cells:
        radio = ap.radios[0] if ap.radios else None
        rows.append({
            "name": ap.name, "mac": ap.mac, "serial": ap.serial,
            "model": ap.model, "online": ap.online, "ip": ap.ip,
            "site_id": ap.site_id, "num_clients": ap.num_clients,
            "floorplan_id": ap.floorplan_id, "x": ap.x, "y": ap.y,
            "placed": ap.floorplan_id is not None and ap.x is not None,
            "real": {
                "technology": radio.technology if radio else None,
                "carrier_mhz": radio.carrier_mhz if radio else None,
                "carrier": radio.carrier_label if radio else None,
                "tx_power_dbm": radio.tx_power_dbm if radio else None,
                # PRB utilisation, where a source could read it off the radio.
                # It sits under "real" because it is: a load measurement, not
                # dressed up as anything.
                "utilization_pct": radio.channel_utilization_pct if radio else None,
                "firmware": ap.firmware,
            },
            "costume": {
                "band": radio.band if radio else None,
                "channel": radio.channel if radio else None,
                "channel_width_mhz": radio.channel_width_mhz if radio else None,
            },
        })
    return {
        "generated_at": snap.generated_at,
        "enabled": source is not None,
        "configured": bool(source and source.configured),
        "note": getattr(col, "cellular_note", None),
        "error": getattr(source, "error", None),
        "status": getattr(source, "status", {}),
        "cells": rows,
    }


@router.post("/refresh")
async def refresh_now(col: Collector = Depends(collector)):
    """Force an immediate poll (useful for demos / after config changes)."""
    snap = await col.poll_once()
    return {"ok": snap.ok, "error": snap.error, "generated_at": snap.generated_at}
