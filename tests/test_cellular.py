"""The cellular source, against the payloads Open5GS actually serves.

The fixtures below are the documented responses from the Open5GS 2.7.7 metrics
server (``/gnb-info``, ``/enb-info``, ``/ue-info``, ``/pdu-info``), trimmed to
the fields consumed here. They are copied from the dumpers' own docstrings
rather than invented, so a change in the core's shape shows up as a test failure
instead of an empty map.
"""

from __future__ import annotations

import httpx
import json

import pytest

from unifi_hamina_live.cellular import normalize, open5g2go, open5gs, prom
from unifi_hamina_live.cellular.open5gs import describe
from unifi_hamina_live.cellular.cells import CellInventory
from unifi_hamina_live.cellular.source import CellularSource
from unifi_hamina_live.config import Settings

GNB_INFO = {
    "items": [{
        "gnb_id": 100,
        "name": "srsgnb01",
        "plmn": "99970",
        "network": {"amf_name": "open5gs-amf0", "ngap_port": 38412},
        "ng": {"sctp": {"peer": "[192.168.168.100]:60110", "max_out_streams": 2,
                        "next_ostream_id": 1},
               "setup_success": True},
        "supported_ta_list": [{
            "tac": "000001",
            "bplmns": [{"plmn": "99970", "snssai": [{"sst": 1, "sd": "ffffff"}]}],
        }],
        "num_connected_ues": 2,
    }],
    "pager": {"page": 0, "page_size": 100, "count": 1},
}

UE_INFO = {
    "items": [
        {
            "supi": "imsi-999700000000001",
            "pei": "imeisv-3535938300494715",
            "cm_state": "connected",
            "gnb": {"amf_ue_ngap_id": 2, "ran_ue_ngap_id": 39,
                    "gnb_id": 100, "cell_id": 1},
            "location": {
                "nr_tai": {"plmn": "99970", "tac_hex": "000001", "tac": 1},
                "nr_cgi": {"plmn": "99970", "nci": 1638401, "gnb_id": 100,
                           "cell_id": 1},
            },
            "pdu_sessions": [{"psi": 1, "dnn": "internet"}],
            "pdu_sessions_count": 1,
        },
        {
            "supi": "imsi-999700000000002",
            "cm_state": "connected",
            "gnb": {"gnb_id": 100, "cell_id": 1},
            "location": {"nr_cgi": {"plmn": "99970", "gnb_id": 100, "cell_id": 1}},
            "pdu_sessions": [],
            "pdu_sessions_count": 0,
        },
        {
            # Registered but with no RRC connection: no cell is carrying it.
            "supi": "imsi-999700000000003",
            "cm_state": "idle",
            "gnb": {"gnb_id": 100, "cell_id": 1},
            "location": {"nr_cgi": {"plmn": "99970", "gnb_id": 100, "cell_id": 1}},
        },
    ],
    "pager": {"page": 0, "page_size": 100, "count": 3},
}

PDU_INFO = {
    "items": [{
        "supi": "imsi-999700000000001",
        "pdu": [{"psi": 1, "dnn": "internet", "ipv4": "10.45.0.11",
                 "pdu_state": "active"}],
        "ue_activity": "active",
    }],
    "pager": {"page": 0, "page_size": 100, "count": 1},
}

ENB_INFO = {
    "items": [{
        "enb_id": 264040,
        "plmn": "99970",
        "network": {"mme_name": "open5gs-mme0"},
        "s1": {"sctp": {"peer": "[192.168.168.254]:36412"}, "setup_success": True},
        "supported_ta_list": [{"tac": "0001", "plmn": "99970"}],
        "num_connected_ues": 1,
    }],
    "pager": {"page": 0, "page_size": 100, "count": 1},
}

MME_UE_INFO = {
    "items": [{
        "supi": "999700000021632",
        "domain": "EPS",
        "rat": "E-UTRA",
        "cm_state": "connected",
        "enb": {"enb_id": 264040, "cell_id": 67594275},
        "location": {"tai": {"plmn": "99970", "tac_hex": "0001", "tac": 1}},
        "pdn": [{"apn": "internet", "qci": 9, "ebi": 5}],
        "pdn_count": 1,
    }],
    "pager": {"page": 0, "page_size": 100, "count": 1},
}

AMF_METRICS = """\
# HELP ran_ue RAN UEs
# TYPE ran_ue gauge
ran_ue 2
# HELP gnb gNodeBs
# TYPE gnb gauge
gnb 1
# HELP fivegs_amffunction_rm_registeredsubnbr Number of registered subscribers
# TYPE fivegs_amffunction_rm_registeredsubnbr gauge
fivegs_amffunction_rm_registeredsubnbr{plmnid="99970",snssai="1-ffffff"} 3
"""

INVENTORY = {
    "network_name": "SharkFi-Private",
    "cells": [
        {
            "id": "gnb-100", "name": "CBRS Cell - Warehouse",
            "match": {"gnb_id": 100}, "model": "CW9166I",
            "radio": {"technology": "nr", "band": "n48", "arfcn": 636667,
                      "bandwidth_mhz": 40, "tx_power_dbm": 30},
            "placement": {"anchor_ap": "AP-Warehouse"},
        },
        {
            "id": "enb-264040", "name": "LTE Cell - Yard",
            "match": {"enb_id": 264040}, "model": "CW9166I",
            "radio": {"technology": "lte", "band": "48", "arfcn": 55340,
                      "bandwidth_mhz": 20, "tx_power_dbm": 27},
            "placement": {"floorplan": "plan-1", "x_px": 980, "y_px": 410},
        },
    ],
}


@pytest.fixture
def inventory(tmp_path):
    path = tmp_path / "cells.json"
    path.write_text(json.dumps(INVENTORY))
    return CellInventory.load(str(path))


class FakeNF:
    """Stands in for one NF's metrics server."""

    def __init__(self, info=None, metrics_text="", missing=()):
        self.base_url = "http://fake:9090"
        self._info = info or {}
        self._metrics = metrics_text
        self._missing = set(missing)
        self.calls = []

    def supports(self, path):
        return path not in self._missing

    async def info(self, path):
        self.calls.append(path)
        if path in self._missing:
            return None
        body = self._info.get(path)
        return None if body is None else body["items"]

    async def metrics(self):
        return prom.parse(self._metrics)

    async def aclose(self):
        pass


def make_source(inventory, amf=None, mme=None, smf=None, **overrides):
    settings = Settings(open5gs_enabled=True, **overrides)
    source = CellularSource(settings, inventory)
    source._amf, source._mme, source._smf = amf, mme, smf
    return source


# --- the payload readers --------------------------------------------------

def test_gnb_info_becomes_a_cell_record():
    cells = open5gs.cells_from_gnb_info(GNB_INFO["items"])
    assert cells == [{
        "technology": "nr", "gnb_id": 100, "name": "srsgnb01", "plmn": "99970",
        "tac": "000001", "tacs": ["000001"],
        "peer": "[192.168.168.100]:60110", "connected": True, "num_ues": 2,
        "slices": ["sst=1/sd=ffffff"],
    }]


def test_enb_info_becomes_the_same_shape():
    cell = open5gs.cells_from_enb_info(ENB_INFO["items"])[0]
    assert cell["technology"] == "lte"
    assert cell["enb_id"] == 264040
    assert cell["num_ues"] == 1
    assert cell["connected"] is True


def test_ue_info_reads_the_cell_out_of_the_nr_cgi():
    ues = open5gs.ues_from_ue_info(UE_INFO["items"])
    assert [u["supi"] for u in ues] == [
        "imsi-999700000000001", "imsi-999700000000002", "imsi-999700000000003"]
    assert ues[0]["gnb_id"] == 100
    assert ues[0]["nci"] == 1638401
    assert ues[0]["tac"] == "000001"
    assert ues[0]["dnn"] == "internet"
    assert ues[2]["state"] == "idle"


def test_mme_ue_info_flattens_to_the_same_vocabulary():
    ue = open5gs.ues_from_ue_info(MME_UE_INFO["items"])[0]
    assert ue["technology"] == "lte"
    assert ue["enb_id"] == 264040
    assert ue["dnn"] == "internet"       # from `pdn[].apn`, not `pdu_sessions`
    assert ue["tac"] == "0001"


def test_pdu_info_joins_on_supi_whether_or_not_it_is_prefixed():
    sessions = open5gs.sessions_from_pdu_info(PDU_INFO["items"])
    assert "999700000000001" in sessions          # `imsi-` prefix stripped
    assert sessions["999700000000001"]["ip"] == "10.45.0.11"


# --- the projection -------------------------------------------------------

def test_cell_becomes_an_access_point_wearing_a_costume(inventory):
    cell = open5gs.cells_from_gnb_info(GNB_INFO["items"])[0]
    ap = normalize.access_point(cell, inventory.spec_for(cell), "default")
    assert ap.name == "CBRS Cell - Warehouse"
    assert ap.source == "cellular"
    assert ap.mac.startswith(normalize.CELL_MAC_PREFIX)
    assert ap.ip == "192.168.168.100"      # the gNB's own address, off the SCTP peer
    assert ap.online is True
    assert ap.model == "CW9166I"
    radio = ap.radios[0]
    # the costume
    assert radio.band == "5"
    assert radio.channel in (100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140)
    assert radio.channel_width_mhz == 40
    # and the truth kept beside it
    assert radio.technology == "nr"
    assert radio.carrier_mhz == pytest.approx(3550.005, abs=1e-3)
    assert "n48" in radio.carrier_label and "636667" in radio.carrier_label
    assert radio.tx_power_dbm == 30


def test_a_cell_with_no_spec_gets_no_invented_radio():
    cell = open5gs.cells_from_gnb_info(GNB_INFO["items"])[0]
    ap = normalize.access_point(cell, None, "default")
    assert ap.radios == []
    assert ap.name == "srsgnb01"           # the gNB's own RAN node name
    assert ap.model == "NR-CELL"


def test_mac_is_locally_administered_and_stable():
    cell = open5gs.cells_from_gnb_info(GNB_INFO["items"])[0]
    first_octet = int(normalize.cell_mac(cell).split(":")[0], 16)
    assert first_octet & 0b10       # locally administered bit
    assert normalize.cell_mac(cell) == normalize.cell_mac(dict(cell))
    # a different gNB is a different cell
    assert normalize.cell_mac({**cell, "gnb_id": 101}) != normalize.cell_mac(cell)


def test_supi_is_masked_by_default():
    assert normalize.mask_supi("999700000000001") == "99970…0001"
    assert normalize.mask_supi("12345") == "12345"     # too short to mask


def test_client_carries_no_invented_signal(inventory):
    cell = open5gs.cells_from_gnb_info(GNB_INFO["items"])[0]
    ap = normalize.access_point(cell, inventory.spec_for(cell), "default")
    ue = open5gs.ues_from_ue_info(UE_INFO["items"])[0]
    client = normalize.client(ue, ap, "default", "SharkFi-Private",
                              session={"ip": "10.45.0.11"})
    assert client.ap_mac == ap.mac
    assert client.ip == "10.45.0.11"
    assert client.essid == "SharkFi-Private"
    assert client.band == "5" and client.channel == ap.radios[0].channel
    # a core never sees the air, so these stay empty rather than being guessed
    assert client.signal_dbm is None and client.rssi is None
    assert client.tx_rate_kbps is None


# --- the poll -------------------------------------------------------------

async def test_collect_joins_ues_to_their_cell(inventory):
    amf = FakeNF({"/gnb-info": GNB_INFO, "/ue-info": UE_INFO})
    smf = FakeNF({"/pdu-info": PDU_INFO})
    source = make_source(inventory, amf=amf, smf=smf)

    aps, clients, placements = await source.collect("default")

    live = [a for a in aps if a.online]
    assert len(live) == 1
    assert live[0].num_clients == 2          # the idle UE is not on a radio
    assert len(clients) == 2
    assert {c.ap_mac for c in clients} == {live[0].mac}
    assert [c.ip for c in clients if c.ip] == ["10.45.0.11"]
    assert placements[live[0].mac].anchor_ap == "AP-Warehouse"
    assert source.status["ue_detail"] is True


async def test_idle_ues_can_be_included(inventory):
    amf = FakeNF({"/gnb-info": GNB_INFO, "/ue-info": UE_INFO})
    source = make_source(inventory, amf=amf, open5gs_include_idle_ues=True)
    aps, clients, _ = await source.collect("default")
    assert len(clients) == 3
    assert [a for a in aps if a.online][0].num_clients == 3


async def test_declared_cell_the_core_is_not_reporting_goes_offline(inventory):
    # Only the gNB is connected; the declared eNB must still be on the map.
    amf = FakeNF({"/gnb-info": GNB_INFO, "/ue-info": UE_INFO})
    source = make_source(inventory, amf=amf)
    aps, _, placements = await source.collect("default")
    offline = [a for a in aps if not a.online]
    assert [a.name for a in offline] == ["LTE Cell - Yard"]
    assert offline[0].num_clients == 0
    assert placements[offline[0].mac].floorplan == "plan-1"


async def test_a_core_that_is_down_leaves_the_cells_visible_but_empty(inventory):
    class Broken(FakeNF):
        async def info(self, path):
            raise open5gs.Open5GSError("connection refused")

    source = make_source(inventory, amf=Broken())
    aps, clients, _ = await source.collect("default")
    assert clients == []
    assert [a.online for a in aps] == [False, False]
    assert source.error and "refused" in source.error


async def test_pre_277_core_falls_back_to_metrics_totals(inventory):
    """No dumpers on the core: one declared cell still gets its client count."""
    single = CellInventory(
        site_id="", network_name="",
        specs=(inventory.specs[0],),          # the 5G cell only
    )
    amf = FakeNF(metrics_text=AMF_METRICS,
                 missing=("/gnb-info", "/ue-info"))
    source = make_source(single, amf=amf)
    aps, clients, _ = await source.collect("default")
    assert len(aps) == 1
    assert aps[0].num_clients == 2            # `ran_ue` from /metrics
    assert clients == []                       # no per-UE detail on this core
    assert source.status["ue_detail"] is False


async def test_pre_277_core_refuses_to_split_a_count_it_cannot_attribute(inventory):
    """Two declared cells, core-wide totals only: reporting the same UEs on both
    would be worse than reporting none."""
    amf = FakeNF(metrics_text=AMF_METRICS, missing=("/gnb-info", "/ue-info"))
    mme = FakeNF(metrics_text="enb_ue 4\nenb 1\n", missing=("/enb-info", "/ue-info"))
    inv = CellInventory(site_id="", network_name="", specs=(
        inventory.specs[0],
        # a second 5G cell, so the AMF total is ambiguous
        inventory.specs[0].__class__(
            id="gnb-101", name="Second", model="CW9166I",
            match={"gnb_id": 101}, radio=inventory.specs[0].radio,
            placement=inventory.specs[0].placement),
        inventory.specs[1],
    ))
    source = make_source(inv, amf=amf, mme=mme)
    aps, _, _ = await source.collect("default")
    by_name = {a.name: a for a in aps}
    assert by_name["CBRS Cell - Warehouse"].num_clients == 0
    assert by_name["Second"].num_clients == 0
    # the single 4G cell is unambiguous, so it does get its total
    assert by_name["LTE Cell - Yard"].num_clients == 4


async def test_four_g_and_five_g_cells_coexist(inventory):
    amf = FakeNF({"/gnb-info": GNB_INFO, "/ue-info": UE_INFO})
    mme = FakeNF({"/enb-info": ENB_INFO, "/ue-info": MME_UE_INFO})
    source = make_source(inventory, amf=amf, mme=mme)
    aps, clients, _ = await source.collect("default")
    assert sorted(a.name for a in aps) == ["CBRS Cell - Warehouse", "LTE Cell - Yard"]
    assert all(a.online for a in aps)
    lte = next(a for a in aps if a.name == "LTE Cell - Yard")
    assert lte.num_clients == 1
    assert len(clients) == 3
    assert {c.ap_mac for c in clients} == {a.mac for a in aps}


async def test_switching_the_ue_list_off_keeps_the_core_s_own_count(inventory):
    """No subscriber identifiers published, but the cell must still show how
    many are on it — not zero because we declined to fetch the list."""
    amf = FakeNF({"/gnb-info": GNB_INFO, "/ue-info": UE_INFO})
    source = make_source(inventory, amf=amf, open5gs_include_ues=False)
    aps, clients, _ = await source.collect("default")
    assert clients == []
    assert "/ue-info" not in amf.calls
    assert next(a for a in aps if a.online).num_clients == 2


# --- transport errors that say what happened ------------------------------

def test_a_timeout_is_named_even_though_httpx_gives_no_message():
    """httpx raises ConnectTimeout with an empty str(), which rendered as a
    URL, a colon and nothing — indistinguishable from a truncated log line.

    Seen live: `open5gs poll failed: http://open5g2go-backend:8000/...: `
    """
    assert describe(httpx.ConnectTimeout("")) == "ConnectTimeout"
    assert describe(httpx.ReadTimeout("")) == "ReadTimeout"


def test_a_real_message_is_kept():
    assert describe(httpx.ConnectError("[Errno 111] Connection refused")) == (
        "[Errno 111] Connection refused")


# --- an eNodeB SNMP cannot reach but S1AP can -----------------------------
#
# Trimmed from the nuc's open5g2go backend, where this drew a cell as OFFLINE
# on the map while it carried six attached UEs. The serial is in BOTH lists —
# SNMP as an unreachable entry — so the S1AP fallback never fired for it.

UNREACHABLE_SNMP_BUT_S1_UP = {
    "network": {"plmn": "315010"},
    "snmp": {
        "available": True, "enabled": True,
        "reachable_count": 0, "configured_count": 1,
        "enodebs": [{
            "serial_number": "1202000291217RB0860",
            "config_name": "Neutrino-eNodeB", "location": "WLPC",
            "reachable": False,
            "error": "No SNMP response received before timeout",
            # note: a DIFFERENT address than the one S1AP connected from —
            # which is why SNMP times out in the first place
            "ip_address": "192.168.1.103",
            "identity": {"serial_number": None, "product_type": None},
        }],
    },
    "s1ap": {
        "available": True, "connected_count": 1,
        "enodebs": [{
            "serial_number": "1202000291217RB0860",
            "config_name": "Neutrino-eNodeB", "location": "WLPC",
            "ip_address": "10.10.5.103", "port": 36412, "connected": True,
        }],
    },
}


def test_a_cell_snmp_cannot_reach_is_up_when_s1ap_says_so():
    """The S1 link is the authority on whether the eNodeB is serving."""
    cells = open5g2go.cells_from_enodeb_status(UNREACHABLE_SNMP_BUT_S1_UP)
    assert len(cells) == 1                       # not duplicated across lists
    assert cells[0]["connected"] is True
    assert cells[0]["serial"] == "1202000291217RB0860"


def test_the_s1ap_address_is_used_when_snmp_has_no_reachable_one():
    cells = open5g2go.cells_from_enodeb_status(UNREACHABLE_SNMP_BUT_S1_UP)
    assert cells[0]["peer"] == "10.10.5.103"


def test_an_unreachable_cell_with_no_s1_link_is_still_offline():
    """Don't paint everything green: no S1AP entry means nothing is serving."""
    body = json.loads(json.dumps(UNREACHABLE_SNMP_BUT_S1_UP))
    body["s1ap"] = {"available": True, "connected_count": 0, "enodebs": []}
    assert open5g2go.cells_from_enodeb_status(body)[0]["connected"] is False


def test_an_s1ap_entry_that_is_not_connected_does_not_revive_it():
    body = json.loads(json.dumps(UNREACHABLE_SNMP_BUT_S1_UP))
    body["s1ap"]["enodebs"][0]["connected"] = False
    assert open5g2go.cells_from_enodeb_status(body)[0]["connected"] is False
