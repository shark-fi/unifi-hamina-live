"""Collector integration: placement flows into the snapshot via poll_once."""

import struct

from unifi_hamina_live.config import Settings
from unifi_hamina_live.unifi.collector import Collector


def _png(w, h):
    return (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
            + struct.pack(">II", w, h) + b"\x08\x06\x00\x00\x00")


class FakeClient:
    """Minimal async stand-in for UniFiClient."""

    def __init__(self, *, maps=None, project=None):
        self._maps = maps or {}
        self._project = project

    async def login(self):
        pass

    async def aclose(self):
        pass

    async def sites(self):
        return [{"name": "default", "desc": "HQ"}]

    async def devices(self, site):
        return [{
            "type": "uap", "name": "AP1", "mac": "aa:bb:cc:00:11:22",
            "model": "U7PRO", "state": 1, "user-num_sta": 0,
            "map_id": "m1", "x": 120, "y": 340,
            "radio_table_stats": [{"radio": "na", "channel": 36, "tx_power": 20}],
        }]

    async def clients(self, site):
        return []

    async def maps(self, site):
        return self._maps

    async def innerspace_project(self):
        return self._project

    async def get_bytes(self, url):
        return _png(1000, 800)


async def test_legacy_placement_flows_into_snapshot():
    maps = {"m1": {"_id": "m1", "name": "Floor 1", "upp": 0.05,
                   "width": 1000, "height": 800}}
    col = Collector(Settings(), client_factory=lambda: FakeClient(maps=maps))
    snap = await col.poll_once()
    assert snap.ok
    assert [f.id for f in snap.floorplans] == ["m1"]
    ap = snap.access_points[0]
    assert ap.floorplan_id == "m1" and ap.x == 120.0 and ap.y == 340.0
    assert snap.floorplans[0].num_aps == 1


async def test_innerspace_placement_flows_into_snapshot():
    project = {
        "plans": [{"id": "p1", "title": "Ground"}],
        "products": [{"id": "prodA", "category": "wifi"}],
        "shapes": [
            {"type": "map", "planId": "p1", "urlImage": "/dl/p1.png",
             "position": [{"x": 0, "y": 0}], "scale": {"x": 1, "y": 1}},
            {"type": "device", "planId": "p1", "productId": "prodA",
             "position": [{"x": 100, "y": 50}], "meta": {"mac": "aa:bb:cc:00:11:22"}},
        ],
    }
    # no classic maps -> falls back to InnerSpace
    col = Collector(Settings(), client_factory=lambda: FakeClient(maps={}, project=project))
    snap = await col.poll_once()
    ap = snap.access_points[0]
    assert ap.floorplan_id == "p1"
    assert (ap.x, ap.y) == (600.0, 450.0)
    fp = next(f for f in snap.floorplans if f.id == "p1")
    assert fp.site_id == "default" and fp.width_px == 1000
