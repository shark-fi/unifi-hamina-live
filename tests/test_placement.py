"""Placement transforms (classic Maps + InnerSpace) and API exposure."""

import struct
import time

from unifi_hamina_live.models import AccessPoint, FloorPlan, Site, Snapshot
from unifi_hamina_live.unifi import placement


def test_legacy_placement_from_device_xy():
    maps = {"m1": {"_id": "m1", "name": "Floor 1", "upp": 0.05,
                   "width": 1000, "height": 800}}
    devices = [
        {"type": "uap", "mac": "aa:bb:cc:00:11:22", "map_id": "m1", "x": 120, "y": 340},
        {"type": "uap", "mac": "aa:bb:cc:00:33:44"},  # unplaced
    ]
    fps, pos = placement.legacy_placement("default", maps, devices)
    assert len(fps) == 1
    fp = fps[0]
    assert fp.id == "m1" and fp.name == "Floor 1" and fp.source == "legacy"
    assert fp.meters_per_px == 0.05 and fp.width_px == 1000
    assert "aa:bb:cc:00:11:22" in pos
    p = pos["aa:bb:cc:00:11:22"]
    assert p.floorplan_id == "m1" and (p.x, p.y) == (120.0, 340.0)
    assert "aa:bb:cc:00:33:44" not in pos


def test_scene_to_pixels_recenters_without_flip():
    map_shape = {"position": [{"x": 0, "y": 0}], "scale": {"x": 1, "y": 1}}
    x, y = placement.scene_to_pixels({"x": 100, "y": 50}, map_shape, 1000, 800)
    assert (x, y) == (600.0, 450.0)


PROJECT = {
    "plans": [{"id": "p1", "title": "Ground"}],
    "products": [{"id": "prodA", "category": "wifi", "sku": "U7-Pro"},
                 {"id": "prodB", "category": "switch"}],
    "shapes": [
        {"type": "map", "planId": "p1", "urlImage": "/dl/p1.png",
         "position": [{"x": 0, "y": 0}], "scale": {"x": 1, "y": 1}},
        {"type": "scale", "planId": "p1", "scale": 5,
         "position": [{"x": 0, "y": 0}, {"x": 100, "y": 0}]},
        {"type": "device", "planId": "p1", "productId": "prodA",
         "position": [{"x": 100, "y": 50}], "meta": {"mac": "aa:bb:cc:00:11:22"}},
        {"type": "device", "planId": "p1", "productId": "prodB",  # switch, skipped
         "position": [{"x": 10, "y": 10}], "meta": {"mac": "aa:bb:cc:00:99:99"}},
    ],
}


def test_innerspace_placement_with_dims():
    fps, pos = placement.innerspace_placement("s1", PROJECT, {"p1": (1000.0, 800.0)})
    assert len(fps) == 1 and fps[0].width_px == 1000 and fps[0].height_px == 800
    assert fps[0].meters_per_px == 0.05
    assert pos["aa:bb:cc:00:11:22"].floorplan_id == "p1"
    assert (pos["aa:bb:cc:00:11:22"].x, pos["aa:bb:cc:00:11:22"].y) == (600.0, 450.0)
    assert "aa:bb:cc:00:99:99" not in pos  # switch excluded


def test_innerspace_without_dims_emits_plan_but_no_positions():
    fps, pos = placement.innerspace_placement("s1", PROJECT, {})
    assert len(fps) == 1 and fps[0].width_px is None
    assert pos == {}


def test_innerspace_image_urls():
    assert placement.innerspace_image_urls(PROJECT) == {"p1": "/dl/p1.png"}


def test_image_size_png():
    data = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
            + struct.pack(">II", 1024, 768) + b"\x08\x06\x00\x00\x00")  # pad >24 bytes
    assert placement.image_size(data) == (1024, 768)
    assert placement.image_size(b"not an image") is None


# --- API exposure ---------------------------------------------------------
def _snapshot_with_placement() -> Snapshot:
    ap = AccessPoint(
        site_id="default", name="AP1", mac="aa:bb:cc:00:11:22",
        serial="Q2AA-AAAA-AAAA", model_code="U7PRO", model="u7-pro",
        online=True, floorplan_id="p1", x=600.0, y=450.0,
    )
    fp = FloorPlan(id="p1", site_id="default", name="Ground", source="innerspace",
                   width_px=1000, height_px=800, meters_per_px=0.05)
    return Snapshot(
        generated_at=time.time(), ok=True,
        sites=[Site(id="default", name="HQ", num_aps=1)],
        access_points=[ap], floorplans=[fp],
    )


def test_neutral_and_meraki_floorplans(settings):
    from fastapi.testclient import TestClient
    from unifi_hamina_live.app import create_app
    from tests.conftest import FakeCollector

    app = create_app(settings=settings, collector=FakeCollector(_snapshot_with_placement()))
    with TestClient(app) as c:
        fps = c.get("/api/floorplans").json()
        assert len(fps) == 1 and fps[0]["meters_per_px"] == 0.05 and fps[0]["num_aps"] == 0

        ap = c.get("/api/access-points").json()[0]
        assert ap["floorplan_id"] == "p1" and ap["x"] == 600.0 and ap["y"] == 450.0

        mfp = c.get("/api/v1/networks/N_default/floorPlans",
                    headers={"X-Cisco-Meraki-API-Key": "test-key"}).json()
        assert mfp[0]["floorPlanId"] == "p1" and mfp[0]["width"] == 1000
        assert mfp[0]["unifiPlacement"][0]["serial"] == "Q2AA-AAAA-AAAA"
        assert mfp[0]["unifiPlacement"][0]["x"] == 600.0
