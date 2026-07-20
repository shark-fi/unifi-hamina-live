"""Project neutral models onto Meraki Dashboard API v1 JSON shapes.

Hamina Live (and other Meraki API consumers) expect a specific vocabulary:

    organization  ->  the whole UniFi console / account
    network       ->  a UniFi site
    device        ->  an access point (productType "wireless")
    floorPlan     ->  a UniFi floor plan (when placement data is present)

Model codes are mapped to the closest Meraki MR model so downstream tools that
key antenna patterns off the model still render something sensible; the true
UniFi model is preserved in the ``notes`` field and in the neutral API.
"""

from __future__ import annotations

import time

from ..models import AccessPoint, Client, Site, Snapshot

# UniFi human model -> closest Meraki MR model (by Wi-Fi generation / class).
# Approximate on purpose: it exists so model-keyed consumers don't choke, not to
# claim hardware equivalence. The real model is always in `notes`.
UNIFI_TO_MERAKI_MODEL: dict[str, str] = {
    "uap-ac-lite": "MR33", "uap-ac-lr": "MR33", "uap-ac-pro": "MR42",
    "uap-ac-hd": "MR52", "uap-ac-shd": "MR53", "uap-nanohd": "MR44",
    "uap-flexhd": "MR44", "uap-ac-mesh": "MR33", "uap-ac-mesh-pro": "MR42",
    "u6-lite": "MR36", "u6-lr": "MR44", "u6-pro": "MR46", "u6-mesh": "MR44",
    "u6-enterprise": "MR57", "u6-iw": "MR36", "u6-extender": "MR36",
    "u7-pro": "MR57", "u7-pro-max": "MR57",
}

BAND_TO_MERAKI = {"2.4": "2.4", "5": "5", "6": "6"}


def org_id(org_name: str) -> str:
    return f"O_{org_name}"


def network_id(site_id: str) -> str:
    return f"N_{site_id}"


def meraki_model(ap: AccessPoint) -> str:
    return UNIFI_TO_MERAKI_MODEL.get(ap.model, "MR46")


def organization(org_name: str) -> dict:
    return {
        "id": org_id(org_name),
        "name": org_name,
        "url": "",
        "api": {"enabled": True},
        "cloud": {"region": {"name": "UniFi (self-hosted bridge)"}},
        "management": {"details": []},
    }


def network(org_name: str, site: Site) -> dict:
    return {
        "id": network_id(site.id),
        "organizationId": org_id(org_name),
        "name": site.name,
        "productTypes": ["wireless"],
        "timeZone": "Etc/UTC",
        "tags": ["unifi"],
        "enrollmentString": None,
        "notes": f"UniFi site '{site.id}'",
    }


def _status(ap: AccessPoint, snap: Snapshot) -> str:
    return "online" if ap.online else ("dormant" if ap.state == "provisioning" else "offline")


def device(org_name: str, ap: AccessPoint) -> dict:
    """`GET /networks/{id}/devices` / `/organizations/{id}/devices` item."""
    return {
        "serial": ap.serial,
        "name": ap.name,
        "mac": ap.mac,
        "model": meraki_model(ap),
        "networkId": network_id(ap.site_id),
        "productType": "wireless",
        "lanIp": ap.ip,
        "firmware": ap.firmware,
        "tags": ["unifi", ap.model],
        "notes": f"UniFi {ap.model_code} ({ap.model})",
        "floorPlanId": ap.floorplan_id,
        "lat": None,
        "lng": None,
        "address": "",
        "beaconIdParams": None,
    }


def device_status(org_name: str, ap: AccessPoint, snap: Snapshot) -> dict:
    """`GET /organizations/{id}/devices/statuses` item."""
    generated = snap.generated_at or time.time()
    last_reported = _iso(generated)
    return {
        "name": ap.name,
        "serial": ap.serial,
        "mac": ap.mac,
        "model": meraki_model(ap),
        "networkId": network_id(ap.site_id),
        "productType": "wireless",
        "status": _status(ap, snap),
        "lanIp": ap.ip,
        "publicIp": None,
        "lastReportedAt": last_reported,
        "components": {"powerSupplies": []},
    }


def radio_settings(ap: AccessPoint) -> dict:
    """`GET /devices/{serial}/wireless/radio/settings`.

    Meraki groups per-band settings into twoFour/five/sixGhzSettings objects
    with `channel`, `channelWidth`, `targetPower`.
    """
    out: dict = {"serial": ap.serial, "rfProfileId": None}
    key = {
        "2.4": ("twoFourGhzSettings", False),
        "5": ("fiveGhzSettings", True),
        "6": ("sixGhzSettings", True),
    }
    for radio in ap.radios:
        mapped = key.get(radio.band)
        if not mapped:
            continue
        name, has_width = mapped
        block: dict = {"channel": radio.channel, "targetPower": radio.tx_power_dbm}
        if has_width:
            block["channelWidth"] = radio.channel_width_mhz
        out[name] = block
    return out


def wireless_status(ap: AccessPoint) -> dict:
    """`GET /devices/{serial}/wireless/status` — basicServiceSets per radio."""
    band_num = {"2.4": 0, "5": 1, "6": 2}
    bss = []
    for radio in ap.radios:
        bss.append(
            {
                "ssidNumber": band_num.get(radio.band, 0),
                "band": BAND_TO_MERAKI.get(radio.band, radio.band),
                "channel": radio.channel,
                "channelWidth": radio.channel_width_mhz,
                "power": radio.tx_power_dbm,
                "enabled": radio.channel is not None,
                "clientCount": radio.num_clients,
            }
        )
    return {"serial": ap.serial, "basicServiceSets": bss}


def client_entry(c: Client, snap: Snapshot) -> dict:
    """`GET /networks/{id}/clients` / `/devices/{serial}/clients` item."""
    now = snap.generated_at or time.time()
    seen = _iso(now)
    return {
        "id": f"k_{c.mac.replace(':', '')}",
        "mac": c.mac,
        "description": c.hostname,
        "ip": c.ip,
        "user": None,
        "ssid": c.essid,
        "vlan": None,
        "status": "Online",
        "recentDeviceSerial": c.ap_serial,
        "recentDeviceMac": c.ap_mac,
        "lastSeen": seen,
        "firstSeen": None,
        "manufacturer": None,
        "os": None,
        "usage": {"sent": c.tx_bytes, "recv": c.rx_bytes},
        "wirelessCapabilities": None,
        "smInstalled": False,
        # Non-standard but useful extras Hamina/observability can ignore:
        "signalStrengthDbm": c.signal_dbm,
        "channel": c.channel,
        "band": BAND_TO_MERAKI.get(c.band or "", None),
    }


def _iso(epoch: float) -> str:
    """RFC3339/ISO8601 UTC without importing datetime.now (epoch is provided)."""
    import datetime

    return (
        datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
