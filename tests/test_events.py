"""WebSocket event application to the live snapshot."""

from unifi_hamina_live.unifi import events
from tests.conftest import build_snapshot

LOBBY_AP = "aa:bb:cc:00:11:22"       # AP-Lobby, online, 3 clients
WAREHOUSE_AP = "aa:bb:cc:00:55:66"   # AP-Warehouse, offline


def _serials(snap, site="default"):
    return {a.mac: a.serial for a in snap.access_points if a.site_id == site}


def test_connect_adds_client_and_bumps_count():
    snap = build_snapshot()
    before = len(snap.clients_for_site("default"))
    ap = next(a for a in snap.access_points if a.mac == LOBBY_AP)
    ap_count = ap.num_clients
    ev = {"key": "EVT_WU_Connected", "user": "11:22:33:44:55:66",
          "ap": LOBBY_AP, "ssid": "Corp", "radio": "na", "channel": 44}
    changed = events.apply_event(snap, ev, "default", _serials(snap))
    assert changed
    assert len(snap.clients_for_site("default")) == before + 1
    c = next(c for c in snap.clients if c.mac == "11:22:33:44:55:66")
    assert c.ap_mac == LOBBY_AP and c.band == "5" and c.channel == 44
    assert c.ap_serial == _serials(snap)[LOBBY_AP]
    assert ap.num_clients == ap_count + 1


def test_disconnect_removes_client():
    snap = build_snapshot()
    target = snap.clients_for_site("default")[0].mac
    ev = {"key": "EVT_WU_Disconnected", "user": target, "ap": LOBBY_AP}
    changed = events.apply_event(snap, ev, "default", _serials(snap))
    assert changed
    assert all(c.mac != target for c in snap.clients)


def test_roam_moves_client_between_aps():
    snap = build_snapshot()
    # ensure both APs exist as move targets
    client = snap.clients_for_site("default")[0]
    client.ap_mac = LOBBY_AP
    ev = {"key": "EVT_WU_Roam", "user": client.mac,
          "ap_from": LOBBY_AP, "ap_to": WAREHOUSE_AP, "channel": 1}
    changed = events.apply_event(snap, ev, "default", _serials(snap))
    assert changed
    moved = next(c for c in snap.clients if c.mac == client.mac)
    assert moved.ap_mac == WAREHOUSE_AP
    assert moved.ap_serial == _serials(snap)[WAREHOUSE_AP]


def test_ap_down_then_up_toggles_state():
    snap = build_snapshot()
    ap = next(a for a in snap.access_points if a.mac == LOBBY_AP)
    assert ap.online is True
    assert events.apply_event(snap, {"key": "EVT_AP_Lost", "ap": LOBBY_AP}, "default", {})
    assert ap.online is False and ap.state == "offline"
    assert events.apply_event(snap, {"key": "EVT_AP_Connected", "ap": LOBBY_AP}, "default", {})
    assert ap.online is True and ap.state == "online"


def test_unknown_event_is_ignored():
    snap = build_snapshot()
    assert events.apply_event(snap, {"key": "EVT_SomethingElse"}, "default", {}) is False
    assert events.apply_event(snap, {}, "default", {}) is False


def test_parse_message_extracts_events_envelope():
    raw = '{"meta":{"message":"events"},"data":[{"key":"EVT_WU_Connected","user":"a"},{"x":1}]}'
    out = events.parse_message(raw)
    assert len(out) == 2 and out[0]["key"] == "EVT_WU_Connected"
    # non-event messages (sync frames) yield nothing
    assert events.parse_message('{"meta":{"message":"device:sync"},"data":[{}]}') == []
    assert events.parse_message("not json") == []
