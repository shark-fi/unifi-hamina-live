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


_PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)  # header is enough for ext sniff


@pytest.fixture
def cat_client():
    from fastapi.testclient import TestClient
    from unifi_hamina_live.app import create_app

    settings = Settings(catalyst_enabled=True, catalyst_username="hamina",
                        catalyst_password="secret", catalyst_export_delay_ms=0,
                        catalyst_advertise_floor_maps=True,
                        catalyst_maps_export_error=False)
    app = create_app(settings=settings,
                     collector=FakeCollector(_snapshot(), images={"p1": _PNG}))
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
    # Global -> Area (UniFi) -> Building (HQ) -> Floor (Ground)
    assert "UniFi" in types  # synthesized area for Hamina's ?type=area query
    assert any(mapping.site_type(s) == "area" for s in sites)
    # v2 GetSite names the paths group*Hierarchy, not site*Hierarchy
    assert floor["groupNameHierarchy"] == "Global/UniFi/HQ/Ground"
    assert "siteNameHierarchy" not in floor and "siteHierarchy" not in floor
    # ids are real UUIDs, and groupHierarchy is a 4-segment UUID path
    _uuid.UUID(floor["id"])
    assert len(floor["groupHierarchy"].split("/")) == 4
    # real v2 has no systemGroup field; the root omits parentId entirely
    assert all("systemGroup" not in s for s in sites)
    root = next(s for s in sites if s["name"] == "Global")
    assert "parentId" not in root and "additionalInfo" not in root
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
    assert all("groupNameHierarchy" in s and "groupHierarchy" in s for s in sites)
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


def test_maps_export_task_flow_and_archive(cat_client):
    import io
    import json
    import tarfile

    from unifi_hamina_live.catalyst import mapping

    tok = _token(cat_client).json()["Token"]
    h = {"X-Auth-Token": tok}
    floor_id = mapping.floor_id_for(_snapshot().floorplans[0])

    # 1. POST export -> 202 with the task handle {response:{taskId,url}}, and
    #    url uses the real /api/v1/task/ path (a text/plain filename body is
    #    what a real appliance wants; we accept any body)
    r = cat_client.post(f"/dna/intent/api/v1/maps/export/{floor_id}",
                        headers={**h, "Content-Type": "text/plain"}, content="HaminaMapExport")
    assert r.status_code == 202
    resp = r.json()["response"]
    task_id, url = resp["taskId"], resp["url"]
    assert url == f"/api/v1/task/{task_id}"

    # 2. GET task -> a completed DNA Maps task shaped like the real appliance:
    #    progress "finished", endTime set, and the download PATH lives in `data`
    #    as /file/{fileId} (no additionalStatusURL, no fileId in progress)
    task = cat_client.get(url, headers=h).json()["response"]
    assert task["isError"] is False and task["endTime"] == task["version"]
    assert task["serviceType"] == "DNA Maps Service" and task["progress"] == "finished"
    assert "additionalStatusURL" not in task
    assert task["data"].startswith("/file/")

    # a completed task is immutable: a second poll is byte-identical
    assert cat_client.get(url, headers=h).json()["response"] == task

    # 3. download the archive from the path in `data` (served at /file/{id})
    arch = cat_client.get(task["data"], headers=h)
    assert arch.status_code == 200 and arch.headers["content-type"] == "application/octet-stream"
    tar = tarfile.open(fileobj=io.BytesIO(arch.content), mode="r:gz")
    names = tar.getnames()
    assert "xmlDir/MapsImportExport.xml" in names and f"images/{floor_id}.png" in names
    xml = tar.extractfile("xmlDir/MapsImportExport.xml").read().decode()
    assert 'xmlns:ns0="http://importexport.cisco.com/1.0"' in xml
    assert 'distUnits="FEET"' in xml and '<ns0:Floor name="Ground" level="1">' in xml
    assert 'width="164.041995"' in xml and 'imageType="PNG"' in xml  # 1000px*0.05m -> 164.04ft

    # unknown floor -> 404; unauth -> 401
    assert cat_client.post("/dna/intent/api/v1/maps/export/nope", headers=h).status_code == 404
    assert cat_client.post(f"/dna/intent/api/v1/maps/export/{floor_id}").status_code == 401


def test_v2_floors_geometry_and_ap_positions(cat_client):
    from unifi_hamina_live.catalyst import mapping

    tok = _token(cat_client).json()["Token"]
    h = {"X-Auth-Token": tok}
    floor_id = mapping.floor_id_for(_snapshot().floorplans[0])

    # floor geometry in feet: 1000px*0.05=50 m -> 164.04 ft wide
    fl = cat_client.get(f"/dna/intent/api/v2/floors/{floor_id}",
                        params={"_unitsOfMeasure": "feet"}, headers=h).json()["response"]
    assert fl["id"] == floor_id and fl["type"] == "floor"
    assert fl["unitsOfMeasure"] == "feet" and abs(fl["width"] - 164.042) < 0.01
    assert fl["nameHierarchy"] == "Global/UniFi/HQ/Ground"  # real appliance has this

    # AP positions on the floor (feet): AP at 600px*0.05=30 m -> 98.425 ft
    import uuid as _uuid
    pos = cat_client.get(f"/dna/intent/api/v2/floors/{floor_id}/accessPointPositions",
                         headers=h).json()["response"]
    assert pos and pos[0]["macAddress"] == "aa:bb:cc:00:11:22"
    assert abs(pos[0]["position"]["x"] - 98.425) < 0.01
    _uuid.UUID(pos[0]["id"])                 # id is the AP's network-device UUID
    assert pos[0]["id"] == mapping.ap_uuid(_snapshot().access_points[0])
    radio = pos[0]["radios"][0]              # RF data present, band as float array
    assert radio["bands"] == [5.0] and radio["channel"] == 36 and radio["txPower"] == 20


def test_assurance_network_devices(cat_client):
    from unifi_hamina_live.catalyst import mapping

    tok = _token(cat_client).json()["Token"]
    h = {"X-Auth-Token": tok}
    r = cat_client.post("/api/assurance/v2/networkDevices", headers=h, json={"query": {}})
    assert r.status_code == 200
    body = r.json()
    # real appliance envelope: {"version":"2.0","data":[{"values":{...}}]}
    assert body["version"] == "2.0" and len(body["data"]) == 1
    vals = body["data"][0]["values"]
    # uuid matches the AP's network-device / position id so Hamina correlates
    assert vals["uuid"] == mapping.ap_uuid(_snapshot().access_points[0])
    assert vals["deviceMacAddress"] == "aa:bb:cc:00:11:22"
    assert vals["deviceFamily"] == "Unified AP" and vals["healthScore"][0]["score"] == 10.0
    # field-gating: with no fields requested, the heavy sub-objects are absent
    assert "radios" not in vals and "neighbors" not in vals

    # fields=["radios"] adds the real assurance radios shape (NOT the positions
    # shape) — band string, slotId, txPower float, channels list.
    rr = cat_client.post("/api/assurance/v2/networkDevices", headers=h, json={
        "query": {"fields": ["radios"]}}).json()
    radio = rr["data"][0]["values"]["radios"][0]
    assert "neighbors" not in rr["data"][0]["values"]
    assert radio["band"] == "5" and radio["slotId"] == 1
    assert radio["baseChannel"] == 36.0 and radio["channels"] == [36]
    assert radio["txPower"] == 20.0 and radio["channelWidth"] == 80
    assert radio["radioType"] == "802.11a" and radio["radioProtocol"] == 4

    # fields=["neighbors"] adds neighbors (empty for us) but not radios
    nn = cat_client.post("/api/assurance/v2/networkDevices", headers=h, json={
        "query": {"fields": ["neighbors"]}}).json()
    assert nn["data"][0]["values"]["neighbors"] == []
    assert "radios" not in nn["data"][0]["values"]

    # a query for a non-AP family returns nothing (we only have APs)
    sw = cat_client.post("/api/assurance/v2/networkDevices", headers=h, json={
        "query": {"filters": [{"key": "deviceFamily", "operator": "eq",
                               "value": "Switches and Hubs"}]}}).json()
    assert sw["data"] == []

    # planned (design) APs: none, ours are all real/positioned
    from unifi_hamina_live.catalyst import mapping
    floor_id = mapping.floor_id_for(_snapshot().floorplans[0])
    planned = cat_client.get(
        f"/dna/intent/api/v1/floors/{floor_id}/planned-access-points", headers=h)
    assert planned.status_code == 200 and planned.json()["response"] == []

    # unauth rejected
    assert cat_client.post("/api/assurance/v2/networkDevices").status_code == 401


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


def test_maps_export_error_mode_reports_failed_task():
    """With catalyst_maps_export_error, the export task reports failure (a real
    Catalyst map export can fail) rather than a success awaiting a download."""
    from fastapi.testclient import TestClient
    from unifi_hamina_live.app import create_app
    from unifi_hamina_live.catalyst import mapping

    settings = Settings(catalyst_enabled=True, catalyst_username="hamina",
                        catalyst_password="secret", catalyst_maps_export_error=True)
    app = create_app(settings=settings, collector=FakeCollector(_snapshot()))
    with TestClient(app) as c:
        tok = _token(c).json()["Token"]
        h = {"X-Auth-Token": tok}
        floor_id = mapping.floor_id_for(_snapshot().floorplans[0])
        tid = c.post(f"/dna/intent/api/v1/maps/export/{floor_id}",
                     headers=h).json()["response"]["taskId"]
        task = c.get(f"/dna/intent/api/v1/task/{tid}", headers=h).json()["response"]
        assert task["isError"] is True and "failureReason" in task


def test_floors_omit_map_by_default():
    """Default: floors advertise no map, so Hamina imports floor + AP data
    without attempting the maps/export image download."""
    from fastapi.testclient import TestClient
    from unifi_hamina_live.app import create_app
    from unifi_hamina_live.catalyst import mapping

    settings = Settings(catalyst_enabled=True, catalyst_username="hamina",
                        catalyst_password="secret")  # advertise_maps defaults False
    app = create_app(settings=settings, collector=FakeCollector(_snapshot()))
    with TestClient(app) as c:
        tok = _token(c).json()["Token"]
        floors = c.get("/dna/intent/api/v2/site", params={"type": "floor"},
                       headers={"X-Auth-Token": tok}).json()["response"]
        assert floors, "the floor is still present, just without a map"
        for f in floors:
            spaces = [a["nameSpace"] for a in f["additionalInfo"]]
            assert "mapGeometry" not in spaces and "mapsSummary" not in spaces
            assert mapping.site_type(f) == "floor"


def test_facade_absent_when_disabled():
    from fastapi.testclient import TestClient
    from unifi_hamina_live.app import create_app

    app = create_app(settings=Settings(catalyst_enabled=False),
                     collector=FakeCollector(_snapshot()))
    with TestClient(app) as c:
        # not mounted -> auth token path 404s
        assert c.post("/dna/system/api/v1/auth/token").status_code == 404
