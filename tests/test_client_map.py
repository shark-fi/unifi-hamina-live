"""Live client-map endpoints: /api/map and the floor-plan image proxy."""

from __future__ import annotations

from fastapi.testclient import TestClient

from unifi_hamina_live.app import create_app
from unifi_hamina_live.config import Settings
from unifi_hamina_live.models import (
    AccessPoint,
    Client,
    FloorPlan,
    Site,
    Snapshot,
)

PNG = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 8  # PNG magic + filler


class _Fake:
    def __init__(self, snap: Snapshot, images: dict) -> None:
        self._snap = snap
        self._images = images

    @property
    def snapshot(self) -> Snapshot:
        return self._snap

    def floor_image(self, plan_id: str) -> bytes | None:
        return self._images.get(plan_id)

    def start(self) -> None:  # noqa: D401 - no-op
        pass

    async def stop(self) -> None:
        pass

    async def poll_once(self) -> Snapshot:
        return self._snap


def _snapshot() -> Snapshot:
    site = "default"
    fp = FloorPlan(
        id="plan1", site_id=site, name="Ground", source="innerspace",
        width_px=1000, height_px=600, num_aps=2,
    )
    ap_a = AccessPoint(
        site_id=site, name="AP-A", mac="aa:aa:aa:aa:aa:aa", serial="S-A",
        model_code="U7PRO", model="u7-pro", online=True, num_clients=2,
        floorplan_id="plan1", x=100, y=120,
    )
    ap_b = AccessPoint(
        site_id=site, name="AP-B", mac="bb:bb:bb:bb:bb:bb", serial="S-B",
        model_code="U6PRO", model="u6-pro", online=True, num_clients=0,
        floorplan_id="plan1", x=500, y=300,
    )
    ap_unplaced = AccessPoint(  # no floorplan/x/y — must not appear on the map
        site_id=site, name="AP-C", mac="cc:cc:cc:cc:cc:cc", serial="S-C",
        model_code="U7PRO", model="u7-pro", online=True,
    )
    clients = [
        Client(
            mac="de:ad:00:00:00:01", hostname="laptop", site_id=site,
            ap_mac="aa:aa:aa:aa:aa:aa", ap_serial="S-A", band="5", signal_dbm=-60,
        ),
        Client(  # guest, no signal_dbm — brief should fall back to rssi
            mac="de:ad:00:00:00:02", hostname="phone", site_id=site,
            ap_mac="aa:aa:aa:aa:aa:aa", ap_serial="S-A", band="2.4",
            rssi=30, is_guest=True,
        ),
    ]
    return Snapshot(
        generated_at=1.0, ok=True,
        sites=[Site(id=site, name="HQ", num_aps=3, num_clients=2)],
        access_points=[ap_a, ap_b, ap_unplaced], clients=clients, floorplans=[fp],
    )


def _client() -> TestClient:
    settings = Settings(openintent_refresh_enabled=False)
    app = create_app(settings=settings, collector=_Fake(_snapshot(), {"plan1": PNG}))
    return TestClient(app)


def test_map_lists_plans_and_places_only_positioned_aps():
    m = _client().get("/api/map").json()
    assert m["selected"] == "plan1"
    plan = m["floorplans"][0]
    assert plan["num_placed_aps"] == 2
    assert plan["has_image"] is True
    assert plan["width_px"] == 1000 and plan["height_px"] == 600
    assert {a["name"] for a in m["access_points"]} == {"AP-A", "AP-B"}  # AP-C excluded


def test_map_embeds_clients_per_ap_with_signal_fallback():
    m = _client().get("/api/map").json()
    a = next(a for a in m["access_points"] if a["name"] == "AP-A")
    assert {c["mac"] for c in a["clients"]} == {
        "de:ad:00:00:00:01", "de:ad:00:00:00:02"
    }
    guest = next(c for c in a["clients"] if c["is_guest"])
    assert guest["signal_dbm"] == 30  # fell back to rssi when signal_dbm is None
    b = next(a for a in m["access_points"] if a["name"] == "AP-B")
    assert b["clients"] == []


def test_map_honors_explicit_floorplan_query():
    m = _client().get("/api/map", params={"floorplan": "plan1"}).json()
    assert m["selected"] == "plan1"


def test_floorplan_image_served_and_missing_404():
    c = _client()
    r = c.get("/api/floorplans/plan1/image")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content.startswith(b"\x89PNG")
    assert c.get("/api/floorplans/does-not-exist/image").status_code == 404
