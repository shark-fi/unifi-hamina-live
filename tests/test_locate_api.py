"""Sensor ingest and /api/located.

The one endpoint in this service that accepts data, so most of what matters
here is what it REFUSES: no token, wrong token, unknown sensor, misconfigured
plan. Each of those otherwise ends as an empty target list, which reads as
"nothing detected" and sends you off to check radios that are working fine.
"""
import json
import math
import os
import tempfile

from fastapi.testclient import TestClient

from unifi_hamina_live.app import create_app
from unifi_hamina_live.config import Settings
from unifi_hamina_live.models import FloorPlan
from tests.conftest import FakeCollector, build_snapshot

PLAN = "plan-1"
# 400 x 300 px at 0.05 m/px == 20 x 15 m
SCALE = 0.05
SENSORS = [{"id": "pi-1", "x_px": 0.0, "y_px": 0.0},
           {"id": "pi-2", "x_px": 400.0, "y_px": 0.0},
           {"id": "pi-3", "x_px": 400.0, "y_px": 300.0},
           {"id": "pi-4", "x_px": 0.0, "y_px": 300.0}]


def write_layout(tmp, sensors=SENSORS, plan_id=PLAN, targets=None):
    path = os.path.join(tmp, "sensors.json")
    with open(path, "w") as f:
        json.dump({"plan_id": plan_id, "sensors": sensors,
                   "targets": targets or []}, f)
    return path


def make(tmp, *, token="s3cret", plan=True, **layout_kw):
    cfg = Settings(_env_file=None, sensors_enabled=True, sensor_token=token,
                   sensor_config_path=write_layout(tmp, **layout_kw))
    snap = build_snapshot()
    if plan:
        snap.floorplans = [FloorPlan(id=PLAN, site_id="default", name="Ground",
                                     source="innerspace", width_px=400,
                                     height_px=300, meters_per_px=SCALE)]
    else:
        snap.floorplans = []
    return TestClient(create_app(settings=cfg, collector=FakeCollector(snap)))


def rssi(sx_m, sy_m, x_m, y_m):
    d = max(math.hypot(x_m - sx_m, y_m - sy_m), 0.3)
    return -40.0 - 30.0 * math.log10(d)


def send(c, x_m, y_m, mac="aa:bb:cc:dd:ee:01", token="s3cret"):
    for s in SENSORS:
        sx, sy = s["x_px"] * SCALE, s["y_px"] * SCALE
        c.post("/report", headers={"X-Sensor-Token": token},
               json={"sensor_id": s["id"],
                     "detections": [{"mac": mac, "rssi": rssi(sx, sy, x_m, y_m),
                                     "kind": "ap", "channel": 36}]})


# --- refusals ---------------------------------------------------------------

def test_ingest_is_absent_unless_enabled():
    """Default config must not expose a write endpoint at all."""
    with TestClient(create_app(Settings(_env_file=None))) as c:
        assert c.post("/report", json={}).status_code == 404
        assert c.get("/api/located").status_code == 404


def test_enabling_without_a_token_refuses_to_start():
    """Not "runs open" — the Meraki facade does that, and this is a write."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Settings(_env_file=None, sensors_enabled=True, sensor_token="",
                       sensor_config_path=write_layout(tmp))
        try:
            create_app(settings=cfg, collector=FakeCollector(build_snapshot()))
        except RuntimeError as e:
            assert "SENSOR_TOKEN" in str(e)
        else:
            raise AssertionError("should refuse to start")


def test_a_report_without_the_token_is_rejected():
    with tempfile.TemporaryDirectory() as tmp, make(tmp) as c:
        assert c.post("/report", json={"sensor_id": "pi-1"}).status_code == 401
        assert c.post("/report", headers={"X-Sensor-Token": "wrong"},
                      json={"sensor_id": "pi-1"}).status_code == 401


def test_an_unknown_sensor_id_is_an_error_not_a_silent_200():
    """A typo that returns 200 forever is an afternoon of looking elsewhere."""
    with tempfile.TemporaryDirectory() as tmp, make(tmp) as c:
        r = c.post("/report", headers={"X-Sensor-Token": "s3cret"},
                   json={"sensor_id": "pi-typo",
                         "detections": [{"mac": "aa:bb", "rssi": -50}]})
        assert r.status_code == 400
        assert "pi-1" in r.json()["detail"]["error"], "should list the real ids"


def test_a_layout_with_too_few_sensors_is_refused_at_startup():
    with tempfile.TemporaryDirectory() as tmp:
        try:
            make(tmp, sensors=SENSORS[:2])
        except RuntimeError as e:
            assert "at least 3 sensors" in str(e)
        else:
            raise AssertionError("should refuse to start")


# --- the empty-list failures, each named ------------------------------------

def test_a_missing_plan_says_so_instead_of_reporting_nothing():
    with tempfile.TemporaryDirectory() as tmp, make(tmp, plan=False) as c:
        body = c.get("/api/located").json()
        assert body["ok"] is False and body["targets"] == []
        assert "not in this console's floor plans" in body["error"]


def test_an_unscaled_plan_says_so():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Settings(_env_file=None, sensors_enabled=True,
                       sensor_token="s3cret",
                       sensor_config_path=write_layout(tmp))
        snap = build_snapshot()
        snap.floorplans = [FloorPlan(id=PLAN, site_id="default", name="Ground",
                                     source="innerspace", width_px=400,
                                     height_px=300, meters_per_px=None)]
        with TestClient(create_app(settings=cfg,
                                   collector=FakeCollector(snap))) as c:
            body = c.get("/api/located").json()
            assert body["ok"] is False
            assert "no scale set" in body["error"]


# --- the happy path ---------------------------------------------------------

def test_a_fix_comes_back_in_floor_plan_pixels():
    with tempfile.TemporaryDirectory() as tmp, make(tmp) as c:
        send(c, 10.0, 7.5)                       # centre of a 20 x 15 m plan
        body = c.get("/api/located").json()
        assert body["ok"] is True and len(body["targets"]) == 1
        t = body["targets"][0]
        # centre in pixels is (200, 150)
        assert math.hypot(t["x"] - 200.0, t["y"] - 150.0) < 12.0, t
        assert t["sensors_used"] == 4 and t["channel"] == 36
        assert body["meters_per_px"] == SCALE

def test_the_result_is_marked_estimated_and_kept_out_of_access_points():
    """A signal-strength estimate must not look like a surveyed placement."""
    with tempfile.TemporaryDirectory() as tmp, make(tmp) as c:
        send(c, 10.0, 7.5)
        assert c.get("/api/located").json()["estimated"] is True
        macs = {a["mac"] for a in c.get("/api/access-points").json()}
        assert "aa:bb:cc:dd:ee:01" not in macs


def test_a_configured_target_name_is_used():
    with tempfile.TemporaryDirectory() as tmp:
        with make(tmp, targets=[{"mac": "AA:BB:CC:DD:EE:01",
                                 "name": "Rogue-1"}]) as c:
            send(c, 5.0, 5.0)
            assert c.get("/api/located").json()["targets"][0]["name"] == "Rogue-1"


def test_every_anchor_is_reported_with_the_fix():
    with tempfile.TemporaryDirectory() as tmp, make(tmp) as c:
        send(c, 10.0, 7.5)
        anchors = c.get("/api/located").json()["targets"][0]["anchors"]
        assert [a["sensor"] for a in anchors] == ["pi-1", "pi-2", "pi-3", "pi-4"]
        assert all(a["dist_m"] > 0 for a in anchors)
