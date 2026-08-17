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


def test_a_layout_with_too_few_sensors_disables_ingest_but_serves():
    """This used to refuse to start, and that was the wrong hammer.

    A layout problem cannot make the write endpoint unsafe — it makes it
    unusable. Taking the whole bridge down for it strands the live data the
    extension and Hamina overlay depend on. Reported on /api/located instead.
    """
    with tempfile.TemporaryDirectory() as tmp:
        with make(tmp, sensors=SENSORS[:2]) as c:
            assert c.get("/api/health").json()["ok"] is True
            r = c.get("/api/located")
            assert r.status_code == 503
            assert "at least 3 sensors" in r.json()["detail"]["error"]


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


def test_a_fix_uses_the_same_y_convention_as_a_placed_ap():
    """The mirroring bug, pinned.

    Sensors are configured in image pixels (y down, read off the plan); the
    console reports placements in plan space (y up). Emitting image space under
    the same field name would draw every located AP mirrored about the middle of
    the plan -- correct-looking, and wrong. The dashboard's own comment records
    this happening once already: "the kitchen AP landed in the garage".
    """
    with tempfile.TemporaryDirectory() as tmp, make(tmp) as c:
        # 3 m down from the top of a 15 m plan, i.e. image y = 60 px of 300
        send(c, 10.0, 3.0)
        t = c.get("/api/located").json()["targets"][0]
        assert t["y"] > 200.0, (
            "plan-space y must be measured from the BOTTOM: a target near the "
            "top of the image should be a LARGE y, got %s" % t["y"])
        assert abs(t["y"] - 240.0) < 15.0, t


def test_sensor_markers_are_flipped_with_the_targets():
    """Otherwise the sensors sit mirrored against the fixes they produced."""
    with tempfile.TemporaryDirectory() as tmp, make(tmp) as c:
        by_id = {s["id"]: s for s in c.get("/api/located").json()["sensors"]}
        # pi-1 is configured at image (0,0) -- the TOP-left corner
        assert by_id["pi-1"]["y"] == 300.0, by_id["pi-1"]
        assert by_id["pi-4"]["y"] == 0.0, by_id["pi-4"]


def test_the_plan_extent_is_reported_so_a_client_can_draw_it():
    with tempfile.TemporaryDirectory() as tmp, make(tmp) as c:
        b = c.get("/api/located").json()
        assert (b["width_px"], b["height_px"]) == (400.0, 300.0)


def test_a_missing_config_does_not_take_the_whole_bridge_down():
    """Observed on a live host: SENSORS_ENABLED set where no sensors.json was.

    The container crash-looped, so the live data the extension and the Hamina
    overlay depend on went off the air — because of a file that has nothing to
    do with either. The rest of the service must keep serving.
    """
    cfg = Settings(_env_file=None, sensors_enabled=True, sensor_token="s3cret",
                   sensor_config_path="/nope/sensors.json")
    with TestClient(create_app(settings=cfg,
                               collector=FakeCollector(build_snapshot()))) as c:
        assert c.get("/api/health").json()["ok"] is True
        assert c.get("/api/access-points").status_code == 200


def test_enabled_but_unusable_reads_differently_from_switched_off():
    """503 with the reason, not 404 "it's off" — different problem, different fix."""
    cfg = Settings(_env_file=None, sensors_enabled=True, sensor_token="s3cret",
                   sensor_config_path="/nope/sensors.json")
    with TestClient(create_app(settings=cfg,
                               collector=FakeCollector(build_snapshot()))) as c:
        r = c.get("/api/located")
        assert r.status_code == 503
        err = r.json()["detail"]["error"]
        assert "sensor ingest is OFF" in err
        assert "/nope/sensors.json" in err, "name the file it looked for"
        assert "mount" in err, "in a container the path is inside the image"


def test_a_broken_config_is_reported_the_same_way():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "sensors.json")
        with open(path, "w") as f:
            json.dump({"plan_id": PLAN, "sensors": SENSORS[:1]}, f)  # too few
        cfg = Settings(_env_file=None, sensors_enabled=True,
                       sensor_token="s3cret", sensor_config_path=path)
        with TestClient(create_app(settings=cfg,
                                   collector=FakeCollector(build_snapshot()))) as c:
            assert c.get("/api/health").json()["ok"] is True
            assert "at least 3 sensors" in c.get("/api/located").json()["detail"]["error"]


def test_a_missing_token_is_still_fatal():
    """Unlike the config file, this one guards a write endpoint."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Settings(_env_file=None, sensors_enabled=True, sensor_token="",
                       sensor_config_path=write_layout(tmp))
        try:
            create_app(settings=cfg, collector=FakeCollector(build_snapshot()))
        except RuntimeError as e:
            assert "SENSOR_TOKEN" in str(e)
        else:
            raise AssertionError("a missing token must still refuse to start")


def _four_sensor_layout(tmp):
    """A fourth sensor, so a fix can be over-determined by more than one."""
    return write_layout(tmp, sensors=SENSORS + [{"id": "pi-5", "x_px": 200.0,
                                                 "y_px": 150.0}])


def test_three_sensors_is_reported_as_weakly_constrained():
    """Two unknowns and three anchors leaves ONE consistency check.

    A residual with almost nothing to disagree with is not accuracy, and a
    uniform error in the ranges — which is exactly what an uncalibrated
    intercept produces — keeps it small while moving the position.
    """
    with tempfile.TemporaryDirectory() as tmp, make(tmp) as c:
        for s in SENSORS[:3]:
            sx, sy = s["x_px"] * SCALE, s["y_px"] * SCALE
            c.post("/report", headers={"X-Sensor-Token": "s3cret"},
                   json={"sensor_id": s["id"], "detections": [
                       {"mac": "aa:bb:cc:dd:ee:01",
                        "rssi": rssi(sx, sy, 10.0, 7.5)}]})
        (t,) = c.get("/api/located").json()["targets"]
        assert t["sensors_used"] == 3
        assert t["redundancy"] == 1
        assert t["weakly_constrained"] is True


def test_four_sensors_is_not():
    with tempfile.TemporaryDirectory() as tmp, make(tmp) as c:
        send(c, 10.0, 7.5)                      # all four report
        (t,) = c.get("/api/located").json()["targets"]
        assert t["sensors_used"] == 4
        assert t["redundancy"] == 2
        assert t["weakly_constrained"] is False


def test_a_tight_residual_does_not_clear_the_flag():
    """The point of the flag: a perfect fit on three anchors is still weak."""
    with tempfile.TemporaryDirectory() as tmp, make(tmp) as c:
        for s in SENSORS[:3]:
            sx, sy = s["x_px"] * SCALE, s["y_px"] * SCALE
            c.post("/report", headers={"X-Sensor-Token": "s3cret"},
                   json={"sensor_id": s["id"], "detections": [
                       {"mac": "aa:bb:cc:dd:ee:09",
                        "rssi": rssi(sx, sy, 4.0, 4.0)}]})
        (t,) = c.get("/api/located").json()["targets"]
        assert t["residual_m"] < 0.5, "noise-free input should fit tightly"
        assert t["weakly_constrained"] is True, "and still be weakly constrained"
