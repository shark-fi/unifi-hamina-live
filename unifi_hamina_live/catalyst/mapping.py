"""Project the neutral snapshot onto Cisco DNA Center Intent API shapes.

DNA Center models the world as a site hierarchy — Global (area) → Building →
Floor — with devices placed on floors by x,y **in metres** on a floor of known
width/length. That matches UniFi/OpenIntent far better than Meraki's geo model:
our placement layer already yields pixel x,y + metres-per-pixel, so we convert
straight to DNAC floor metres.

Field names follow DNA Center 2.3.x Intent API. The exact set Hamina consumes
is confirmed from the request log (see the facade's capture buffer); this covers
the well-known endpoints and leaves room to extend.
"""

from __future__ import annotations

from ..models import AccessPoint, FloorPlan, Snapshot

GLOBAL_ID = "global"


def wrap(data) -> dict:
    """Standard Intent API envelope."""
    return {"response": data, "version": "1.0"}


# --- site hierarchy -------------------------------------------------------
def _area(name: str, site_id: str, parent_id: str, hierarchy: str) -> dict:
    return {
        "id": site_id,
        "name": name,
        "siteNameHierarchy": hierarchy,
        "parentId": parent_id,
        "additionalInfo": [
            {"nameSpace": "Location", "attributes": {"type": "area"}}
        ],
    }


def _building(site) -> dict:
    return {
        "id": f"bld_{site.id}",
        "name": site.name,
        "siteNameHierarchy": f"Global/{site.name}",
        "parentId": GLOBAL_ID,
        "additionalInfo": [
            {"nameSpace": "Location",
             "attributes": {"type": "building", "address": "", "latitude": "",
                            "longitude": "", "country": ""}}
        ],
    }


def _floor(site, fp: FloorPlan) -> dict:
    w_m, l_m = _metres_dims(fp)
    return {
        "id": f"flr_{fp.id}",
        "name": fp.name,
        "siteNameHierarchy": f"Global/{site.name}/{fp.name}",
        "parentId": f"bld_{site.id}",
        "additionalInfo": [
            {"nameSpace": "Location",
             "attributes": {"type": "floor"}},
            {"nameSpace": "mapGeometry",
             "attributes": {
                 "width": _s(w_m), "length": _s(l_m), "height": "3.0",
                 "offsetX": "0", "offsetY": "0",
                 "widthPx": _s(fp.width_px), "lengthPx": _s(fp.height_px),
                 "metersPerPixel": _s(fp.meters_per_px)}},
            {"nameSpace": "mapsSummary",
             "attributes": {"floorIndex": "1", "rfModel": "Cubes And Walled Offices"}},
        ],
    }


def site_hierarchy(snap: Snapshot) -> list[dict]:
    sites = [
        {"id": GLOBAL_ID, "name": "Global", "siteNameHierarchy": "Global",
         "parentId": "", "additionalInfo": [
             {"nameSpace": "Location", "attributes": {"type": "area"}}]}
    ]
    for site in snap.sites:
        sites.append(_building(site))
        for fp in snap.floorplans_for_site(site.id):
            sites.append(_floor(site, fp))
    return sites


def floor_id_for(fp: FloorPlan) -> str:
    return f"flr_{fp.id}"


# --- devices --------------------------------------------------------------
def network_device(ap: AccessPoint) -> dict:
    return {
        "id": ap.serial,
        "instanceUuid": ap.serial,
        "hostname": ap.name,
        "managementIpAddress": ap.ip,
        "macAddress": ap.mac,
        "platformId": ap.model,
        "series": ap.model,
        "type": "Unified AP",
        "family": "Unified AP",
        "role": "ACCESS",
        "softwareVersion": ap.firmware,
        "softwareType": "UniFi",
        "reachabilityStatus": "Reachable" if ap.online else "Unreachable",
        "collectionStatus": "Managed" if ap.online else "Unreachable",
        "upTime": _uptime(ap.uptime_seconds),
        "associatedWlcIp": "",
        "apManagerInterfaceIp": "",
    }


def device_detail(ap: AccessPoint, snap: Snapshot) -> dict:
    fp = _ap_floor(ap, snap)
    x_m, y_m = _ap_metres(ap, fp)
    detail = {
        "nwDeviceName": ap.name,
        "macAddress": ap.mac,
        "platformId": ap.model,
        "nwDeviceId": ap.serial,
        "family": "Unified AP",
        "reachabilityStatus": "Reachable" if ap.online else "Unreachable",
        "managementIpAddr": ap.ip,
        "location": fp.name if fp else None,
        "locationName": (f"Global/{_site_name(ap, snap)}/{fp.name}" if fp else None),
    }
    if fp is not None:
        detail["geoLocation"] = {
            "floorId": floor_id_for(fp), "xCoord": x_m, "yCoord": y_m,
            "xPixel": ap.x, "yPixel": ap.y, "unit": "meters",
        }
    return detail


def ap_configuration(ap: AccessPoint, snap: Snapshot) -> dict:
    fp = _ap_floor(ap, snap)
    x_m, y_m = _ap_metres(ap, fp)
    radios = []
    for i, r in enumerate(ap.radios):
        radios.append({
            "slotId": i,
            "radioBand": {"2.4": "2.4GHz", "5": "5GHz", "6": "6GHz"}.get(r.band, r.band),
            "channelNumber": r.channel,
            "channelWidth": (str(r.channel_width_mhz) if r.channel_width_mhz else None),
            "txPowerLevel": r.tx_power_dbm,
            "adminStatus": "Enabled" if r.channel is not None else "Disabled",
        })
    return {
        "instanceUuid": ap.serial,
        "apName": ap.name,
        "macAddress": ap.mac,
        "ethMac": ap.mac,
        "apModel": ap.model,
        "reachabilityStatus": "Reachable" if ap.online else "Unreachable",
        "floorId": floor_id_for(fp) if fp else None,
        "location": {"xCoord": x_m, "yCoord": y_m, "unit": "meters"} if fp else None,
        "radioDTOs": radios,
    }


# --- helpers --------------------------------------------------------------
def _metres_dims(fp: FloorPlan):
    if fp.width_px and fp.height_px and fp.meters_per_px:
        return round(fp.width_px * fp.meters_per_px, 3), round(fp.height_px * fp.meters_per_px, 3)
    return fp.width_px, fp.height_px  # fall back to pixels if unscaled


def _ap_floor(ap: AccessPoint, snap: Snapshot) -> FloorPlan | None:
    if not ap.floorplan_id:
        return None
    return next((f for f in snap.floorplans if f.id == ap.floorplan_id), None)


def _ap_metres(ap: AccessPoint, fp: FloorPlan | None):
    if fp is None or ap.x is None or ap.y is None:
        return None, None
    mpp = fp.meters_per_px
    if not mpp:
        return ap.x, ap.y
    return round(ap.x * mpp, 3), round(ap.y * mpp, 3)


def _site_name(ap: AccessPoint, snap: Snapshot) -> str:
    s = next((s for s in snap.sites if s.id == ap.site_id), None)
    return s.name if s else ap.site_id


def _uptime(seconds: int | None) -> str:
    if not seconds:
        return ""
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    return f"{d} days, {h}:{m:02d}:{s:02d}"


def _s(v) -> str | None:
    return None if v is None else str(v)
