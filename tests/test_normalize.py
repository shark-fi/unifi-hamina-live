"""Normalization of raw UniFi payloads into neutral models."""

from unifi_hamina_live.unifi import normalize
from tests.conftest import DEVICES_RAW, CLIENTS_RAW


def test_serial_is_stable_and_meraki_shaped():
    s1 = normalize.synth_serial("aa:bb:cc:00:11:22")
    s2 = normalize.synth_serial("AA-BB-CC-00-11-22")  # different formatting, same MAC
    assert s1 == s2
    assert len(s1) == 14 and s1.count("-") == 2  # QXXX-XXXX-XXXX
    assert "0" not in s1 and "1" not in s1  # confusing chars removed


def test_access_point_parsing():
    ap = normalize.access_point(DEVICES_RAW["default"][0], "default")
    assert ap.name == "AP-Lobby"
    assert ap.mac == "aa:bb:cc:00:11:22"
    assert ap.model == "u7-pro" and ap.model_code == "U7PRO"
    assert ap.online is True and ap.state == "online"
    assert ap.num_clients == 3
    bands = {r.band for r in ap.radios}
    assert bands == {"2.4", "5", "6"}
    five = next(r for r in ap.radios if r.band == "5")
    assert five.channel == 36 and five.channel_width_mhz == 80 and five.tx_power_dbm == 20
    assert five.num_clients == 2
    assert five.channel_utilization_pct == 41


def test_offline_ap_state():
    ap = normalize.access_point(DEVICES_RAW["default"][2], "default")
    assert ap.online is False and ap.state == "offline"


def test_switch_is_not_ap():
    assert normalize.is_access_point(DEVICES_RAW["default"][1]) is False


def test_wireless_clients_filtered():
    kept = normalize.wireless_clients_only(CLIENTS_RAW["default"])
    assert len(kept) == 2  # wired client dropped
    serials = {"aa:bb:cc:00:11:22": "SER"}
    c = normalize.client(kept[0], "default", serials)
    assert c.ap_serial == "SER" and c.band == "5" and c.signal_dbm == -62
    assert c.hostname == "laptop"


# --- gateways with an integrated AP ---------------------------------------
#
# Shapes below are trimmed from a real UniFi Express 7 (`UDMA69B`) on the nuc's
# console, which reported type "udm" and was invisible to the bridge: 1 site,
# 0 access points, an empty map, and nothing distinguishing that from a
# permissions problem.

EXPRESS_7 = {
    "type": "udm", "model": "UDMA69B", "name": "WLPC Box", "state": 1,
    "mac": "94:2a:6f:11:22:33", "num_sta": 6, "user-num_sta": 6,
    "radio_table": [
        {"name": "wifi0", "radio": "ng", "ht": 20, "tx_power": 6},
        {"name": "wifi1", "radio": "na", "ht": 80, "tx_power": 20},
    ],
    "radio_table_stats": [
        {"name": "wifi0", "radio": "ng", "state": "RUN", "channel": 11,
         "tx_power": 23, "bw": 20, "user-num_sta": 0, "cu_total": 33},
        {"name": "wifi1", "radio": "na", "state": "RUN", "channel": 44,
         "tx_power": 20, "bw": 80, "user-num_sta": 2, "cu_total": 10},
    ],
}

UDM_PRO = {  # a gateway with no Wi-Fi at all
    "type": "udm", "model": "UDMPROMAX", "name": "Gateway", "state": 1,
    "mac": "94:2a:6f:44:55:66", "num_sta": 40, "user-num_sta": 40,
}

SWITCH = {"type": "usw", "model": "USWED37", "name": "Flex", "state": 1,
          "mac": "94:2a:6f:77:88:99"}


def test_a_gateway_with_radios_is_an_access_point():
    """On an Express 7 the gateway IS the only AP; excluding it empties the map."""
    assert normalize.is_access_point(EXPRESS_7)


def test_a_gateway_without_radios_is_not_an_access_point():
    """A UDM Pro would otherwise be a map marker that can never show a client."""
    assert not normalize.is_access_point(UDM_PRO)


def test_a_switch_is_still_not_an_access_point():
    assert not normalize.is_access_point(SWITCH)


def test_a_plain_ap_is_unaffected():
    assert normalize.is_access_point({"type": "uap", "model": "U7PRO"})


def test_the_gateways_radios_normalize_like_any_ap():
    ap = normalize.access_point(EXPRESS_7, "site-1")
    assert [(r.band, r.channel, r.channel_width_mhz) for r in ap.radios] == [
        ("2.4", 11, 20), ("5", 44, 80)]


def test_wired_clients_are_not_counted_against_the_wi_fi():
    """The Express 7 reported 6 clients with every radio at 0 — those were wired."""
    assert normalize.access_point(EXPRESS_7, "site-1").num_clients == 2


def test_a_plain_ap_client_count_is_unchanged():
    ap = normalize.access_point(
        {"type": "uap", "model": "U7PRO", "mac": "94:2a:6f:00:00:01", "state": 1,
         "user-num_sta": 5,
         "radio_table_stats": [
             {"radio": "ng", "state": "RUN", "channel": 6, "bw": 20,
              "user-num_sta": 2},
             {"radio": "na", "state": "RUN", "channel": 36, "bw": 80,
              "user-num_sta": 3}]}, "site-1")
    assert ap.num_clients == 5


def test_client_count_falls_back_when_the_radios_do_not_publish_it():
    """Older firmware omits the per-radio field; don't report 0 clients then."""
    assert normalize.wireless_client_count(
        {"user-num_sta": 4,
         "radio_table_stats": [{"radio": "ng", "state": "RUN"}]}) == 4
