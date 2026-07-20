"""Catalyst Center (DNA Center) Intent API endpoints + request capture.

Implements the auth-token flow and the well-known Intent API endpoints backed by
live UniFi data. Any /dna/* path we don't implement is caught, recorded, and
answered with a DNA-Center-shaped 404 — so pointing Hamina here and reading
``GET /catalyst/_captured`` reveals the exact call sequence its version needs.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from ..config import Settings
from ..deps import settings as settings_dep
from ..models import Snapshot
from ..unifi.normalize import normalize_mac
from . import auth, mapping

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
    return mapping.wrap(mapping.site_hierarchy(_snap(request)))


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
    sites = mapping.site_hierarchy(_snap(request))
    page = mapping.filter_sites(sites, groupNameHierarchy, type, offset, limit)
    return mapping.wrap(page)


@router.get("/dna/intent/api/v1/site/count")
def get_site_count(request: Request):
    if not _require_token(request):
        return _unauthorized()
    return mapping.wrap(len(mapping.site_hierarchy(_snap(request))))


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
         if searchBy in (a.serial, a.mac, a.name) or a.mac == key),
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
