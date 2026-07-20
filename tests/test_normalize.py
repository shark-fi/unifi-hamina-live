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
