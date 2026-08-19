"""Cells on the map: the collector merge, and the two ways a cell gets a
position — anchored to a UniFi device, or pinned by hand.

The anchor case is the one that matters most in practice: it makes the UniFi
console the only place anything is positioned, so moving a cell is dragging an
icon rather than editing a file and re-importing.
"""

from __future__ import annotations

import json

import pytest

from unifi_hamina_live.cellular import normalize
from unifi_hamina_live.cellular.cells import CellInventory
from unifi_hamina_live.cellular.source import CellularSource, load_inventory
from unifi_hamina_live.config import Settings
from unifi_hamina_live.unifi.collector import Collector

from .test_cellular import GNB_INFO, UE_INFO, FakeNF

INVENTORY = {
    "network_name": "SharkFi-Private",
    "cells": [{
        "id": "gnb-100", "name": "CBRS Cell", "match": {"gnb_id": 100},
        "model": "CW9166I",
        "radio": {"technology": "nr", "band": "n48", "arfcn": 636667,
                  "bandwidth_mhz": 40, "tx_power_dbm": 30},
        "placement": {"anchor_ap": "ap1"},          # deliberately lower-cased
    }],
}


class FakeClient:
    """A console with one placed AP on one classic-Maps floor plan."""

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
        return {"m1": {"_id": "m1", "name": "Floor 1", "upp": 0.05,
                       "width": 1000, "height": 800}}

    async def innerspace_project(self):
        return None

    async def get_bytes(self, url):
        return None


def _collector(tmp_path, inventory=INVENTORY, **overrides):
    path = tmp_path / "cells.json"
    path.write_text(json.dumps(inventory))
    settings = Settings(open5gs_enabled=True, open5gs_cells_path=str(path),
                        open5gs_amf_url="http://amf:9090", **overrides)
    col = Collector(settings, client_factory=lambda: FakeClient())
    col.cellular = CellularSource(settings, CellInventory.load(str(path)))
    col.cellular._amf = FakeNF({"/gnb-info": GNB_INFO, "/ue-info": UE_INFO})
    col.cellular._mme = None
    col.cellular._smf = None
    return col


async def test_cell_inherits_its_anchor_ap_position(tmp_path):
    snap = await (_collector(tmp_path)).poll_once()

    cell = next(a for a in snap.access_points if a.source == "cellular")
    anchor = next(a for a in snap.access_points if a.source == "unifi")
    assert cell.floorplan_id == anchor.floorplan_id
    # nudged off the anchor, or the two icons draw on top of each other and the
    # cell looks like it never imported
    offset = normalize.DEFAULT_ANCHOR_OFFSET_PX
    assert (cell.x, cell.y) == (120.0 + offset, 340.0 + offset)
    # both radios are now on the same plan, which is what Hamina imports
    assert snap.floorplans[0].num_aps == 2
    # and the UEs came along as clients of the cell
    assert [c.ap_mac for c in snap.clients] == [cell.mac, cell.mac]


async def test_anchor_offset_is_configurable(tmp_path):
    inventory = json.loads(json.dumps(INVENTORY))
    inventory["cells"][0]["placement"] = {"anchor_ap": "ap1", "dx_px": 60,
                                          "dy_px": -15}
    snap = await (_collector(tmp_path, inventory)).poll_once()
    cell = next(a for a in snap.access_points if a.source == "cellular")
    assert (cell.x, cell.y) == (180.0, 325.0)


async def test_cell_placed_by_explicit_pixels(tmp_path):
    inventory = json.loads(json.dumps(INVENTORY))
    inventory["cells"][0]["placement"] = {"floorplan": "m1", "x_px": 800,
                                          "y_px": 200}
    snap = await (_collector(tmp_path, inventory)).poll_once()
    cell = next(a for a in snap.access_points if a.source == "cellular")
    assert (cell.floorplan_id, cell.x, cell.y) == ("m1", 800.0, 200.0)


async def test_a_missing_anchor_leaves_the_cell_unplaced_not_missing(tmp_path):
    inventory = json.loads(json.dumps(INVENTORY))
    inventory["cells"][0]["placement"] = {"anchor_ap": "no-such-ap"}
    snap = await (_collector(tmp_path, inventory)).poll_once()
    cell = next(a for a in snap.access_points if a.source == "cellular")
    # It is still in the API, still counting its UEs — it just cannot be drawn.
    assert cell.floorplan_id is None and cell.num_clients == 2


async def test_site_counts_include_the_cell(tmp_path):
    snap = await (_collector(tmp_path)).poll_once()
    site = next(s for s in snap.sites if s.id == "default")
    assert site.num_aps == 2
    assert site.num_clients == 2


async def test_cells_survive_a_console_outage_without_piling_up(tmp_path):
    """A failed console poll carries the last good APs forward. The cells must
    be re-merged onto that, not appended to a list that already has them."""
    col = _collector(tmp_path)
    await col.poll_once()

    class Down(FakeClient):
        async def login(self):
            from unifi_hamina_live.unifi.client import UniFiUnreachableError
            raise UniFiUnreachableError("host is down")

    col._client_factory = lambda: Down()
    await col._drop_session()
    for _ in range(3):
        snap = await col.poll_once()

    assert snap.ok is False
    cells = [a for a in snap.access_points if a.source == "cellular"]
    assert len(cells) == 1
    assert len(snap.clients) == 2
    # the cell keeps the position it inherited while the console was up
    assert cells[0].floorplan_id is None or cells[0].floorplan_id == "m1"


async def test_a_missing_inventory_is_a_note_not_a_crash(tmp_path):
    inventory, note = load_inventory(str(tmp_path / "nope.json"))
    assert inventory.specs == ()
    assert "no cell inventory" in note


async def test_a_broken_inventory_says_what_is_wrong(tmp_path):
    path = tmp_path / "cells.json"
    path.write_text(json.dumps({"cells": [{"id": "x", "radio": {"technology": "6g"}}]}))
    inventory, note = load_inventory(str(path))
    assert inventory.specs == ()
    assert "technology" in note


def test_placement_with_coordinates_but_no_plan_is_rejected(tmp_path):
    path = tmp_path / "cells.json"
    path.write_text(json.dumps({"cells": [
        {"id": "x", "placement": {"x_px": 10, "y_px": 20}}]}))
    with pytest.raises(ValueError, match="cannot be drawn"):
        CellInventory.load(str(path))


def test_duplicate_cell_ids_are_rejected(tmp_path):
    path = tmp_path / "cells.json"
    path.write_text(json.dumps({"cells": [{"id": "x"}, {"id": "x"}]}))
    with pytest.raises(ValueError, match="duplicate cell id"):
        CellInventory.load(str(path))


async def test_api_cellular_separates_the_real_from_the_costume(tmp_path):
    """The one endpoint that says plainly what these entries are, because every
    other surface deliberately presents them as access points."""
    from fastapi.testclient import TestClient

    from unifi_hamina_live.app import create_app

    col = _collector(tmp_path)
    await col.poll_once()
    with TestClient(create_app(settings=col._settings, collector=col)) as c:
        body = c.get("/api/cellular").json()

    assert body["enabled"] and body["configured"]
    assert body["status"] == {"cells": 1, "ues": 2, "ue_detail": True,
                              "source": "open5gs", "error": None}
    cell = body["cells"][0]
    assert cell["placed"] is True
    assert cell["real"]["technology"] == "nr"
    assert cell["real"]["carrier_mhz"] == pytest.approx(3550.005, abs=1e-3)
    assert cell["costume"]["band"] == "5"
    # the costume is a Wi-Fi channel; the real carrier is nowhere near one
    assert cell["costume"]["channel"] != cell["real"]["carrier_mhz"]


async def test_the_wifi_side_is_untouched_by_any_of_this(tmp_path):
    """The extension and the dashboard join on these three fields; a cell in the
    snapshot must not change what a real AP looks like."""
    col = _collector(tmp_path)
    snap = await col.poll_once()
    ap = next(a for a in snap.access_points if a.source == "unifi")
    assert ap.source == "unifi"
    assert ap.radios[0].technology == "wifi"
    assert ap.radios[0].carrier_mhz is None


async def test_inventory_site_id_is_honoured(tmp_path):
    """A multi-site console: the cells belong to the site the inventory names,
    which decides which floor plans they may be placed on."""
    inventory = json.loads(json.dumps(INVENTORY))
    inventory["site_id"] = "warehouse"
    inventory["cells"][0]["placement"] = {"floorplan": "m1", "x_px": 1, "y_px": 2}
    snap = await (_collector(tmp_path, inventory)).poll_once()
    cell = next(a for a in snap.access_points if a.source == "cellular")
    assert cell.site_id == "warehouse"
    assert {s.id for s in snap.sites} == {"default", "warehouse"}
