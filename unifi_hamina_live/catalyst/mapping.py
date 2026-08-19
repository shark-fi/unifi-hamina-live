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

import logging
import math
import uuid

from ..models import AccessPoint, FloorPlan, Snapshot

log = logging.getLogger(__name__)

# The model string a client resolves an AP against.
#
# Hamina rejects EVERY value here — "Import partially failed. Some AP models
# (...) aren't yet supported" — for six different strings across two vendors:
# UniFi's code (U7PROMAX), our slug (u7-pro-max, which is also Hamina's own bare
# modelId), Hamina's catalog display name (U7 Pro Max), Hamina's fully-qualified
# catalog id (ubiquiti:u7-pro-max), a bare Cisco model (C9130AXI), and Hamina's
# catalog id for the Cisco make its own connector is configured for
# (ciscoCatalystEnt:CW9166). It quotes back whatever we send, so it reads this
# field; it resolves against something we cannot reach. See issue #1.
#
# A table mapping our slugs onto Hamina's catalog ids lived here briefly. It is
# gone: it coupled this facade to another product's catalog, needed maintaining
# against a list only that product controls, and bought nothing, because the
# model string is not what the import is refusing. Report the AP's real model
# and let the client resolve it.
#
# Diagnostic override (CATALYST_MODEL_OVERRIDE); set from create_app. Empty
# means "report the AP's real model", which is the only sane production value.
MODEL_OVERRIDE: str = ""


def catalog_model(ap: AccessPoint) -> str | None:
    """The model string a client resolves against its hardware catalog.

    Prefers the marketing name over UniFi's internal code — every other surface
    here (platformId, series) already used it, and the code is in nobody's
    catalog. One helper so the surfaces cannot disagree about the same AP.
    """
    if MODEL_OVERRIDE:
        return MODEL_OVERRIDE
    return ap.model or ap.model_code


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


def floor_number(snap: Snapshot, fp: FloorPlan) -> int:
    """1-based position of a floor within its building.

    A floor is identified inside a building by its number, so two floors that
    both claim 1 are not a hierarchy a client can reconcile — it sees one
    building with two different floor ids at the same position. This was
    hard-coded to 1 in three places (the hierarchy's ``floorIndex``, get-floor's
    ``floorNumber``, and the archive XML's ``Floor level``), which is invisible
    on a single-floor site and breaks the moment a second plan exists.

    Ordered by floorplan id rather than name or list order: ids are stable
    across polls, so a floor keeps its number when a plan is renamed or the
    console returns them in a different order. Every caller must use this, or
    the surfaces disagree about the same floor.
    """
    plans = sorted(snap.floorplans_for_site(fp.site_id), key=lambda p: str(p.id))
    for i, p in enumerate(plans, start=1):
        if p.id == fp.id:
            return i
    return 1


def _as_uuid(value) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return str(uuid.uuid5(_NS, "floor:" + str(value)))


def ap_uuid(ap: AccessPoint) -> str:
    """Stable UUID for an AP, used as its network-device id everywhere (a real
    appliance identifies APs by UUID, and accessPointPositions must use the same
    id the device inventory does so Hamina can correlate)."""
    return str(uuid.uuid5(_NS, "ap:" + ap.mac))


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
                    "rfModel": _RF_MODEL, "imageURL": "",
                    "floorIndex": str(floor_number(snap, fp))}},
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
        "id": ap_uuid(ap),
        "instanceUuid": ap_uuid(ap),
        "serialNumber": ap.serial,
        "hostname": ap.name,
        "managementIpAddress": ap.ip,
        "macAddress": ap.mac,
        "platformId": catalog_model(ap),
        "series": catalog_model(ap),
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
        "platformId": catalog_model(ap),
        "nwDeviceId": ap_uuid(ap),
        "serialNumber": ap.serial,
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
        "instanceUuid": ap_uuid(ap),
        "apName": ap.name,
        "macAddress": ap.mac,
        "ethMac": ap.mac,
        "apModel": ap.model,
        "reachabilityStatus": "Reachable" if ap.online else "Unreachable",
        "floorId": floor_id_for(fp) if fp else None,
        "location": {"xCoord": x_m, "yCoord": y_m, "unit": "meters"} if fp else None,
        "radioDTOs": radios,
    }


def assurance_device(ap: AccessPoint, snap: Snapshot,
                     fields: list[str] | None = None) -> dict:
    """One entry in the Assurance networkDevices `data` list, wrapped in
    `{"values": {...}}`. The base object is matched FIELD-FOR-FIELD (every key,
    correct type) to a real Catalyst appliance's fields=["radios"] response
    (live capture, issue #1). Hamina parses this with a strict fail-on-unknown
    parser, so both extra and missing keys fail the vendor-data sync — hence the
    exact match.

    The heavy sub-objects are FIELD-GATED exactly as a real appliance does: the
    base never carries them, and each is added only when its query asks for it —
    fields=["radios"] -> `radios`, ["neighbors"] -> `neighbors`,
    ["connectedNetworkDevice"] -> `connectedNetworkDevice`. (Always-injecting
    radios/neighbors made them extra keys on the connectedNetworkDevice/Switches
    queries and broke the sync.)"""
    want = {f.lower() for f in (fields or [])}
    fp = _ap_floor(ap, snap)
    floor_id = floor_id_for(fp) if fp else ""
    score = 10.0 if ap.online else 1.0
    up = ap.online
    mac = ap.mac
    values = {
        "adminState": "1",
        # device-config threshold string; a real appliance's default, carried verbatim
        "allThresholds": ("MEM=I_90::CPU=I_90::ITF=I_50,I_20,I_20::"
                          "NE=I_-81,I_-83,I_-83::UTIL=I_70,I_70,I_70::"
                          "AQ=I_60,I_75,I_75::LE=I_1"),
        "ancestorSiteId": "",
        "apLastDisconnectTime": "0",
        "apMode": "Local",
        "apProtocol": 4.0,
        "apSlotCount": float(len(ap.radios)),
        "apType": "Standard",
        "areaId": "",
        "bootTime": 0.0,
        "buildingId": "",
        "category": "6",
        "channelAirQualityScore": score,
        "channelNoiseScore": score,
        "clCount": float(ap.num_clients),
        "collectionStatus": "Managed",
        "communicationState": "UP" if up else "DOWN",
        "connectedTime": "",
        "connectedToWlcUuid": "",
        "connectedWlcName": "",
        "connectedWlcUuid": "",
        "connectivityStatus": 100 if up else 0,
        "cpScore": -1.0,
        "cpu": 0.0,
        "cpuScore": score,
        "deviceFamily": "Unified AP",
        "deviceGroupHierarchyId": "/",
        "deviceMacAddress": mac,
        "deviceModel": ap.model,
        "deviceRole": "ACCESS",
        "deviceSeries": ap.model,
        "dpScore": score,
        "errorScore": score,
        "ethernetInterfaces": [
            {"apInterfaceName": "GigabitEthernet0", "errorPercent": 0.0,
             "speed": "1000000000"}
        ],
        "ethernetMac": mac,
        "flexGroup": "",
        "floorId": floor_id,
        "groupUUID": "",
        "healthScore": [
            {"healthType": "OVERALL", "reason": "", "score": score},
            {"healthType": "SYSTEMHEALTH", "reason": "", "score": -1.0},
            {"healthType": "CPHEALTH", "reason": "", "score": -1.0},
            {"healthType": "DPHEALTH", "reason": "", "score": -1.0},
        ],
        "homeApEnabled": "false",
        "icapCapability": "0",
        "interferenceScore": score,
        "isDeleted": False,
        "issueCount": 0,
        "lastBootTime": 0.0,
        "ledFlashEnabled": False,
        "ledFlashSeconds": 0,
        "location": "",
        "maintenanceMode": False,
        "manageabilityState": "Managed",
        "managementIpAddress": ap.ip or "",
        "memory": 0.0,
        "memoryScore": score,
        "name": ap.name,
        "nwDeviceType": ap.model,
        "opState": "4",
        "osVersion": ap.firmware or "",
        "overallScore": score,
        "owningEntityId": mac,
        "parentSiteId": "",
        "platformId": catalog_model(ap),
        "policyTagName": "",
        "powerCalendarProfile": "",
        "powerMode": "HIGH_POWER",
        "powerProfile": "",
        "powerSaveMode": 1.0,
        "powerSaveModeCapable": 2.0,
        "powerStatus": "PoE / Full Power",
        "powerType": "PoE+",
        "protocol": "4",
        "reachability": "UP" if up else "DOWN",
        "regulatoryDomain": "",
        "resetReason": "--",
        "rfTagName": "",
        "ringStatus": False,
        "serialNumber": ap.serial,
        "siteHierarchy": "",
        "siteHierarchyGraphId": f"/{floor_id}/" if floor_id else "/",
        "siteTagName": "",
        "siteUUID": floor_id,
        "softwareVersion": ap.firmware or "",
        "stackType": "NA",
        "subMode": "None",
        "switchName": "",
        "switchPort": "",
        "switchUUID": "",
        "systemScore": score,
        "tagIdList": [],
        "upTime": "",
        "utilizationScore": score,
        "uuid": ap_uuid(ap),
        "wifi6Status": 2.0,
        "wifi6eStatus": -1.0,
        "wifi7Status": 1.0,
    }
    # Field-gated sub-objects — present ONLY when the query requests them, as on
    # a real appliance (captured fields=["radios"]/["neighbors"]/["connectedNetworkDevice"]).
    if "radios" in want:
        values["radios"] = _assurance_radios(ap)
    if "neighbors" in want:
        values["neighbors"] = []  # no RRM neighbor telemetry available
    # connectedNetworkDevice (the AP's uplink switch) is intentionally OMITTED:
    # we don't model the UniFi switch/port an AP uplinks to, and a synthesized
    # blank switch references a device that is absent from the Switches
    # inventory (which we also return empty) — that dangling cross-reference
    # fails the vendor sync. An AP with no known uplink switch is a valid state.
    return {"values": values}


# --- v2 floors API (called after the map archive is downloaded) -----------
# After importing the map, Hamina fetches the floor geometry and the AP
# placements via the newer /dna/intent/api/v2/floors endpoints. Shapes here are
# a best effort pending a real-appliance capture (issue #1); dimensions and
# positions are in the requested unit (feet by default, matching the archive).
_M_TO_FT = 3.280839895


def _unit_conv(units: str) -> tuple[float, str]:
    if (units or "feet").lower().startswith("m"):
        return 1.0, "meters"
    return _M_TO_FT, "feet"


def _floor_by_id(snap: Snapshot, floor_id: str) -> FloorPlan | None:
    return next((f for f in snap.floorplans if floor_id_for(f) == floor_id), None)


def floor_v2(snap: Snapshot, floor_id: str, units: str = "feet") -> dict | None:
    """Get-floor v2, matched to a real appliance: id/parentId/nameHierarchy/
    type/name/floorNumber/rfModel/width/length/height/unitsOfMeasure."""
    fp = _floor_by_id(snap, floor_id)
    if fp is None:
        return None
    conv, unit_name = _unit_conv(units)
    w_m, l_m = _metres_dims(fp)
    site = next((s for s in snap.sites if s.id == fp.site_id), None)
    site_name = site.name if site else fp.site_id
    return {
        "id": floor_id,
        "parentId": building_id(fp.site_id),
        "nameHierarchy": f"Global/{_AREA_NAME}/{site_name}/{fp.name}",
        "type": "floor",
        "name": fp.name,
        "floorNumber": floor_number(snap, fp),
        "rfModel": "Cubes And Walled Offices",
        "width": round((w_m or 0) * conv, 3),
        "length": round((l_m or 0) * conv, 3),
        "height": round(3.0 * conv, 3),
        "unitsOfMeasure": unit_name,
    }


# Assurance radios shape, captured field-for-field from a real appliance's
# POST /api/assurance/v2/networkDevices (fields=["radios"]) for a Unified AP.
# This is a DIFFERENT shape from accessPointPositions.radios (which uses
# id/bands/antenna) — using that shape here breaks Hamina's floor import, so
# the Assurance radios must follow this schema exactly.
_BAND_SLOT = {"2.4": 0, "5": 1, "6": 2}
_BAND_RADIOTYPE = {"2.4": "802.11abgn", "5": "802.11a", "6": "802.11ax"}
_BAND_RFPROFILE = {"2.4": "Typical_Client_Density_rf_24gh",
                   "5": "Typical_Client_Density_rf_5gh",
                   "6": "Typical_Client_Density_rf_6gh"}


def _assurance_radios(ap: AccessPoint) -> list[dict]:
    """The Assurance `radios` array for an AP, matched to a real appliance."""
    radios = []
    for r in ap.radios:
        ch = r.channel
        up = ch is not None
        radios.append({
            "slotId": _BAND_SLOT.get(r.band, 0),
            "band": r.band,
            "radioType": _BAND_RADIOTYPE.get(r.band, "802.11a"),
            "radioProtocol": 4,
            "radioMode": 1,
            "radioModeStr": "Local",
            "radioSubType": "Main",
            "adminState": 1,
            "operState": 2 if up else 1,
            "baseChannel": float(ch) if up else 0.0,
            "channels": [ch] if up else [],
            "channelWidth": int(r.channel_width_mhz) if r.channel_width_mhz else 20,
            "txPower": float(r.tx_power_dbm) if r.tx_power_dbm is not None else 0.0,
            "clientCount": int(r.num_clients),
            "channelUtilization": _f(r.channel_utilization_pct),
            "trafficUtilization": 0.0,
            "txTrafficUtilization": 0.0,
            "rxTrafficUtilization": 0.0,
            "txRateValue": 0.0,
            "rxRateValue": 0.0,
            "noise": -92.0,
            "interference": 0.0,
            "airQuality": 100.0,
            "cleanAirStatus": "Up",
            "antennaPlatformId": "N/A",
            "rfProfile": _BAND_RFPROFILE.get(r.band, ""),
            "xorRadio": 0,
            "wifi6Status": 2,
        })
    return radios


def _f(v) -> float:
    return float(v) if v is not None else 0.0


# Warned once per AP+band, not once per poll: the sync runs repeatedly and a
# radio with no TX power says the same thing every time.
_WARNED: set[str] = set()


def _warn_once(key: str, msg: str, *args) -> None:
    if key not in _WARNED:
        _WARNED.add(key)
        log.warning(msg, *args)


def _position_radios(ap: AccessPoint) -> list[dict]:
    """Radios for accessPointPositions.

    A radio with no live channel or TX power is **dropped**, not emitted with
    nulls. A real appliance never reports null there, so a client deserialising
    this into a typed model throws on it — and the failure surfaces as an opaque
    "unexpected error" with nothing naming the radio, the AP, or the field. A UniFi
    AP produces exactly this whenever a band is disabled or its radio has no live
    state: the other radios on the same AP are fine, so the response looks healthy
    apart from one object.

    Dropping also matches the companion OpenIntent exporter, which omits radios
    whose live state is not RUN rather than seeding coverage that does not exist.
    Both surfaces of this platform now agree about a radio that is not on air.
    """
    radios = []
    for r in ap.radios:
        if r.channel is None or r.tx_power_dbm is None:
            if r.channel is not None:
                # A radio with a channel but no TX power is NOT the ordinary
                # off-air case, and dropping it silently is how an AP arrives in
                # Hamina with every band reading "Off" and nothing anywhere
                # saying why. Seen live: a Baicells eNodeB that publishes
                # min/max TX power over SNMP but not a current value, so the
                # cell synced with radios: [] while its client count came
                # through by another route and made it look half-working.
                _warn_once(
                    "no-txpower:%s:%s" % (ap.mac, r.band),
                    "catalyst: %s %s GHz has channel %s but no TX power, so it "
                    "is dropped from accessPointPositions and will show as Off "
                    "in Hamina. A cell can declare tx_power_dbm in cells.json; "
                    "a UniFi AP reporting this has no live radio state.",
                    ap.name, r.band, r.channel)
            else:
                log.debug("catalyst: dropping %s radio %s GHz from positions "
                          "(channel=%r txPower=%r — not on air)",
                          ap.name, r.band, r.channel, r.tx_power_dbm)
            continue
        try:
            band = float(r.band)
        except (TypeError, ValueError):
            band = 0.0
        radios.append({
            "id": str(uuid.uuid5(_NS, f"radio:{ap.mac}:{r.band}")),
            "bands": [band],
            "channel": r.channel,
            "txPower": int(r.tx_power_dbm),
            "antenna": {"elevation": 0, "name": "Internal", "azimuth": 0},
        })
    return radios


def ap_positions(snap: Snapshot, floor_id: str, units: str = "feet") -> list[dict]:
    """accessPointPositions, matched to a real appliance: each AP is
    id (the network-device UUID) + name/macAddress/type/model/position/radios."""
    fp = _floor_by_id(snap, floor_id)
    if fp is None:
        return []
    conv, _ = _unit_conv(units)
    out = []
    for ap in snap.access_points:
        if ap.floorplan_id != fp.id:
            continue
        x_m, y_m = _ap_metres(ap, fp)
        out.append({
            "id": ap_uuid(ap),
            "name": ap.name,
            "macAddress": ap.mac,
            # `type` must be a model string the client can resolve to a real AP,
            # not UniFi's internal code. Hamina reads THIS field and reported
            # "Some AP models (U7PROMAX, U7PRO, UAPA6A6) aren't yet supported"
            # — those are `model_code` values. It resolves the marketing names
            # fine: the companion OpenIntent exporter maps code -> "u7-pro-max"
            # before export and those imports land. Every other surface here
            # already uses ap.model (platformId, series); this was the outlier.
            "type": catalog_model(ap),
            "model": catalog_model(ap),
            "position": {
                "x": round((x_m or 0) * conv, 3),
                "y": round((y_m or 0) * conv, 3),
                "z": round(3.0 * conv, 3),
            },
            "radios": _position_radios(ap),
        })
    return out


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
    """AP placement in DNAC floor metres.

    The placement layer reports x,y in IMAGE pixels — origin top-left, y
    increasing DOWNWARD. A DNAC floor has its origin bottom-left with y going
    UP, so y must be flipped against the floor's length. Without it every AP
    lands mirrored about the horizontal centre line: an AP by the front door
    appears at the back of the building, and the error is symmetric enough to
    look like a plausible layout rather than a bug.

    This is the third surface in this platform to need the same flip — the
    OpenIntent exporter and the live map's SVG both had it (unifi-hamina-export
    #7, #7 here). Pixel-down versus metres-up is the one conversion this
    codebase gets wrong by default.
    """
    if fp is None or ap.x is None or ap.y is None:
        return None, None
    mpp = fp.meters_per_px
    if not mpp:
        # unscaled: still flip, in the pixel space we are falling back to
        return ap.x, (fp.height_px - ap.y) if fp.height_px else ap.y
    _, length_m = _metres_dims(fp)
    y_m = (length_m - ap.y * mpp) if length_m else (ap.y * mpp)
    return round(ap.x * mpp, 3), round(y_m, 3)


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


# --- Assurance clients ----------------------------------------------------
# GET /dna/data/api/v1/clients?siteId=<floorId>&type=Wireless — the call Hamina
# makes to place clients on a floor, and the last one still 404ing once device
# sync worked ("Failed to synchronize client information — Resource not found").
#
# Unlike the networkDevices shape, this is NOT captured from a real appliance:
# it follows Catalyst Center 2.3.7's documented Assurance client model. Field
# names are therefore the least certain thing in this module. The request log
# (GET /catalyst/_captured) will show whether Hamina stops asking; if it accepts
# the response but shows nothing, the shape is what to revisit first.
#
# siteId is a FLOOR id here, not a building — Hamina asks per floor, so clients
# are filtered to the APs placed on that floor. A client on an AP that is not on
# this floor plan is not on this floor.
_HEALTH_GOOD, _HEALTH_FAIR = -67, -75

# Client placement. UniFi does not locate clients — it reports which AP a
# station is on and nothing more — so the only position available is the AP's.
# Stacking every client on one point is useless to look at, so they are spread
# on concentric rings around the AP, the same representation the InnerSpace
# overlay uses and for the same reason: a ring says "somewhere near this AP",
# which is exactly what is known.
#
# This is NOT client location. A station 20 m down the corridor renders as if it
# were beside the AP, and a map cannot tell that apart from a real fix. It is
# here because a working Juniper Mist project in the same Hamina account returns
# coordinates for 371 of 440 live clients, so the connector evidently expects
# them — Mist genuinely locates clients; we cannot.
_RING_R0 = 1.0        # innermost ring, metres from the AP
_RING_STEP = 0.8      # gap between rings
_RING_MAX = 4.0       # a busy AP must not sprawl across the floor plan
_RING_SEP = 0.7       # preferred spacing between clients on a ring


def _ring_offsets(n: int, seed: int) -> list[tuple[float, float]]:
    """`n` (dx, dy) offsets in metres, on rings around the origin.

    Deterministic: the same client keeps the same spot between polls, or the
    map would shimmer every refresh and a station would look like it was
    moving. `seed` rotates each AP's ring so neighbouring APs do not line their
    clients up in the same direction.
    """
    if n <= 0:
        return []
    out: list[tuple[float, float]] = []
    radius = _RING_R0
    base = (seed % 360) * math.pi / 180.0
    while len(out) < n:
        cap = max(3, int((2 * math.pi * radius) / _RING_SEP))
        take = min(cap, n - len(out))
        step = 2 * math.pi / take
        for i in range(take):
            ang = base + i * step
            out.append((radius * math.cos(ang), radius * math.sin(ang)))
        radius += _RING_STEP
        if radius > _RING_MAX:
            # past the cap, keep packing the outermost ring rather than reaching
            # further out across the plan
            radius = _RING_MAX
            base += step / 2      # interleave with the ring already placed
    return out[:n]


def _client_health(signal: int | None) -> int:
    """DNAC reports a 1-10 client health score. Derive it from RSSI rather than
    invent one: a client at -60 dBm is not "unknown", and Hamina may filter."""
    if signal is None:
        return 5
    if signal >= _HEALTH_GOOD:
        return 10
    if signal >= _HEALTH_FAIR:
        return 7
    return 3


def clients_v1(snap: Snapshot, floor_id: str, req_type: str = "") -> list[dict]:
    """Assurance clients for a floor.

    Second pass at the shape. The first was accepted (HTTP 200, confirmed in the
    request log) and still produced "Failed to synchronize client information —
    An unexpected error occurred", so Hamina parsed the body and rejected it.
    Four things it lacked against Catalyst Center's documented client model, any
    of which would do it:

      * ``connectionStatus`` — a client with no connection state is arguably not
        connected, and a consumer filtering for CONNECTED sees an empty list.
      * ``connectedNetworkDevice`` — the AP is a nested object of its own, not
        just an apMac inside ``connection``. This is how a client is tied to the
        AP it is on, which is the entire point for a map.
      * numeric ``rssi``/``snr`` — sent as strings before.
      * ``lastUpdatedTime`` — spelled ``lastUpdated``, so a freshness check
        finds nothing and may treat every client as stale.

    Still not captured from a real appliance, so still a guess — just a better
    informed one. If it fails again, the next move is to ask Hamina for a client
    payload rather than iterate a third time.
    """
    fp = _floor_by_id(snap, floor_id)
    if fp is None:
        return []
    aps_here = {ap.mac: ap for ap in snap.access_points if ap.floorplan_id == fp.id}
    now_ms = int(snap.generated_at * 1000)
    width_m, length_m = _metres_dims(fp)

    # group by AP so each AP's clients get one ring, strongest signal innermost
    # and ties broken by MAC so the layout is stable across polls
    by_ap: dict[str, list] = {}
    for c in snap.clients:
        if c.ap_mac in aps_here:
            by_ap.setdefault(c.ap_mac, []).append(c)
    placed: dict[str, tuple[float, float]] = {}
    for mac, group in by_ap.items():
        ap = aps_here[mac]
        ax, ay = _ap_metres(ap, fp)
        group.sort(key=lambda c: (-(c.signal_dbm if c.signal_dbm is not None
                                    else c.rssi if c.rssi is not None else -999),
                                  c.mac))
        seed = int(uuid.uuid5(_NS, "ring:" + mac).int % 360)
        for c, (dx, dy) in zip(group, _ring_offsets(len(group), seed)):
            x = (ax or 0) + dx
            y = (ay or 0) + dy
            # keep the ring on the floor: an AP near a wall would otherwise
            # scatter half its clients outside the plan
            if width_m:
                x = min(max(x, 0.0), width_m)
            if length_m:
                y = min(max(y, 0.0), length_m)
            placed[c.mac] = (round(x, 3), round(y, 3))

    out = []
    for c in snap.clients:
        ap = aps_here.get(c.ap_mac or "")
        if ap is None:
            continue
        signal = c.signal_dbm if c.signal_dbm is not None else c.rssi
        snr = (signal - c.noise_dbm
               if signal is not None and c.noise_dbm is not None else None)
        out.append({
            "id": str(uuid.uuid5(_NS, "client:" + c.mac)),
            "macAddress": c.mac,
            "type": req_type or "WIRELESS",
            # Position on the AP's ring — see _ring_offsets. A working Mist
            # project returns coordinates for 371 of 440 live clients, so the
            # connector expects them; ours mean "near this AP", not a fix.
            "x": placed.get(c.mac, (None, None))[0],
            "y": placed.get(c.mac, (None, None))[1],
            "name": c.name or c.hostname or c.mac,
            "hostName": c.hostname or c.name or c.mac,
            "userId": None,
            "username": None,
            "ipv4Address": c.ip,
            "ipv6Addresses": [],
            "vendor": c.vendor,
            "osType": None,
            "osVersion": None,
            "formFactor": None,
            "deviceForm": None,
            "connectionStatus": "CONNECTED",
            "isPrivateMacAddress": False,
            "tracked": "No",
            "health": {
                "overallScore": _client_health(signal),
                "onboardingScore": _client_health(signal),
                "connectedScore": _client_health(signal),
                "linkErrorPercentage": None,
            },
            "traffic": {
                "usage": (c.tx_bytes or 0) + (c.rx_bytes or 0),
                "rxBytes": c.rx_bytes,
                "txBytes": c.tx_bytes,
            },
            "connectedNetworkDevice": {
                "connectedNetworkDeviceId": ap_uuid(ap),
                "connectedNetworkDeviceName": ap.name,
                "connectedNetworkDeviceMac": ap.mac,
                "connectedNetworkDeviceManagementIp": ap.ip,
                "connectedNetworkDeviceType": "Unified AP",
            },
            "connection": {
                "ssid": c.essid,
                "band": c.band,
                "channel": c.channel,
                "channelWidth": None,
                "protocol": None,
                "rssi": signal,
                "snr": snr,
                "dataRate": (round(c.tx_rate_kbps / 1000, 1)
                             if c.tx_rate_kbps else None),
                "apMac": ap.mac,
                "apEthernetMac": ap.mac,
                "apMode": "Local",
                "sessionDuration": c.uptime_seconds,
                "vlanId": None,
                "wlcName": None,
            },
            "onboarding": {"averageRunDuration": None, "maxRunDuration": None},
            "siteId": floor_id,
            "siteHierarchy": f"Global/{_AREA_NAME}/{_site_name(ap, snap)}/{fp.name}",
            "siteHierarchyId": f"{GLOBAL_ID}/{AREA_ID}/"
                               f"{building_id(ap.site_id)}/{floor_id}",
            "lastUpdatedTime": now_ms,
        })
    return out
