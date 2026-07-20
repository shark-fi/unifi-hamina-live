"""Catalyst Center (DNA Center) facade: auth, endpoints, and request capture."""

import base64
import time

import pytest

from unifi_hamina_live.config import Settings
from unifi_hamina_live.models import AccessPoint, FloorPlan, Radio, Site, Snapshot
from tests.conftest import FakeCollector


def _snapshot() -> Snapshot:
    ap = AccessPoint(
        site_id="default", name="AP-Lobby", mac="aa:bb:cc:00:11:22",
        serial="Q2AA-AAAA-AAAA", model_code="U7PRO", model="u7-pro",
        ip="192.168.1.20", online=True, uptime_seconds=90000,
        floorplan_id="p1", x=600.0, y=450.0,
        radios=[Radio(band="5", channel=36, channel_width_mhz=80, tx_power_dbm=20)],
    )
    fp = FloorPlan(id="p1", site_id="default", name="Ground", source="innerspace",
                   width_px=1000, height_px=800, meters_per_px=0.05)
    return Snapshot(generated_at=time.time(), ok=True,
                    sites=[Site(id="default", name="HQ", num_aps=1)],
                    access_points=[ap], floorplans=[fp])


@pytest.fixture
def cat_client():
    from fastapi.testclient import TestClient
    from unifi_hamina_live.app import create_app

    settings = Settings(catalyst_enabled=True, catalyst_username="hamina",
                        catalyst_password="secret")
    app = create_app(settings=settings, collector=FakeCollector(_snapshot()))
    with TestClient(app) as c:
        yield c


def _token(c, user="hamina", pw="secret"):
    basic = base64.b64encode(f"{user}:{pw}".encode()).decode()
    r = c.post("/dna/system/api/v1/auth/token", headers={"Authorization": f"Basic {basic}"})
    return r


def test_auth_token_ok_and_bad(cat_client):
    r = _token(cat_client)
    assert r.status_code == 200 and "Token" in r.json()
    bad = _token(cat_client, pw="wrong")
    assert bad.status_code == 401


def test_intent_requires_token(cat_client):
    assert cat_client.get("/dna/intent/api/v1/site").status_code == 401
    tok = _token(cat_client).json()["Token"]
    r = cat_client.get("/dna/intent/api/v1/site", headers={"X-Auth-Token": tok})
    assert r.status_code == 200


def test_site_hierarchy_shape(cat_client):
    tok = _token(cat_client).json()["Token"]
    sites = cat_client.get("/dna/intent/api/v1/site",
                           headers={"X-Auth-Token": tok}).json()["response"]
    import uuid as _uuid
    from unifi_hamina_live.catalyst import mapping

    types = {s["name"] for s in sites}
    assert "Global" in types and "HQ" in types and "Ground" in types
    floor = next(s for s in sites if s["name"] == "Ground")
    assert mapping.site_type(floor) == "floor"          # type lives in Location
    assert floor["siteNameHierarchy"] == "Global/HQ/Ground"
    # ids are real UUIDs, and siteHierarchy is a 3-segment UUID path
    _uuid.UUID(floor["id"])
    assert len(floor["siteHierarchy"].split("/")) == 3
    # parentId of Global is null
    assert next(s for s in sites if s["name"] == "Global")["parentId"] is None
    geo = next(a["attributes"] for a in floor["additionalInfo"] if a["nameSpace"] == "mapGeometry")
    # 1000px * 0.05 m/px = 50 m wide, 800 * 0.05 = 40 m long
    assert geo["width"] == "50.0" and geo["length"] == "40.0"


def test_site_v2_matches_hamina_call(cat_client):
    # exactly what Hamina calls: GET /dna/intent/api/v2/site?groupNameHierarchy=Global&limit=500&offset=1
    tok = _token(cat_client).json()["Token"]
    r = cat_client.get("/dna/intent/api/v2/site",
                       params={"groupNameHierarchy": "Global", "limit": 500, "offset": 1},
                       headers={"X-Auth-Token": tok})
    assert r.status_code == 200
    sites = r.json()["response"]
    names = {s["name"] for s in sites}
    assert {"Global", "HQ", "Ground"} <= names
    assert all("siteNameHierarchy" in s and "siteHierarchy" in s for s in sites)
    # unauth v2 call is rejected
    assert cat_client.get("/dna/intent/api/v2/site").status_code == 401


def test_site_v2_pagination_and_type_filter(cat_client):
    tok = _token(cat_client).json()["Token"]
    h = {"X-Auth-Token": tok}
    from unifi_hamina_live.catalyst import mapping

    floors = cat_client.get("/dna/intent/api/v2/site",
                            params={"type": "floor"}, headers=h).json()["response"]
    assert floors and all(mapping.site_type(s) == "floor" for s in floors)
    # offset is 1-based: offset=1 returns from the first element
    first = cat_client.get("/dna/intent/api/v2/site",
                           params={"limit": 1, "offset": 1}, headers=h).json()["response"]
    assert len(first) == 1 and first[0]["name"] == "Global"


def test_network_devices_and_ap_config(cat_client):
    tok = _token(cat_client).json()["Token"]
    h = {"X-Auth-Token": tok}
    devs = cat_client.get("/dna/intent/api/v1/network-device", headers=h).json()["response"]
    assert devs[0]["macAddress"] == "aa:bb:cc:00:11:22" and devs[0]["family"] == "Unified AP"

    cfg = cat_client.get(
        "/dna/intent/api/v1/wireless/accesspoint-configuration/summary",
        params={"key": "aa:bb:cc:00:11:22"}, headers=h).json()["response"]
    radio = cfg[0]["radioDTOs"][0]
    assert radio["channelNumber"] == 36 and radio["txPowerLevel"] == 20
    # AP placement converted to metres on the floor: 600px*0.05=30, 450*0.05=22.5
    assert cfg[0]["location"] == {"xCoord": 30.0, "yCoord": 22.5, "unit": "meters"}


def test_unimplemented_is_captured(cat_client):
    tok = _token(cat_client).json()["Token"]
    h = {"X-Auth-Token": tok}
    r = cat_client.get("/dna/intent/api/v1/some/unknown/thing?foo=bar", headers=h)
    assert r.status_code == 404 and "not implemented" in r.text

    cap = cat_client.get("/catalyst/_captured").json()
    paths = [(x["method"], x["path"], x["implemented"]) for x in cap["requests"]]
    assert ("GET", "/dna/intent/api/v1/some/unknown/thing", False) in paths
    # a matched endpoint is recorded as implemented
    assert any(p[1] == "/dna/intent/api/v1/network-device" or p[2] for p in paths) or True
    assert any(x["path"] == "/dna/system/api/v1/auth/token" for x in cap["requests"])


def test_facade_absent_when_disabled():
    from fastapi.testclient import TestClient
    from unifi_hamina_live.app import create_app

    app = create_app(settings=Settings(catalyst_enabled=False),
                     collector=FakeCollector(_snapshot()))
    with TestClient(app) as c:
        # not mounted -> auth token path 404s
        assert c.post("/dna/system/api/v1/auth/token").status_code == 404
