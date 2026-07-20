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

import uuid

from ..models import AccessPoint, FloorPlan, Snapshot

# DNA Center identifies every site with a UUID; a strict Catalyst client will
# choke on plain strings like "global". Synthesize deterministic UUIDs, and let
# floors reuse their InnerSpace/Maps UUID directly so a floor's id equals the
# device floorPlanId.
_NS = uuid.UUID("6f5c9e2a-1111-4000-8000-000000000000")
GLOBAL_ID = str(uuid.uuid5(_NS, "global"))
# DNA Center tenant ids are 24-hex (Mongo ObjectId style), not a UUID/word.
_TENANT = uuid.uuid5(_NS, "tenant").hex[:24]
# rfModel on a real appliance is a numeric code string (e.g. "106110"), NOT the
# human name — a client that parses it as a number would choke otherwise.
_RF_MODEL = "57057"

# Hamina's importer cascades Global -> Area -> Building -> Floor and queries the
# Area dropdown with GET /v2/site?type=area. UniFi has no "area" concept, so we
# synthesize a single area that holds every UniFi site (mapped to a building).
_AREA_NAME = "UniFi"
AREA_ID = str(uuid.uuid5(_NS, "area:" + _AREA_NAME))


def building_id(site_id: str) -> str:
    return str(uuid.uuid5(_NS, "building:" + site_id))


def floor_id_for(fp: FloorPlan) -> str:
    return _as_uuid(fp.id)


def _as_uuid(value) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return str(uuid.uuid5(_NS, "floor:" + str(value)))


def wrap(data) -> dict:
    """Standard Intent API envelope."""
    return {"response": data, "version": "1.0"}


# --- site hierarchy -------------------------------------------------------
# Faithful to Catalyst Center 2.3.7.x GET /dna/intent/api/v2/site, verified
# field-for-field against a live appliance (Cisco DevNet sandbox):
#   * v2 names the paths groupNameHierarchy (names) + groupHierarchy (ids) —
#     NOT the v1 siteNameHierarchy/siteHierarchy. Hamina reads the v2 names.
#   * a non-root site has exactly: parentId, additionalInfo, groupTypeList,
#     groupNameHierarchy, groupHierarchy, name, instanceTenantId, id — and
#     nothing else (a strict fail-on-unknown parser rejects extra fields).
#   * the root (Global) omits parentId, additionalInfo and groupTypeList.
#   * `type` lives only inside the Location additionalInfo attributes.
# additionalInfo[].attributes is a free-form string map, so the extra
# mapGeometry / mapsSummary namespaces on a floor are safe to carry.
def _site(*, id, name, name_path, id_path, parent_id, location_attrs, extra_ns=None) -> dict:
    info = [{"nameSpace": "Location", "attributes": location_attrs}]
    info.extend(extra_ns or [])
    return {
        "parentId": parent_id,
        "additionalInfo": info,
        "groupTypeList": ["SITE"],
        "groupNameHierarchy": name_path,
        "groupHierarchy": id_path,
        "name": name,
        "instanceTenantId": _TENANT,
        "id": id,
    }


def _root() -> dict:
    """The Global root: no parentId / additionalInfo / groupTypeList, as on a
    real appliance. groupHierarchy is its own id."""
    return {
        "groupNameHierarchy": "Global",
        "groupHierarchy": GLOBAL_ID,
        "name": "Global",
        "instanceTenantId": _TENANT,
        "id": GLOBAL_ID,
    }


def site_hierarchy(snap: Snapshot, advertise_maps: bool = True) -> list[dict]:
    a_names = f"Global/{_AREA_NAME}"
    a_ids = f"{GLOBAL_ID}/{AREA_ID}"
    sites = [
        _root(),
        _site(id=AREA_ID, name=_AREA_NAME, name_path=a_names, id_path=a_ids,
              parent_id=GLOBAL_ID,
              location_attrs={"addressInheritedFrom": AREA_ID, "type": "area"}),
    ]
    for site in snap.sites:
        bid = building_id(site.id)
        b_names = f"{a_names}/{site.name}"
        b_ids = f"{a_ids}/{bid}"
        sites.append(_site(
            id=bid, name=site.name,
            name_path=b_names, id_path=b_ids,
            parent_id=AREA_ID,
            location_attrs={"country": "United States",
                            "address": f"{site.name}, USA",
                            "latitude": "37.41810", "longitude": "-121.91900",
                            "addressInheritedFrom": bid, "type": "building"}))
        for fp in snap.floorplans_for_site(site.id):
            fid = floor_id_for(fp)
            w_m, l_m = _metres_dims(fp)
            # mapGeometry/mapsSummary tell Hamina the floor HAS a map, which
            # makes it attempt the (currently unsupported) maps/export image
            # download on import. Omit them so the floor + live AP data import
            # cleanly; the image is added by hand afterwards.
            extra_ns = [
                {"nameSpace": "mapGeometry", "attributes": {
                    "offsetX": "0.0", "offsetY": "0.0",
                    "width": _s(w_m) or "0", "length": _s(l_m) or "0",
                    "height": "3.0"}},
                {"nameSpace": "mapsSummary", "attributes": {
                    "rfModel": _RF_MODEL, "imageURL": "", "floorIndex": "1"}},
            ] if advertise_maps else None
            sites.append(_site(
                id=fid, name=fp.name,
                name_path=f"{b_names}/{fp.name}",
                id_path=f"{b_ids}/{fid}",
                parent_id=bid,
                location_attrs={"address": "", "addressInheritedFrom": bid,
                                "type": "floor"},
                extra_ns=extra_ns))
    return sites


def limit_depth(sites: list[dict], max_depth: int) -> list[dict]:
    """Debug bisect: keep sites up to max_depth (1=root/areas, 2=+buildings,
    3=+floors). The root (Global) has no type and is always kept."""
    allowed = {None, "area"}
    if max_depth >= 2:
        allowed.add("building")
    if max_depth >= 3:
        allowed.add("floor")
    return [s for s in sites if site_type(s) in allowed]


def site_type(site: dict) -> str | None:
    for ai in site.get("additionalInfo", []):
        if ai.get("nameSpace") == "Location":
            return ai.get("attributes", {}).get("type")
    return None


def filter_sites(sites: list[dict], group_name_hierarchy: str, type_: str,
                 offset: int, limit: int) -> list[dict]:
    """v2 GetSite query params: subtree filter + type + 1-based pagination."""
    out = sites
    if group_name_hierarchy and group_name_hierarchy != "Global":
        out = [s for s in out
               if s["groupNameHierarchy"] == group_name_hierarchy
               or s["groupNameHierarchy"].startswith(group_name_hierarchy + "/")]
    if type_:
        out = [s for s in out if site_type(s) == type_]
    start = max(0, (offset or 1) - 1)
    return out[start:start + (limit or 500)]


def aps_for_site_id(snap: Snapshot, site_id: str) -> list[AccessPoint]:
    """Resolve a site UUID (global / area / building / floor) to its APs."""
    if site_id in (GLOBAL_ID, AREA_ID):
        return snap.access_points
    for site in snap.sites:
        if building_id(site.id) == site_id:
            return snap.aps_for_site(site.id)
    for fp in snap.floorplans:
        if floor_id_for(fp) == site_id:
            return [a for a in snap.access_points if a.floorplan_id == fp.id]
    return []


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
