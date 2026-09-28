"""The Meraki live source, against Dashboard API payloads captured from a real
CW9166I (trimmed to the fields read), served through an httpx mock transport."""

from __future__ import annotations

import asyncio

import httpx

from unifi_hamina_live.config import Settings
from unifi_hamina_live.meraki_live import normalize
from unifi_hamina_live.meraki_live.api import MerakiClient, encode_params
from unifi_hamina_live.meraki_live.source import (MerakiLiveSource, MerakiLiveSpec,
                                                  parse_anchors)

ORG, NET, SERIAL = "123456", "N_1234567890", "Q5AA-BBBB-CCCC"

DEVICE = {"name": "Home-Lab", "serial": SERIAL, "mac": "68:49:92:3e:82:10", "networkId": NET,
          "model": "CW9166I", "lanIp": "192.0.2.10", "firmware": "wireless-32-2-4"}


def bss(ssid, band, bssid, ch, width, power, broadcasting):
    return {"ssidName": ssid, "enabled": True, "band": band, "bssid": bssid, "channel": ch,
            "channelWidth": width, "power": power, "visible": True, "broadcasting": broadcasting}


STATUS = {"basicServiceSets": [
    bss("Tron8", "2.4 GHz", "68:49:92:3e:82:10", 11, "20 MHz", "16 dBm", False),   # radio up, SSID off on 2.4
    bss("Tron8", "5 GHz", "6a:49:82:3e:82:10", 56, "20 MHz", "22 dBm", True),
    bss("Tron8", "6 GHz", "6a:49:b2:3e:82:10", 5, "80 MHz", "15 dBm", True),
    bss("eduroam", "5 GHz", "6e:49:82:3e:82:10", 56, "20 MHz", "22 dBm", False),
]}

CLIENTS = [
    {"mac": "aa:bb:cc:00:00:01", "description": "Phone", "dhcpHostname": "iPhone", "ip": "10.0.0.20",
     "ssid": "Tron8", "recentDeviceSerial": SERIAL, "recentDeviceConnection": "Wireless",
     "status": "Online", "manufacturer": "Apple", "usage": {"sent": 10, "recv": 20}},
    {"mac": "aa:bb:cc:00:00:02", "recentDeviceSerial": SERIAL, "recentDeviceConnection": "Wireless",
     "status": "Offline"},
]


def api(status="online", fail=False, orgs=None):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if fail:
            return httpx.Response(500, text="boom")
        path = request.url.path.removeprefix("/api/v1")
        body = {
            "/organizations": orgs or [{"id": ORG, "name": "Home Lab", "api": {"enabled": True}}],
            f"/organizations/{ORG}/networks": [{"id": NET, "name": "Lab"}],
            f"/organizations/{ORG}/devices": [DEVICE],
            f"/organizations/{ORG}/devices/statuses": [{"serial": SERIAL, "status": status}],
            f"/organizations/{ORG}/wireless/devices/channelUtilization/byDevice": [
                {"serial": SERIAL, "byBand": [{"band": "5", "total": {"percentage": 23.46}}]}],
            f"/devices/{SERIAL}/wireless/status": STATUS,
            f"/networks/{NET}/clients": CLIENTS,
        }.get(path)
        return httpx.Response(200, json=body) if body is not None else httpx.Response(404, json={"errors": ["no"]})

    return handler, calls


def source(handler, **spec):
    client = MerakiClient("k", transport=httpx.MockTransport(handler))
    return MerakiLiveSource(Settings(), spec=MerakiLiveSpec(**spec), client=client)


def test_array_params_use_brackets():
    assert encode_params({"productTypes": ["wireless"], "perPage": 5}) == [
        ("productTypes[]", "wireless"), ("perPage", "5")]


def test_ap_radios_clients_and_busy_time():
    handler, calls = api()
    aps, clients, _ = asyncio.run(source(handler).collect("default"))
    (ap,) = aps
    assert (ap.name, ap.serial, ap.model, ap.source, ap.online) == ("Home-Lab", SERIAL, "CW9166I", "meraki", True)
    assert ap.mac == "68:49:92:3e:82:10"
    # one radio per band, from a BSS that is broadcasting; 2.4 GHz has none
    assert [(r.band, r.channel, r.channel_width_mhz, r.tx_power_dbm) for r in ap.radios] == [
        ("5", 56, 20, 22.0), ("6", 5, 80, 15.0)]
    assert ap.radios[0].channel_utilization_pct == 23.5
    assert ap.radios[1].channel_utilization_pct is None
    # the offline client is dropped; the online one is joined to the AP
    assert [(c.mac, c.name, c.essid, c.ap_mac, c.vendor) for c in clients] == [
        ("aa:bb:cc:00:00:01", "Phone", "Tron8", ap.mac, "Apple")]
    assert clients[0].signal_dbm is None and clients[0].band is None  # not in Meraki's list: not guessed
    assert ap.num_clients == 1
    assert any("productTypes%5B%5D=wireless" in str(r.url) for r in calls)


def test_offline_ap_has_no_radios_and_no_status_call():
    handler, calls = api(status="offline")
    (ap,), clients, _ = asyncio.run(source(handler).collect("default"))
    assert not ap.online and ap.radios == [] and clients == []
    assert not any(r.url.path.endswith("/wireless/status") for r in calls)


def test_dashboard_outage_greys_out_the_last_known_aps():
    handler, _ = api()
    src = source(handler)
    asyncio.run(src.collect("default"))
    src._client = MerakiClient("k", transport=httpx.MockTransport(api(fail=True)[0]))
    src._slow_at = 0  # force the slow reads too
    (ap,), clients, _ = asyncio.run(src.collect("default"))
    assert ap.name == "Home-Lab" and not ap.online and ap.radios == []
    assert src.error and "500" in src.error


def test_several_orgs_need_an_org_id():
    handler, _ = api(orgs=[{"id": ORG, "name": "A"}, {"id": "2", "name": "B"}])
    src = source(handler)
    aps, _, _ = asyncio.run(src.collect("default"))
    assert aps == [] and "MERAKI_LIVE_ORG_ID" in src.error


def test_anchor_placement_by_ap_name():
    anchors = parse_anchors("home-lab = U7-Pro-Bedroom@30,-10; broken; Other=Kitchen")
    assert anchors["home-lab"].anchor_ap == "U7-Pro-Bedroom"
    assert (anchors["home-lab"].dx_px, anchors["home-lab"].dy_px) == (30.0, -10.0)
    assert anchors["other"].anchor_ap == "Kitchen"
    handler, _ = api()
    aps, _, placements = asyncio.run(source(handler, anchors=anchors).collect("default"))
    assert placements[aps[0].mac].anchor_ap == "U7-Pro-Bedroom"


def test_band_and_number_parsing():
    assert normalize.band_of("2.4 GHz") == "2.4" and normalize.band_of("6") == "6"
    assert normalize.number("22 dBm") == 22.0 and normalize.number(None) is None


def test_collector_merges_meraki_aps_and_serves_them():
    """End to end: the collector folds the Meraki AP into the snapshot, the
    neutral API (which the Hamina panel reads) lists it, and a failed console
    poll carrying it forward doesn't double it."""
    from fastapi.testclient import TestClient

    from unifi_hamina_live.app import create_app
    from unifi_hamina_live.unifi.collector import Collector

    settings = Settings(meraki_live_enabled=True, meraki_live_api_key="k")
    col = Collector(settings)
    handler, _ = api()
    col.meraki_live = source(handler)
    snap = col.snapshot
    asyncio.run(col._merge_meraki(snap))
    asyncio.run(col._merge_meraki(snap))  # a second tick over the carried-forward snapshot
    assert [a.name for a in snap.access_points if a.source == "meraki"] == ["Home-Lab"]
    assert len(snap.clients) == 1
    with TestClient(create_app(settings=settings, collector=col)) as c:
        aps = c.get("/api/access-points").json()
        diag = c.get("/api/meraki-live").json()
    assert any(a["name"] == "Home-Lab" and a["source"] == "meraki" for a in aps)
    assert diag["configured"] and diag["access_points"][0]["radios"][0]["channel"] == 56
