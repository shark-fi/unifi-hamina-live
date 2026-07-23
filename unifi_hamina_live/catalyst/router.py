"""Catalyst Center (DNA Center) Intent API endpoints + request capture.

Implements the auth-token flow and the well-known Intent API endpoints backed by
live UniFi data. Any /dna/* path we don't implement is caught, recorded, and
answered with a DNA-Center-shaped 404 — so pointing Hamina here and reading
``GET /catalyst/_captured`` reveals the exact call sequence its version needs.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse, Response

from ..config import Settings
from ..deps import settings as settings_dep
from ..models import Snapshot
from ..unifi.normalize import normalize_mac
from . import auth, mapping, maps

log = logging.getLogger("unifi_hamina_live.catalyst")

router = APIRouter(tags=["catalyst-compat"])


def _snap(request: Request) -> Snapshot:
    return request.app.state.collector.snapshot


def _cfg(request: Request) -> Settings:
    return request.app.state.settings


def _unauthorized(msg: str = "Invalid X-Auth-Token") -> JSONResponse:
    return JSONResponse(status_code=401, content={"error": msg})


def _require_token(request: Request) -> bool:
    token = request.headers.get("X-Auth-Token")
    return request.app.state.catalyst_tokens.valid(token)


# --- auth -----------------------------------------------------------------
@router.post("/dna/system/api/v1/auth/token")
def auth_token(request: Request, authorization: str | None = Header(default=None)):
    cfg = _cfg(request)
    if not auth.check_basic(authorization, cfg.catalyst_username, cfg.catalyst_password):
        return _unauthorized("Authentication has failed. Please provide valid credentials.")
    token = request.app.state.catalyst_tokens.issue()
    return {"Token": token}


# --- sites ----------------------------------------------------------------
@router.get("/dna/intent/api/v1/site")
def get_sites(request: Request):
    if not _require_token(request):
        return _unauthorized()
    return mapping.wrap(_hierarchy(request))


def _hierarchy(request: Request) -> list:
    cfg = _cfg(request)
    return mapping.site_hierarchy(_snap(request), cfg.catalyst_advertise_floor_maps)


@router.get("/dna/intent/api/v2/site")
def get_sites_v2(
    request: Request,
    groupNameHierarchy: str = "",
    type: str = "",
    offset: int = 1,
    limit: int = 500,
):
    """v2 GetSite — what Hamina calls: ?groupNameHierarchy=Global&limit&offset."""
    if not _require_token(request):
        return _unauthorized()
    sites = _hierarchy(request)
    sites = mapping.limit_depth(sites, _cfg(request).catalyst_site_max_depth)
    page = mapping.filter_sites(sites, groupNameHierarchy, type, offset, limit)
    return mapping.wrap(page)


@router.get("/dna/intent/api/v1/site/count")
def get_site_count(request: Request):
    if not _require_token(request):
        return _unauthorized()
    return mapping.wrap(len(_hierarchy(request)))


@router.get("/dna/intent/api/v1/membership/{site_id}")
def get_membership(site_id: str, request: Request):
    if not _require_token(request):
        return _unauthorized()
    snap = _snap(request)
    devs = mapping.aps_for_site_id(snap, site_id)
    return {
        "version": "1.0",
        "site": {"response": [], "version": "1.0"},
        "device": [{"siteId": site_id,
                    "response": [mapping.network_device(a) for a in devs]}],
    }


# --- devices --------------------------------------------------------------
@router.get("/dna/intent/api/v1/network-device")
def get_network_devices(request: Request):
    if not _require_token(request):
        return _unauthorized()
    return mapping.wrap([mapping.network_device(a) for a in _snap(request).access_points])


@router.get("/dna/intent/api/v1/network-device/count")
def get_network_device_count(request: Request):
    if not _require_token(request):
        return _unauthorized()
    return mapping.wrap(len(_snap(request).access_points))


@router.get("/dna/intent/api/v1/device-detail")
def get_device_detail(request: Request, identifier: str = "", searchBy: str = ""):
    if not _require_token(request):
        return _unauthorized()
    snap = _snap(request)
    key = normalize_mac(searchBy) if "mac" in identifier.lower() else searchBy
    ap = next(
        (a for a in snap.access_points
         if searchBy in (a.serial, a.mac, a.name, mapping.ap_uuid(a)) or a.mac == key),
        None,
    )
    if ap is None:
        return mapping.wrap({})
    return mapping.wrap(mapping.device_detail(ap, snap))


@router.get("/dna/intent/api/v1/wireless/accesspoint-configuration/summary")
def get_ap_configuration(request: Request, key: str = ""):
    if not _require_token(request):
        return _unauthorized()
    snap = _snap(request)
    mac = normalize_mac(key)
    aps = [a for a in snap.access_points if not key or a.mac == mac]
    return mapping.wrap([mapping.ap_configuration(a, snap) for a in aps])


# --- v2 floors (called after the map archive is downloaded) ---------------
@router.get("/dna/intent/api/v2/floors/{floor_id}/accessPointPositions")
def get_floor_ap_positions(floor_id: str, request: Request,
                           limit: int = 500, offset: int = 1):
    """AP placements on a floor (x,y in the floor's units)."""
    if not _require_token(request):
        return _unauthorized()
    return mapping.wrap(mapping.ap_positions(_snap(request), floor_id))


@router.get("/dna/intent/api/v2/floors/{floor_id}")
def get_floor_v2(floor_id: str, request: Request,
                 units: str = Query("feet", alias="_unitsOfMeasure")):
    """Floor geometry (width/length/height) in the requested unit."""
    if not _require_token(request):
        return _unauthorized()
    fl = mapping.floor_v2(_snap(request), floor_id, units)
    if fl is None:
        return _dna_404(f"Floor {floor_id} not found")
    return mapping.wrap(fl)


# --- assurance (live device health, called after AP placement) ------------
@router.post("/api/assurance/v2/networkDevices")
async def assurance_network_devices(request: Request):
    """Assurance device list for the placed APs. Hamina POSTs a filter body; we
    log it (to learn the exact query) and return all APs in an assurance shape."""
    if not _require_token(request):
        return _unauthorized()
    import json as _json

    family = None
    try:
        raw = await request.body()
        if raw:
            log.info("catalyst assurance/networkDevices body: %s",
                     raw[:2000].decode("utf-8", "replace"))
            q = _json.loads(raw).get("query", {})
            for f in q.get("filters", []):
                if f.get("key") == "deviceFamily":
                    family = str(f.get("value") or "")
    except Exception:  # pragma: no cover - defensive
        pass
    # We only have APs; a query for switches/other families returns nothing.
    snap = _snap(request)
    if family and "unified ap" not in family.lower():
        data = []
    else:
        data = [mapping.assurance_device(a, snap) for a in snap.access_points]
    # Real appliance envelope: {"version":"2.0","data":[{"values":{...}}]}
    return {"version": "2.0", "data": data}


@router.get("/dna/intent/api/v1/floors/{floor_id}/planned-access-points")
def get_planned_access_points(floor_id: str, request: Request,
                              limit: int = 500, offset: int = 1):
    """Planned (design) APs on a floor. Ours are all real/positioned APs
    (delivered via accessPointPositions), so there are no planned ones."""
    if not _require_token(request):
        return _unauthorized()
    return mapping.wrap([])


# --- maps export (task-based async BAPI) ----------------------------------
def _dna_404(msg: str) -> JSONResponse:
    return JSONResponse(status_code=404,
                        content={"response": {"errors": [msg]}, "version": "1.0"})


@router.post("/dna/intent/api/v1/maps/export/{floor_id}")
def maps_export(floor_id: str, request: Request):
    """Submit a floor map export. A real appliance takes `Content-Type:
    text/plain` with the archive filename as the body, and answers 202 with the
    task handle (response.taskId + url); the client polls the task then downloads
    the file. We accept any body and generate the archive ourselves."""
    if not _require_token(request):
        return _unauthorized()
    floor = maps._floor(_snap(request), floor_id)
    if floor is None:
        return _dna_404(f"Floor {floor_id} not found")
    job = request.app.state.catalyst_maps.create(floor_id)
    return JSONResponse(status_code=202, content=maps.submit_response(job))


@router.get("/dna/intent/api/v1/task/{task_id}")
@router.get("/api/v1/task/{task_id}")
def get_task(task_id: str, request: Request):
    """Report the export task as complete, pointing at the file download."""
    if not _require_token(request):
        return _unauthorized()
    job = request.app.state.catalyst_maps.by_task(task_id)
    if job is None:
        return _dna_404(f"No task {task_id}")
    cfg = _cfg(request)
    if cfg.catalyst_maps_export_error:
        log.info("catalyst maps/export task %s poll -> FAILED (skip image)", task_id)
        return maps.task_error_response(job)
    body, done = maps.task_response(job, cfg.catalyst_export_delay_ms)
    log.info("catalyst maps/export task %s poll -> %s", task_id, "DONE" if done else "running")
    return body


@router.get("/dna/intent/api/v1/file/{file_id}")
@router.get("/api/v1/file/{file_id}")
@router.get("/file/{file_id}")
def get_file(file_id: str, request: Request):
    """Serve the generated CiscoUnifiedInterchange map archive. The done task's
    `data` field points here as `/file/{fileId}`; also served under the /api/v1
    and /dna/intent/api/v1 prefixes in case the client resolves it that way."""
    if not _require_token(request):
        return _unauthorized()
    job = request.app.state.catalyst_maps.by_file(file_id)
    if job is None:
        return _dna_404(f"No file {file_id}")
    snap = _snap(request)
    floor = maps._floor(snap, job["floor_id"])
    if floor is None:
        return _dna_404(f"Floor {job['floor_id']} not found")
    image = request.app.state.collector.floor_image(floor.id)
    archive = maps.build_archive(snap, job["floor_id"], image)
    return Response(
        content=archive,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{floor.name}.tar.gz"'},
    )


# --- capture / debug ------------------------------------------------------
@router.get("/catalyst/_captured", include_in_schema=False)
def captured(request: Request, clear: bool = False):
    buf = request.app.state.catalyst_captured
    items = list(buf)
    if clear:
        buf.clear()
    return {"count": len(items), "requests": items}


# --- catch-all: record anything we don't implement yet --------------------
@router.api_route("/dna/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
                  include_in_schema=False)
async def unimplemented(rest: str, request: Request):
    log.warning("catalyst: UNIMPLEMENTED %s /dna/%s?%s",
                request.method, rest, request.url.query)
    return JSONResponse(
        status_code=404,
        content={"response": {"errors": [f"Endpoint /dna/{rest} not implemented"]},
                 "version": "1.0"},
    )
