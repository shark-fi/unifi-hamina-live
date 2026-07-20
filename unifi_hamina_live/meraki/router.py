"""Meraki Dashboard API v1 compatible endpoints, backed by live UniFi data.

This implements the subset of the Meraki REST surface a Live/observability
consumer needs to enumerate infrastructure and read wireless RF state:

    GET /api/v1/organizations
    GET /api/v1/organizations/{orgId}
    GET /api/v1/organizations/{orgId}/networks
    GET /api/v1/organizations/{orgId}/devices
    GET /api/v1/organizations/{orgId}/devices/statuses
    GET /api/v1/organizations/{orgId}/devices/availabilities
    GET /api/v1/networks/{networkId}
    GET /api/v1/networks/{networkId}/devices
    GET /api/v1/networks/{networkId}/floorPlans
    GET /api/v1/networks/{networkId}/clients
    GET /api/v1/networks/{networkId}/wireless/rfProfiles
    GET /api/v1/devices/{serial}
    GET /api/v1/devices/{serial}/wireless/radio/settings
    GET /api/v1/devices/{serial}/wireless/status
    GET /api/v1/devices/{serial}/clients

Shapes follow Meraki v1 field names; unsupported-but-expected fields are present
with null/empty values so strict clients parse cleanly.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..config import Settings
from ..deps import require_meraki_key, settings, snapshot
from ..models import Snapshot
from . import mapping

router = APIRouter(
    prefix="/api/v1",
    tags=["meraki-compat"],
    dependencies=[Depends(require_meraki_key)],
)


def _check_org(org_id: str, cfg: Settings) -> str:
    if org_id != mapping.org_id(cfg.meraki_org_name):
        raise HTTPException(status_code=404, detail={"errors": ["Organization not found."]})
    return cfg.meraki_org_name


def _site_for_network(network_id: str, snap: Snapshot) -> str:
    for site in snap.sites:
        if mapping.network_id(site.id) == network_id:
            return site.id
    raise HTTPException(status_code=404, detail={"errors": ["Network not found."]})


# -- organizations --------------------------------------------------------
@router.get("/organizations")
def list_organizations(cfg: Settings = Depends(settings)):
    return [mapping.organization(cfg.meraki_org_name)]


@router.get("/organizations/{org_id}")
def get_organization(org_id: str, cfg: Settings = Depends(settings)):
    name = _check_org(org_id, cfg)
    return mapping.organization(name)


@router.get("/organizations/{org_id}/networks")
def list_networks(
    org_id: str, cfg: Settings = Depends(settings), snap: Snapshot = Depends(snapshot)
):
    name = _check_org(org_id, cfg)
    return [mapping.network(name, s) for s in snap.sites]


@router.get("/organizations/{org_id}/devices")
def list_org_devices(
    org_id: str, cfg: Settings = Depends(settings), snap: Snapshot = Depends(snapshot)
):
    name = _check_org(org_id, cfg)
    return [mapping.device(name, ap) for ap in snap.access_points]


@router.get("/organizations/{org_id}/devices/statuses")
def list_org_device_statuses(
    org_id: str, cfg: Settings = Depends(settings), snap: Snapshot = Depends(snapshot)
):
    name = _check_org(org_id, cfg)
    return [mapping.device_status(name, ap, snap) for ap in snap.access_points]


@router.get("/organizations/{org_id}/devices/availabilities")
def list_org_device_availabilities(
    org_id: str, cfg: Settings = Depends(settings), snap: Snapshot = Depends(snapshot)
):
    name = _check_org(org_id, cfg)
    out = []
    for ap in snap.access_points:
        out.append(
            {
                "serial": ap.serial,
                "name": ap.name,
                "mac": ap.mac,
                "model": mapping.meraki_model(ap),
                "networkId": mapping.network_id(ap.site_id),
                "productType": "wireless",
                "status": "online" if ap.online else "offline",
            }
        )
    return out


# -- networks -------------------------------------------------------------
@router.get("/networks/{network_id}")
def get_network(
    network_id: str, cfg: Settings = Depends(settings), snap: Snapshot = Depends(snapshot)
):
    site_id = _site_for_network(network_id, snap)
    site = next(s for s in snap.sites if s.id == site_id)
    return mapping.network(cfg.meraki_org_name, site)


@router.get("/networks/{network_id}/devices")
def list_network_devices(
    network_id: str, cfg: Settings = Depends(settings), snap: Snapshot = Depends(snapshot)
):
    site_id = _site_for_network(network_id, snap)
    return [mapping.device(cfg.meraki_org_name, ap) for ap in snap.aps_for_site(site_id)]


@router.get("/networks/{network_id}/floorPlans")
def list_floor_plans(network_id: str, snap: Snapshot = Depends(snapshot)):
    # Placement/floor-plan geometry is not part of the live poll in this
    # version. The scheduled OpenIntent refresh carries floor plans instead;
    # see docs/HAMINA.md. Return an empty (valid) list rather than 404.
    _site_for_network(network_id, snap)
    return []


@router.get("/networks/{network_id}/clients")
def list_network_clients(
    network_id: str, snap: Snapshot = Depends(snapshot)
):
    site_id = _site_for_network(network_id, snap)
    return [mapping.client_entry(c, snap) for c in snap.clients_for_site(site_id)]


@router.get("/networks/{network_id}/wireless/rfProfiles")
def list_rf_profiles(network_id: str, snap: Snapshot = Depends(snapshot)):
    _site_for_network(network_id, snap)
    return []


# -- devices --------------------------------------------------------------
def _ap_or_404(serial: str, snap: Snapshot):
    ap = snap.ap_by_serial(serial)
    if ap is None:
        raise HTTPException(status_code=404, detail={"errors": ["Device not found."]})
    return ap


@router.get("/devices/{serial}")
def get_device(
    serial: str, cfg: Settings = Depends(settings), snap: Snapshot = Depends(snapshot)
):
    return mapping.device(cfg.meraki_org_name, _ap_or_404(serial, snap))


@router.get("/devices/{serial}/wireless/radio/settings")
def get_radio_settings(serial: str, snap: Snapshot = Depends(snapshot)):
    return mapping.radio_settings(_ap_or_404(serial, snap))


@router.get("/devices/{serial}/wireless/status")
def get_wireless_status(serial: str, snap: Snapshot = Depends(snapshot)):
    return mapping.wireless_status(_ap_or_404(serial, snap))


@router.get("/devices/{serial}/clients")
def get_device_clients(serial: str, snap: Snapshot = Depends(snapshot)):
    ap = _ap_or_404(serial, snap)
    return [mapping.client_entry(c, snap) for c in snap.clients_for_ap(ap.mac)]
