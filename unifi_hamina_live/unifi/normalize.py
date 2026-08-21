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
    "UAPA6A6": "u7-pro-outdoor",
    # Deliberate stand-in: Hamina's catalogue has no UniFi Express 7, so the
    # honest code maps to nothing and an unmapped model is not merely a bad
    # label — Hamina answers an unknown model with an empty device list and
    # drops every AP in the batch, not just this one. The Express 7 is Wi-Fi 7
    # 2x2 on 2.4/5/6 and Hamina's u7-pro is BE 2/2/2, so the radios match.
    "UDMA69B": "u7-pro",
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
        num_clients=wireless_client_count(dev),
        radios=radios_from_device(dev),
    )


def wireless_client_count(dev: dict) -> int:
    """Wireless clients on a device, preferring the per-radio counts.

    The device-level ``user-num_sta`` is wireless-only on a standalone AP, but
    on a gateway that also serves Wi-Fi it counts everything behind the gateway
    — a UniFi Express 7 reported 6 with every radio at 0, because those clients
    were wired. Summing the radios gives the same answer on an AP and the right
    one on a gateway, so it is preferred wherever the radios publish the field.
    """
    stats = dev.get("radio_table_stats") or []
    per_radio = [s.get("user-num_sta") for s in stats]
    if per_radio and all(v is not None for v in per_radio):
        return sum(int(v) for v in per_radio)
    return int(dev.get("user-num_sta") or dev.get("num_sta") or 0)


def is_access_point(dev: dict) -> bool:
    """Does this device serve Wi-Fi?

    ``type == "uap"`` misses every console with an integrated AP — a UniFi
    Express 7 reports ``type: "udm"``, and on a console where it is the only
    Wi-Fi radio the bridge saw zero access points, an empty map, and no way to
    tell that from a permissions problem.

    Asking for radios rather than adding gateway types to the allow-list keeps
    a Wi-Fi-less gateway out: a UDM Pro or UXG publishes no ``radio_table`` and
    would otherwise appear as an AP with no radios, which on a floor plan is a
    marker that can never show a client.
    """
    if dev.get("type") == "uap":
        return True
    return bool(dev.get("radio_table") or dev.get("radio_table_stats"))


def _dev_id(sta: dict) -> int | None:
    """UniFi's fingerprint device-type id, which keys the client icon on its CDN.

    dev_id_override wins when set: that is a user-corrected fingerprint, and it
    is what the console's own client list renders. The value can arrive as a
    string, so coerce rather than trusting the type.
    """
    fp = sta.get("fingerprint") or {}
    for v in (sta.get("dev_id_override"), sta.get("dev_id"),
              fp.get("computed_dev_id"), fp.get("dev_id")):
        if v is None or isinstance(v, bool):
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    return None


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
        name=sta.get("name") or None,
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
        dev_id=_dev_id(sta),
        vendor=sta.get("oui") or None,
    )


def wireless_clients_only(stas: list[dict]) -> list[dict]:
    """Keep stations associated over Wi-Fi (have an ap_mac and are not wired)."""
    return [s for s in stas if s.get("ap_mac") and not s.get("is_wired")]
