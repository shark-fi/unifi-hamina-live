"""Apply UniFi controller WebSocket events to a live :class:`Snapshot`.

The controller's event stream (``/proxy/network/wss/s/<site>/events``) is
undocumented and varies across Network versions. These helpers are deliberately
tolerant: they handle the high-value events (client connect / disconnect / roam
and AP up/down) and ignore anything they don't recognise. The periodic poll
remains the authoritative reconciler, so a missed or malformed event is
self-healing on the next poll.

Kept as pure functions so they can be unit-tested without a live socket.
"""

from __future__ import annotations

from ..models import AccessPoint, Client, Snapshot
from .normalize import RADIO_BAND, normalize_mac

# Event keys we act on. UniFi prefixes wireless-user events EVT_WU_*.
CONNECT_KEYS = {"EVT_WU_Connected", "EVT_WG_Connected", "EVT_LU_Connected"}
DISCONNECT_KEYS = {"EVT_WU_Disconnected", "EVT_WG_Disconnected", "EVT_LU_Disconnected"}
ROAM_KEYS = {"EVT_WU_Roam", "EVT_WU_RoamRadio"}
AP_UP_KEYS = {"EVT_AP_Connected", "EVT_AP_Adopted"}
AP_DOWN_KEYS = {"EVT_AP_Lost", "EVT_AP_Isolated", "EVT_AP_RestartUnknown"}


def _band_from_event(ev: dict) -> str | None:
    radio = ev.get("radio") or ev.get("radio_proto")
    if radio in RADIO_BAND:
        return RADIO_BAND[radio]
    ch = ev.get("channel")
    if isinstance(ch, int):
        return "2.4" if ch <= 14 else ("6" if ch > 177 else "5")
    return None


def _client_mac(ev: dict) -> str:
    return normalize_mac(ev.get("user") or ev.get("client") or ev.get("mac"))


def _ap_mac(ev: dict) -> str:
    return normalize_mac(ev.get("ap") or ev.get("ap_mac"))


def apply_event(snap: Snapshot, ev: dict, site_id: str, serial_by_mac: dict) -> bool:
    """Mutate ``snap`` for one event. Returns True if anything changed.

    Caller is responsible for any locking and for bumping ``generated_at``.
    """
    key = ev.get("key") or ev.get("event_type")
    if not key:
        return False

    if key in CONNECT_KEYS:
        return _connect(snap, ev, site_id, serial_by_mac)
    if key in DISCONNECT_KEYS:
        return _disconnect(snap, ev, site_id)
    if key in ROAM_KEYS:
        return _roam(snap, ev, site_id, serial_by_mac)
    if key in AP_UP_KEYS:
        return _ap_state(snap, ev, online=True)
    if key in AP_DOWN_KEYS:
        return _ap_state(snap, ev, online=False)
    return False


def _connect(snap: Snapshot, ev: dict, site_id: str, serial_by_mac: dict) -> bool:
    mac = _client_mac(ev)
    if not mac:
        return False
    ap_mac = _ap_mac(ev) or None
    existing = next((c for c in snap.clients if c.mac == mac), None)
    band = _band_from_event(ev)
    channel = ev.get("channel") if isinstance(ev.get("channel"), int) else None
    if existing:
        existing.ap_mac = ap_mac or existing.ap_mac
        existing.ap_serial = serial_by_mac.get(ap_mac, existing.ap_serial)
        existing.essid = ev.get("ssid") or existing.essid
        if band:
            existing.band = band
        if channel is not None:
            existing.channel = channel
    else:
        snap.clients.append(
            Client(
                mac=mac,
                hostname=ev.get("hostname") or ev.get("name"),
                site_id=site_id,
                ap_mac=ap_mac,
                ap_serial=serial_by_mac.get(ap_mac),
                essid=ev.get("ssid"),
                band=band,
                channel=channel,
                is_guest=bool(ev.get("is_guest")),
            )
        )
    _bump_ap_client_count(snap, ap_mac)
    return True


def _disconnect(snap: Snapshot, ev: dict, site_id: str) -> bool:
    mac = _client_mac(ev)
    if not mac:
        return False
    before = len(snap.clients)
    gone = [c for c in snap.clients if c.mac == mac]
    snap.clients[:] = [c for c in snap.clients if c.mac != mac]
    for c in gone:
        _bump_ap_client_count(snap, c.ap_mac, delta=-1)
    return len(snap.clients) != before


def _roam(snap: Snapshot, ev: dict, site_id: str, serial_by_mac: dict) -> bool:
    mac = _client_mac(ev)
    ap_mac = _ap_mac(ev) or normalize_mac(ev.get("ap_to")) or None
    client = next((c for c in snap.clients if c.mac == mac), None)
    if not client:
        # Unknown client roaming in — treat as a connect.
        return _connect(snap, ev, site_id, serial_by_mac)
    old_ap = client.ap_mac
    client.ap_mac = ap_mac or client.ap_mac
    client.ap_serial = serial_by_mac.get(ap_mac, client.ap_serial)
    band = _band_from_event(ev)
    if band:
        client.band = band
    if isinstance(ev.get("channel"), int):
        client.channel = ev["channel"]
    if old_ap != client.ap_mac:
        _bump_ap_client_count(snap, old_ap, delta=-1)
        _bump_ap_client_count(snap, client.ap_mac, delta=1)
    return True


def _ap_state(snap: Snapshot, ev: dict, online: bool) -> bool:
    mac = _ap_mac(ev) or normalize_mac(ev.get("sw") or ev.get("gw"))
    ap = next((a for a in snap.access_points if a.mac == mac), None)
    if not ap:
        return False
    if ap.online == online:
        return False
    ap.online = online
    ap.state = "online" if online else "offline"
    return True


def _bump_ap_client_count(snap: Snapshot, ap_mac: str | None, delta: int = 1) -> None:
    if not ap_mac:
        return
    ap = next((a for a in snap.access_points if a.mac == ap_mac), None)
    if ap:
        ap.num_clients = max(0, ap.num_clients + delta)


def parse_message(raw: str) -> list[dict]:
    """Extract event dicts from a raw WS text frame.

    Controller frames look like ``{"meta":{"message":"events"},"data":[...]}``.
    Only ``events`` messages carry the EVT_* records we apply; sync messages
    (full device/client snapshots) are left to the poll.
    """
    import json

    try:
        msg = json.loads(raw)
    except (ValueError, TypeError):
        return []
    meta = msg.get("meta") if isinstance(msg, dict) else None
    if not isinstance(meta, dict) or meta.get("message") != "events":
        return []
    data = msg.get("data")
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []
