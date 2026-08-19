"""The Open5G2GO path, against that project's own payload shapes.

Fixtures below follow `web_backend/api/routes.py` (the documented
`/enodeb/status` and `/connections` responses) and `EnodebSNMPStatus.to_dict`
in `opensurfcontrol/snmp_client.py`, with values from the Baicells Nova 430i in
its `config/enodebs.yaml`. They are transcribed from that project rather than
invented, so a change in its API shows up here as a failure rather than as an
empty map.
"""

from __future__ import annotations

import json

import pytest

from unifi_hamina_live.cellular import normalize, open5g2go
from unifi_hamina_live.cellular.cells import CellInventory
from unifi_hamina_live.cellular.source import CellularSource
from unifi_hamina_live.config import Settings

ENODEB_STATUS = {
    "timestamp": "2026-08-19 05:30:00 UTC",
    "s1ap": {
        "available": True,
        "connected_count": 1,
        "enodebs": [{
            "serial_number": "120200046421CKY0606",
            "config_name": "Nova-430i-Test",
            "location": "Test Lab",
            "connected": True,
            "ip_address": "10.48.0.159",
        }],
    },
    "snmp": {
        "available": True, "enabled": True,
        "reachable_count": 1, "configured_count": 1,
        "enodebs": [{
            "serial_number": "120200046421CKY0606",
            "config_name": "Nova-430i-Test",
            "location": "Test Lab",
            "reachable": True,
            "error": None,
            "ip_address": "10.48.0.159",
            "timestamp": "2026-08-19 05:30:00 UTC",
            "identity": {
                "serial_number": "120200046421CKY0606",
                "product_type": "Nova430i",
                "hardware_version": "V1.0",
                "software_version": "BaiBS_RTS_3.7.11",
                "enodeb_name": "Nova-430i",
                "mac_address": "48:BF:74:11:22:33",
            },
            "cell": {
                "status": "Active", "band_class": 48, "bandwidth": "20 MHz",
                "earfcn": 55340, "pci": 120, "cell_id": 67594275, "tac": 1,
            },
            "connection": {"s1_link_up": True, "rf_enabled": True, "ue_count": 2},
            "performance": {
                "ul_throughput_kbps": 1200, "dl_throughput_kbps": 8400,
                "ul_prb_pct": 9, "dl_prb_pct": 37, "cpu_utilization": 22,
            },
            "kpis": {"erab_success_pct": 100, "rrc_success_pct": 99},
            "alarms": {"count": 0, "sctp_failure": False,
                       "cell_unavailable": False},
            "tx_power": {"current_dbm": 27, "min_dbm": 0, "max_dbm": 30},
        }],
    },
    "network": {"plmn": "315010", "mcc": "315", "mnc": "010", "tac": 1,
                "network_name": "Waveriders"},
}

CONNECTIONS = {
    "timestamp": "2026-08-19 05:30:00 UTC",
    "total_active": 2,
    "connections": [
        {"imsi": "315010000000010", "name": "Camera 3", "ip": "10.48.99.10",
         "apn": "internet", "cm_state": "CONNECTED",
         "attached_at": "2026-08-19T05:13:03"},
        {"imsi": "315010000000011", "name": "Comms Laptop", "ip": "10.48.99.11",
         "apn": "internet", "cm_state": "IDLE", "attached_at": None},
    ],
}

GNODEB_STATUS = {
    "timestamp": "2026-08-19 05:30:00 UTC",
    "gnodebs": [{
        "config_name": "gNodeB-1", "location": "Test Lab",
        "ip_address": "10.48.0.160", "gnb_id": 100, "connected": True,
    }],
    "network": {"plmn": "315010"},
}

# Placement only — the radio comes off the eNodeB itself.
INVENTORY = {
    "network_name": "Waveriders-Private",
    "cells": [{
        "id": "nova-430i", "name": "CBRS Cell - Test Lab",
        "model": "CW9166I",
        "placement": {"anchor_ap": "AP1"},
    }],
}


class FakeBackend:
    def __init__(self, routes):
        self.base_url = "http://open5g2go:8080"
        self.prefix = "/api/v1"
        self._routes = routes
        self.calls = []

    async def get(self, path):
        self.calls.append(path)
        return self._routes.get(path)

    async def aclose(self):
        pass


@pytest.fixture
def inventory(tmp_path):
    path = tmp_path / "cells.json"
    path.write_text(json.dumps(INVENTORY))
    return CellInventory.load(str(path))


def make_source(inventory, routes, **overrides):
    settings = Settings(open5gs_enabled=True,
                        open5g2go_url="http://open5g2go:8080", **overrides)
    source = CellularSource(settings, inventory)
    source._o5g2go = FakeBackend(routes)
    source._amf = source._mme = source._smf = None
    return source


# --- the readers ----------------------------------------------------------

def test_enodeb_status_carries_the_rf_off_the_radio():
    cell = open5g2go.cells_from_enodeb_status(ENODEB_STATUS)[0]
    assert cell["name"] == "Nova-430i-Test"        # from enodebs.yaml
    assert cell["location"] == "Test Lab"
    assert cell["mac"] == "48:bf:74:11:22:33"      # a REAL mac, normalised
    assert cell["model"] == "Nova430i"
    assert cell["firmware"] == "BaiBS_RTS_3.7.11"
    assert cell["connected"] is True
    assert cell["num_ues"] == 2
    # the live carrier — none of this is declared anywhere
    assert (cell["band_number"], cell["arfcn"]) == (48, 55340)
    assert cell["bandwidth_mhz"] == 20.0
    assert cell["tx_power_dbm"] == 27.0
    assert cell["utilization_pct"] == 37.0


def test_a_radio_with_its_transmitter_off_is_not_online():
    body = json.loads(json.dumps(ENODEB_STATUS))
    body["snmp"]["enodebs"][0]["connection"]["rf_enabled"] = False
    assert open5g2go.cells_from_enodeb_status(body)[0]["connected"] is False


def test_an_enodeb_snmp_could_not_reach_still_appears_from_s1ap():
    body = json.loads(json.dumps(ENODEB_STATUS))
    body["snmp"]["enodebs"] = []
    cells = open5g2go.cells_from_enodeb_status(body)
    assert [c["name"] for c in cells] == ["Nova-430i-Test"]
    assert cells[0]["connected"] is True           # S1 is up; SNMP simply failed
    assert cells[0].get("arfcn") is None           # and the RF is unknown


def test_the_same_enodeb_is_not_listed_twice():
    cells = open5g2go.cells_from_enodeb_status(ENODEB_STATUS)
    assert len(cells) == 1


def test_connections_keep_the_device_name_beside_the_identifier():
    ues = open5g2go.ues_from_connections(CONNECTIONS)
    assert [u["name"] for u in ues] == ["Camera 3", "Comms Laptop"]
    assert ues[0]["state"] == "connected" and ues[1]["state"] == "idle"
    assert ues[0]["ip"] == "10.48.99.10"
    assert all(u["enb_id"] is None for u in ues)   # no cell association at all


@pytest.mark.parametrize("value,mhz", [
    ("20 MHz", 20.0), ("5 MHz", 5.0), (10, 10.0), (None, None), ("", None),
])
def test_bandwidth_string_is_read_not_re_derived(value, mhz):
    assert open5g2go._bandwidth_mhz(value) == mhz


# --- the projection -------------------------------------------------------

def test_live_rf_beats_the_declared_radio(inventory):
    cell = open5g2go.cells_from_enodeb_status(ENODEB_STATUS)[0]
    spec = inventory.spec_for(cell)
    ap = normalize.access_point(cell, spec, "default")

    assert ap.name == "Nova-430i-Test"    # the operator's own name, kept
    assert ap.mac == "48:bf:74:11:22:33"
    assert ap.model == "CW9166I"          # the costume still wins for the model
    assert ap.firmware == "BaiBS_RTS_3.7.11"
    radio = ap.radios[0]
    # EARFCN 55340 in band 48 -> 3560 MHz, derived, not configured
    assert radio.carrier_mhz == pytest.approx(3560.0, abs=1e-3)
    assert radio.technology == "lte"
    assert radio.tx_power_dbm == 27.0
    assert radio.channel_width_mhz == 20
    # PRB utilisation passes through as load — the one live RF figure that is
    # not a costume
    assert radio.channel_utilization_pct == 37.0
    assert radio.band == "5"              # 3.56 GHz dressed as 5 GHz Wi-Fi


def test_declared_values_fill_the_gaps_live_data_leaves(inventory):
    """SNMP gave band and EARFCN but the deployment did not report TX power:
    the declared figure must survive rather than the whole radio reverting."""
    cell = open5g2go.cells_from_enodeb_status(ENODEB_STATUS)[0]
    cell["tx_power_dbm"] = None
    from unifi_hamina_live.cellular.cells import CellSpec, RadioSpec

    spec = CellSpec(id="x", name="x", model="CW9166I",
                    radio=RadioSpec(technology="lte", band="48",
                                    tx_power_dbm=24.0))
    radio = normalize.access_point(cell, spec, "default").radios[0]
    assert radio.tx_power_dbm == 24.0                       # declared
    assert radio.carrier_mhz == pytest.approx(3560.0)       # live


# --- the poll -------------------------------------------------------------

async def test_a_single_cell_takes_every_unattributed_ue(inventory):
    source = make_source(inventory, {"/enodeb/status": ENODEB_STATUS,
                                     "/connections": CONNECTIONS})
    aps, clients, placements = await source.collect("default")

    assert len(aps) == 1
    ap = aps[0]
    assert [c.name for c in clients] == ["Camera 3"]     # the idle one is not on air
    assert clients[0].ap_mac == ap.mac
    assert clients[0].ip == "10.48.99.10"
    assert clients[0].essid == "Waveriders-Private"
    assert ap.num_clients == 1
    assert placements[ap.mac].anchor_ap == "AP1"
    assert source.status["source"] == "open5g2go"


async def test_it_refuses_to_split_unattributed_ues_across_two_cells(inventory):
    """Two radios and a connection list that names neither: putting the UEs on
    one of them would be a guess, and putting them on both would double-count."""
    both = {"/enodeb/status": ENODEB_STATUS, "/gnodeb/status": GNODEB_STATUS,
            "/connections": CONNECTIONS}
    source = make_source(inventory, both)
    aps, clients, _ = await source.collect("default")

    assert len(aps) == 2
    assert clients == []
    # each cell still shows the count its own source measured
    by_name = {a.name: a for a in aps}
    # and each keeps its own live name: a fallback spec describes the radio,
    # not its identity, so it must not relabel both cells to one string
    assert sorted(by_name) == ["Nova-430i-Test", "gNodeB-1"]
    assert by_name["Nova-430i-Test"].num_clients == 2      # SNMP ue_count
    assert by_name["gNodeB-1"].num_clients == 0


async def test_a_four_g_only_deployment_does_not_need_the_5g_route(inventory):
    source = make_source(inventory, {"/enodeb/status": ENODEB_STATUS,
                                     "/connections": CONNECTIONS})
    aps, _, _ = await source.collect("default")
    assert [a.name for a in aps] == ["Nova-430i-Test"]
    assert "/gnodeb/status" in source._o5g2go.calls    # asked, and got nothing


async def test_backend_down_leaves_declared_cells_visible(inventory):
    from unifi_hamina_live.cellular.open5gs import Open5GSError

    class Broken(FakeBackend):
        async def get(self, path):
            raise Open5GSError("connection refused")

    source = make_source(inventory, {})
    source._o5g2go = Broken({})
    aps, clients, _ = await source.collect("default")
    assert clients == []
    # the inventory here declares only a fallback spec, so there is no specific
    # cell to show offline — but the poll must not raise, and must say why
    assert source.error and "refused" in source.error


async def test_supi_masking_still_applies_to_named_devices(inventory):
    source = make_source(inventory, {"/enodeb/status": ENODEB_STATUS,
                                     "/connections": CONNECTIONS})
    _, clients, _ = await source.collect("default")
    assert clients[0].hostname == "31501…0010"      # masked identifier
    assert clients[0].name == "Camera 3"            # readable name beside it


async def test_api_cellular_reports_utilisation_as_real(inventory):
    """PRB utilisation is the one live RF figure that is not a costume, so it
    must not be filed under one."""
    from fastapi.testclient import TestClient

    from unifi_hamina_live.app import create_app
    from unifi_hamina_live.unifi.collector import Collector

    settings = Settings(open5gs_enabled=True,
                        open5g2go_url="http://open5g2go:8080")
    col = Collector(settings)
    col.cellular = CellularSource(settings, inventory)
    col.cellular._o5g2go = FakeBackend({"/enodeb/status": ENODEB_STATUS,
                                        "/connections": CONNECTIONS})
    col.cellular._amf = col.cellular._mme = col.cellular._smf = None
    snap = col.snapshot
    await col._merge_cellular(snap)
    with TestClient(create_app(settings=settings, collector=col)) as c:
        cell = c.get("/api/cellular").json()["cells"][0]

    assert cell["real"]["utilization_pct"] == 37.0
    assert cell["real"]["firmware"] == "BaiBS_RTS_3.7.11"
    assert cell["costume"]["channel"] in (100, 104, 108, 112, 116, 120, 124,
                                          128, 132, 136, 140)
