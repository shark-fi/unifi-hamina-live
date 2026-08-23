"""The HaLow source, against the ``ubus`` payloads the Alfa AHM27292U serves.

The ``INFO`` fixture is a real ``iwinfo info`` reply captured from the radio;
``ASSOC`` is an ``iwinfo assoclist`` entry in OpenWrt's documented shape, added
so client projection is exercised even when no station happens to be associated.
Copied rather than invented, so a change in the radio's shape shows up here as a
failure instead of an empty map.
"""

from __future__ import annotations

import httpx
import pytest

from unifi_hamina_live.config import Settings
from unifi_hamina_live.halow import normalize, rf
from unifi_hamina_live.halow.source import HalowSource, HalowSpec
from unifi_hamina_live.halow.ubus import (UbusAuthError, UbusClient,
                                          UbusUnreachableError)

INFO = {
    "phy": "phy0", "ssid": "WLPC-HALOW", "bssid": "00:C0:CA:B4:74:75",
    "country": "US", "mode": "Master", "channel": 8, "frequency": 2447,
    "txpower": 30, "quality": 60, "quality_max": 70, "noise": -92,
    "htmode": "HT20", "hwmode": "n",
    "encryption": {"enabled": True, "authentication": ["sae"]},
}

ASSOC = {
    "mac": "AA:BB:CC:DD:EE:FF", "signal": -67, "noise": -95, "inactive": 40,
    "rx": {"rate": 7200, "mcs": 0}, "tx": {"rate": 7200, "mcs": 0},
    "rx_bytes": 12345, "tx_bytes": 54321,
}


# --- the projection -------------------------------------------------------

def test_ap_keeps_the_radios_real_bssid_and_power():
    ap = normalize.access_point(INFO, "site1", [])
    assert ap.mac == "00:c0:ca:b4:74:75", "the real BSSID, not a synthetic one"
    assert ap.source == "halow"
    assert ap.online is True
    radio = ap.radios[0]
    assert radio.tx_power_dbm == 30.0
    assert radio.technology == "halow"


def test_the_costume_is_out_of_the_way_and_stable():
    # HaLow is sub-GHz; drawing it on 2.4 GHz ch8 (what iwinfo reports) would
    # read as a co-channel neighbour of a real AP. It wears a 5 GHz DFS channel
    # instead, and the same one every time.
    ap = normalize.access_point(INFO, "site1", [])
    radio = ap.radios[0]
    assert radio.band == "5"
    assert radio.channel in rf.CHANNEL_POOL
    again = normalize.access_point(INFO, "site1", [])
    assert again.radios[0].channel == radio.channel


def test_the_mapped_frequency_is_labelled_not_promoted():
    # 2447 MHz is where the driver's S1G mapping sits, not where the radio
    # transmits, so it must never become a real carrier_mhz.
    radio = normalize.access_point(INFO, "site1", []).radios[0]
    assert radio.carrier_mhz is None
    assert "802.11ah" in radio.carrier_label
    assert "ch8/2447" in radio.carrier_label
    assert "US" in radio.carrier_label


def test_a_station_carries_its_measurement_across():
    ap = normalize.access_point(INFO, "site1", [ASSOC])
    assert ap.num_clients == 1
    c = normalize.client(ASSOC, ap, "site1", "WLPC-HALOW", ip="10.10.5.163")
    assert c.mac == "aa:bb:cc:dd:ee:ff"
    assert c.rssi == -67 and c.signal_dbm == -67
    assert c.noise_dbm == -95
    assert c.ap_mac == ap.mac
    assert c.rx_rate_kbps == 7200 and c.tx_rate_kbps == 7200
    assert c.ip == "10.10.5.163"
    assert c.essid == "WLPC-HALOW"


def test_an_absent_signal_stays_absent_rather_than_zero():
    # A 0 dBm RSSI would draw the client sitting on top of the AP.
    bare = {"mac": "11:22:33:44:55:66"}
    ap = normalize.access_point(INFO, "site1", [bare])
    c = normalize.client(bare, ap, "site1", "WLPC-HALOW")
    assert c.rssi is None and c.signal_dbm is None
    assert c.rx_rate_kbps is None
    assert c.ip is None


# --- the ubus client: session handling ------------------------------------

def _handler(script):
    """A mock ubus endpoint driven by a list of (assert_fn, response) steps."""
    calls = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        i = calls["n"]
        calls["n"] += 1
        check, resp = script[min(i, len(script) - 1)]
        if check:
            check(body)
        return httpx.Response(200, json=resp)

    return handle, calls


@pytest.mark.asyncio
async def test_a_lapsed_session_relogs_in_and_retries():
    # login, then a call that the transport refuses (-32002), then a re-login,
    # then the call succeeds. The client should hide all of that.
    login_ok = {"result": [0, {"ubus_rpc_session": "s" * 32}]}
    denied = {"error": {"code": -32002, "message": "Access denied"}}
    info_ok = {"result": [0, {"bssid": "00:c0:ca:b4:74:75", "ssid": "X"}]}
    handler, calls = _handler([
        (None, login_ok),   # initial login
        (None, denied),     # call #1 -> session lapsed
        (None, login_ok),   # re-login
        (None, info_ok),    # call retried -> ok
    ])
    client = UbusClient("10.0.0.1", "admin", "admin")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    data = await client.call("iwinfo", "info", {"device": "wlan0"})
    assert data["bssid"] == "00:c0:ca:b4:74:75"
    assert calls["n"] == 4, "login, denied call, re-login, retried call"
    await client.aclose()


@pytest.mark.asyncio
async def test_a_refused_login_is_an_auth_error():
    handler, _ = _handler([(None, {"result": [6]})])
    client = UbusClient("10.0.0.1", "admin", "wrong")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(UbusAuthError):
        await client.login()
    await client.aclose()


@pytest.mark.asyncio
async def test_an_absent_radio_is_unreachable_not_auth():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    client = UbusClient("10.0.0.1", "admin", "admin")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    with pytest.raises(UbusUnreachableError):
        await client.login()
    await client.aclose()


# --- the source -----------------------------------------------------------

def _source_over(script) -> HalowSource:
    handler, _ = _handler(script)
    s = Settings(halow_enabled=True, halow_host="10.0.0.1",
                 halow_username="admin", halow_password="admin")
    src = HalowSource(s)
    src._client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler))
    return src


@pytest.mark.asyncio
async def test_source_collects_ap_and_clients():
    login = {"result": [0, {"ubus_rpc_session": "s" * 32}]}
    src = _source_over([
        (None, login),
        (None, {"result": [0, INFO]}),                     # iwinfo info
        (None, {"result": [0, {"results": [ASSOC]}]}),     # assoclist
        (None, {"result": [0, {"entries": [
            {"macaddr": "AA:BB:CC:DD:EE:FF", "ipaddr": "10.10.5.163"}]}]}),  # arp
    ])
    aps, clients, placements = await src.collect("site1")
    assert len(aps) == 1 and aps[0].source == "halow"
    assert len(clients) == 1 and clients[0].ip == "10.10.5.163"
    assert src.status["online"] is True
    assert aps[0].mac in placements
    await src.aclose()


@pytest.mark.asyncio
async def test_an_unreachable_radio_greys_out_the_last_known_ap():
    login = {"result": [0, {"ubus_rpc_session": "s" * 32}]}
    src = _source_over([(None, login), (None, {"result": [0, INFO]}),
                        (None, {"result": [0, {"results": []}]}),
                        (None, {"result": [0, {"entries": []}]})])
    aps, _, _ = await src.collect("site1")
    assert aps[0].online is True
    known = aps[0].mac

    # Now the radio stops answering. The AP should stay on the map, greyed out.
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("gone")

    src._client._client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    aps2, clients2, placements2 = await src.collect("site1")
    assert len(aps2) == 1
    assert aps2[0].mac == known
    assert aps2[0].online is False
    assert aps2[0].radios == [], "an offline AP advertises no radio"
    assert clients2 == []
    assert src.error is not None
    await src.aclose()


@pytest.mark.asyncio
async def test_nothing_is_drawn_before_the_first_successful_poll():
    # An AP invented from thin air on a first-poll failure would import and then
    # vanish when the radio came up with its real BSSID.
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("gone")

    s = Settings(halow_enabled=True, halow_host="10.0.0.1")
    src = HalowSource(s)
    src._client._client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    aps, clients, placements = await src.collect("site1")
    assert aps == [] and clients == [] and placements == {}
    await src.aclose()


def test_spec_reads_placement_from_settings():
    s = Settings(halow_enabled=True, halow_host="10.0.0.1",
                 halow_anchor_ap="Lobby AP", halow_dx_px=30, halow_name="HaLow 1")
    spec = HalowSpec.from_settings(s)
    assert spec.name == "HaLow 1"
    assert spec.placement.anchor_ap == "Lobby AP"
    assert spec.placement.configured is True
