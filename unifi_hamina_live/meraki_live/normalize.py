"""Meraki Dashboard records -> the neutral :mod:`..models`.

Pure functions over the dicts the Dashboard API returns, so the projection is
testable without an organization.
"""

from __future__ import annotations

import re

from ..models import AccessPoint, Client, Radio
from ..unifi.normalize import normalize_mac

# Where a Meraki AP comes from, marked on every access point this package makes.
SOURCE = "meraki"

_BAND = re.compile(r"(2\.4|5|6)")
_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def band_of(value) -> str | None:
    """'2.4 GHz' / '5 GHz' / '6' -> '2.4' / '5' / '6'."""
    m = _BAND.search(str(value or ""))
    return m.group(1) if m else None


def number(value) -> float | None:
    """'22 dBm' / '20 MHz' / 56 -> the number, else None."""
    m = _NUM.search(str(value if value is not None else ""))
    return float(m.group(0)) if m else None


def radios(status: dict, utilization: dict[str, float] | None = None) -> list[Radio]:
    """One radio per band from ``wireless/status``.

    The status lists a BSS per SSID per band, including SSIDs that are
    configured but off; the radio is described by a BSS that is actually
    broadcasting, and a band with none is not transmitting, so it is left out.
    """
    utilization = utilization or {}
    by_band: dict[str, dict] = {}
    for bss in status.get("basicServiceSets") or []:
        band = band_of(bss.get("band"))
        if band and bss.get("channel") and bss.get("broadcasting"):
            by_band.setdefault(band, bss)
    out = []
    for band in sorted(by_band, key=float):
        bss = by_band[band]
        width = number(bss.get("channelWidth"))
        out.append(Radio(
            band=band,
            channel=int(number(bss.get("channel"))),
            channel_width_mhz=int(width) if width else None,
            tx_power_dbm=number(bss.get("power")),
            channel_utilization_pct=utilization.get(band),
        ))
    return out


def access_point(device: dict, site_id: str, *, online: bool,
                 status: dict | None = None,
                 utilization: dict[str, float] | None = None) -> AccessPoint:
    mac = normalize_mac(device.get("mac"))
    rads = radios(status or {}, utilization) if online else []
    return AccessPoint(
        site_id=site_id,
        name=device.get("name") or device.get("serial") or mac,
        mac=mac,
        # the real Meraki serial, not a synthesized one: this *is* a Meraki device
        serial=device.get("serial") or mac,
        model_code=device.get("model") or "",
        model=device.get("model") or "",
        ip=device.get("lanIp"),
        state="online" if online else "offline",
        online=online,
        firmware=device.get("firmware"),
        radios=rads,
        source=SOURCE,
    )


def client(row: dict, ap: AccessPoint) -> Client:
    return Client(
        mac=normalize_mac(row.get("mac")),
        hostname=row.get("dhcpHostname") or row.get("mdnsName"),
        name=row.get("description"),
        ip=row.get("ip"),
        site_id=ap.site_id,
        ap_mac=ap.mac,
        ap_serial=ap.serial,
        essid=row.get("ssid"),
        tx_bytes=_kb(row, "sent"),
        rx_bytes=_kb(row, "recv"),
        vendor=row.get("manufacturer"),
    )


def _kb(row: dict, key: str) -> int | None:
    """Meraki reports usage in kilobytes over the query's timespan."""
    v = (row.get("usage") or {}).get(key)
    return int(v * 1024) if isinstance(v, (int, float)) else None
