"""Turn raw UniFi ``stat/device`` and ``stat/sta`` payloads into neutral models.

The model map and radio parsing mirror the companion ``unifi_export.py`` so the
two tools agree on how a UniFi device is described.
"""

from __future__ import annotations

import base64
import hashlib

from ..models import AccessPoint, Client, Radio

# UniFi model code -> human model name (kept in sync with unifi_export.py).
UNIFI_MODEL_NAMES: dict[str, str] = {
    "U7PG2": "uap-ac-pro", "U7LT": "uap-ac-lite", "U7LR": "uap-ac-lr",
    "U7HD": "uap-ac-hd", "U7SHD": "uap-ac-shd", "U7NHD": "uap-nanohd",
    "UFLHD": "uap-flexhd", "UHDIW": "uap-iw-hd", "U7IW": "uap-ac-iw",
    "U7MSH": "uap-ac-mesh", "U7MP": "uap-ac-mesh-pro",
    "UAL6": "u6-lite", "UAP6": "u6-lr", "UAP6MP": "u6-pro",
    "U6M": "u6-mesh", "U6IW": "u6-iw", "U6ENT": "u6-enterprise",
    "U6EXT": "u6-extender",
    "U7PRO": "u7-pro", "U7PROMAX": "u7-pro-max",
}

# UniFi radio key -> band label (GHz).
RADIO_BAND: dict[str, str] = {"ng": "2.4", "na": "5", "6e": "6", "ad": "6"}

# UniFi 'ht' (HT/VHT/HE width) -> channel width in MHz.
HT_WIDTH: dict[int, int] = {20: 20, 40: 40, 80: 80, 160: 160, 320: 320}

# UniFi device 'state' code -> label (from unifi_export.py STATE_NAMES).
STATE_NAMES: dict[int, str] = {
    0: "offline", 1: "online", 4: "upgrading", 5: "provisioning",
    6: "heartbeat_missed", 9: "adopting",
}


def normalize_mac(mac: str | None) -> str:
    if not mac:
        return ""
    hexs = mac.replace(":", "").replace("-", "").lower()
    return ":".join(hexs[i : i + 2] for i in range(0, len(hexs), 2))


def synth_serial(mac: str) -> str:
    """A stable, Meraki-looking pseudo-serial derived from the AP MAC.

    Meraki serials look like ``Q2XX-XXXX-XXXX``. We derive 10 base32 chars from
    a hash of the MAC so the same AP always maps to the same serial, and prefix
    ``Q2`` so it is 12 chars in three dash-separated groups of four.
    """
    digest = hashlib.sha1(normalize_mac(mac).encode()).digest()
    b32 = base64.b32encode(digest).decode().rstrip("=")
    # Meraki-safe alphabet excludes 0/1/O/I; map the two b32 chars that could be
    # confusing. Keep it deterministic.
    body = ("Q2" + b32)[:12].upper().replace("0", "2").replace("1", "9")
    return f"{body[0:4]}-{body[4:8]}-{body[8:12]}"


def model_name(code: str | None) -> str:
    code = code or ""
    return UNIFI_MODEL_NAMES.get(code, code.lower())


def _radio(rt: dict, stat: dict) -> Radio | None:
    band = RADIO_BAND.get(rt.get("radio") or stat.get("radio"))
    if not band:
        return None
    channel = stat.get("channel", rt.get("channel"))
    channel = channel if isinstance(channel, int) else None
    width = None
    try:
        width = HT_WIDTH.get(int(rt.get("ht")))
    except (TypeError, ValueError):
        width = None
    tx = stat.get("tx_power")
    tx = float(tx) if isinstance(tx, (int, float)) else None
    util = stat.get("cu_total") or stat.get("channel_utilization")
    retries = stat.get("tx_retries_pct")
    return Radio(
        band=band,
        channel=channel,
        channel_width_mhz=width,
        tx_power_dbm=tx,
        num_clients=int(stat.get("num_sta") or 0),
        channel_utilization_pct=float(util) if isinstance(util, (int, float)) else None,
        tx_retries_pct=float(retries) if isinstance(retries, (int, float)) else None,
    )


def radios_from_device(dev: dict) -> list[Radio]:
    """Merge radio_table (config: width) with radio_table_stats (live)."""
    stats = {s.get("radio"): s for s in dev.get("radio_table_stats") or []}
    table = dev.get("radio_table") or [{"radio": r} for r in stats]
    out: list[Radio] = []
    for rt in table:
        stat = stats.get(rt.get("radio")) or {}
        radio = _radio(rt, stat)
        if radio:
            out.append(radio)
    return out


def access_point(dev: dict, site_id: str) -> AccessPoint:
    mac = normalize_mac(dev.get("mac"))
    code = dev.get("model") or ""
    state = dev.get("state")
    state_label = STATE_NAMES.get(state, str(state)) if state is not None else "unknown"
    return AccessPoint(
        site_id=site_id,
        name=dev.get("name") or mac or "AP",
        mac=mac,
        serial=synth_serial(mac),
        model_code=code,
        model=model_name(code),
        ip=dev.get("ip") or None,
        state=state_label,
        online=state == 1,
        uptime_seconds=dev.get("uptime"),
        firmware=dev.get("version") or None,
        num_clients=int(dev.get("user-num_sta") or dev.get("num_sta") or 0),
        radios=radios_from_device(dev),
    )


def is_access_point(dev: dict) -> bool:
    return dev.get("type") == "uap"


def client(sta: dict, site_id: str, serial_by_mac: dict[str, str]) -> Client:
    ap_mac = normalize_mac(sta.get("ap_mac"))
    band_raw = sta.get("radio")
    band = RADIO_BAND.get(band_raw) if band_raw else None
    if band is None and isinstance(sta.get("channel"), int):
        ch = sta["channel"]
        band = "2.4" if ch <= 14 else ("6" if ch > 177 else "5")
    return Client(
        mac=normalize_mac(sta.get("mac")),
        hostname=sta.get("hostname") or sta.get("name") or None,
        ip=sta.get("ip") or None,
        site_id=site_id,
        ap_mac=ap_mac or None,
        ap_serial=serial_by_mac.get(ap_mac),
        essid=sta.get("essid") or None,
        band=band,
        channel=sta.get("channel") if isinstance(sta.get("channel"), int) else None,
        rssi=sta.get("rssi") if isinstance(sta.get("rssi"), int) else None,
        signal_dbm=sta.get("signal") if isinstance(sta.get("signal"), int) else None,
        noise_dbm=sta.get("noise") if isinstance(sta.get("noise"), int) else None,
        tx_rate_kbps=sta.get("tx_rate") if isinstance(sta.get("tx_rate"), int) else None,
        rx_rate_kbps=sta.get("rx_rate") if isinstance(sta.get("rx_rate"), int) else None,
        tx_bytes=sta.get("tx_bytes"),
        rx_bytes=sta.get("rx_bytes"),
        uptime_seconds=sta.get("uptime"),
        is_guest=bool(sta.get("is_guest")),
    )


def wireless_clients_only(stas: list[dict]) -> list[dict]:
    """Keep stations associated over Wi-Fi (have an ap_mac and are not wired)."""
    return [s for s in stas if s.get("ap_mac") and not s.get("is_wired")]
