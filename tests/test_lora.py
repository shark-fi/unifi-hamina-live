"""The LoRa source, against the RouterOS API payloads a wAP LR8 serves.

The ``LORA`` fixture is a real ``/lora/print detail`` row captured from the
gateway; ``ETH``/``BOARD``/``SERVERS`` are the companion replies the source
reads for identity, model and the upstream-server label. Copied rather than
invented, so a change in the box's shape shows up here as a failure instead of
an empty map.
"""

from __future__ import annotations

import asyncio

import pytest

from unifi_hamina_live.config import Settings
from unifi_hamina_live.lora import normalize, rf
from unifi_hamina_live.lora.api import (RouterOSAuthError, RouterOSClient,
                                        RouterOSUnreachableError,
                                        _encode_length)
from unifi_hamina_live.lora.source import LoraSource, LoraSpec

LORA = {
    "name": "WLPC-Gateway", "status": "Enabled",
    "hardware-id": "3235313254002800", "gateway-id": "3235313254002800",
    "servers": "DockerNUC", "channel-plan": "us-915-1", "antenna-gain": "0",
    "forward": "crc-valid,crc-error", "network": "public",
    "lbt-enabled": "false", "disabled": "false",
}
ETH = {"name": "ether1", "mac-address": "2C:C8:1B:01:5F:A1"}
BOARD = {"board-name": "wAP R", "model": "RBwAPR-2nD"}
SERVERS = [
    {"name": "TTN-US", "address": "us.mikrotik.thethings.industries"},
    {"name": "DockerNUC", "address": "10.10.5.147"},
]


# --- the projection -------------------------------------------------------

def test_ap_uses_the_boxes_ethernet_mac_as_identity():
    ap = normalize.access_point(LORA, "2C:C8:1B:01:5F:A1", "site1",
                                model="RBwAPR-2nD", server="DockerNUC")
    assert ap.mac == "2c:c8:1b:01:5f:a1", "the real ether MAC, lowercased"
    assert ap.source == "lora"
    assert ap.online is True
    assert ap.name == "WLPC-Gateway"
    assert ap.model == "RBwAPR-2nD"
    assert ap.num_clients == 0
    radio = ap.radios[0]
    assert radio.technology == "lora"


def test_the_costume_is_out_of_the_way_and_stable():
    # LoRa is sub-GHz; drawing it on a real Wi-Fi channel would read as a
    # co-channel neighbour of a real AP. It wears a 5 GHz DFS channel instead,
    # and the same one every poll (keyed on the gateway EUI).
    ap = normalize.access_point(LORA, "2C:C8:1B:01:5F:A1", "site1")
    radio = ap.radios[0]
    assert radio.band == "5"
    assert radio.channel in rf.CHANNEL_POOL
    assert radio.channel_width_mhz == 20
    again = normalize.access_point(LORA, "2C:C8:1B:01:5F:A1", "site1")
    assert again.radios[0].channel == radio.channel


def test_the_sub_band_is_labelled_not_promoted_to_a_carrier():
    # A gateway spans a whole sub-band, so there is no single centre frequency
    # to report; carrier_mhz must stay None and the band ride on the label.
    radio = normalize.access_point(LORA, "2C:C8:1B:01:5F:A1", "site1",
                                   server="DockerNUC").radios[0]
    assert radio.carrier_mhz is None
    label = radio.carrier_label
    assert "LoRaWAN gateway" in label
    assert "US915" in label and "902-928 MHz" in label
    assert "us-915-1" in label
    assert "3235313254002800" in label
    assert "DockerNUC" in label


def test_a_disabled_gateway_is_not_online():
    # A box that is reachable but with the LoRa interface disabled is not
    # transmitting, so it must not draw as a live radio.
    disabled = {**LORA, "disabled": "true"}
    ap = normalize.access_point(disabled, "2C:C8:1B:01:5F:A1", "site1")
    assert ap.online is False
    assert ap.radios == [], "an offline gateway advertises no radio"


def test_band_mapping_covers_the_common_regions():
    assert "EU868" in rf.band_for_plan("eu-868")
    assert "AU915" in rf.band_for_plan("au-915")
    assert "AS923" in rf.band_for_plan("as-923-1")
    assert rf.band_for_plan("no-such-plan") is None
    assert rf.band_for_plan(None) is None


# --- the RouterOS API client ----------------------------------------------

def _encode_sentence(words: list[str]) -> bytes:
    out = bytearray()
    for w in words:
        wb = w.encode()
        out += _encode_length(len(wb)) + wb
    out += b"\x00"
    return bytes(out)


class _FakeWriter:
    def __init__(self) -> None:
        self.buf = bytearray()

    def write(self, b: bytes) -> None:
        self.buf += b

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


def _client_over(sentences: list[list[str]]) -> RouterOSClient:
    """A client whose connection replays ``sentences`` (in order) as the box's
    replies, so a whole poll can be driven without a gateway on the network."""
    reader = asyncio.StreamReader()
    for s in sentences:
        reader.feed_data(_encode_sentence(s))
    reader.feed_eof()
    client = RouterOSClient("10.0.0.1", "admin", "admin")
    client._reader = reader

    # _writer stays None so the first command triggers login (which consumes the
    # leading "!done"); connect is a no-op that only installs the fake writer,
    # leaving the pre-loaded reader in place.
    async def _noop_connect() -> None:
        client._writer = _FakeWriter()
    client._connect = _noop_connect  # type: ignore[method-assign]
    return client


def test_length_prefix_round_trips_across_the_width_boundaries():
    # The one bit of the protocol easy to get wrong: the self-describing length.
    for n, width in [(0x7F, 1), (0x80, 2), (0x3FFF, 2), (0x4000, 3)]:
        assert len(_encode_length(n)) == width


@pytest.mark.asyncio
async def test_command_parses_re_rows_into_dicts():
    client = _client_over([
        ["!done"],                                   # login
        ["!re", "=name=WLPC-Gateway", "=channel-plan=us-915-1"],
        ["!done"],
    ])
    rows = await client.command("/lora/print", detail="")
    assert rows == [{"name": "WLPC-Gateway", "channel-plan": "us-915-1"}]
    # the wire carried the command and its =detail= attribute
    assert b"/lora/print" in client._writer.buf
    assert b"=detail=" in client._writer.buf
    await client.aclose()


@pytest.mark.asyncio
async def test_a_trap_becomes_a_router_error():
    from unifi_hamina_live.lora.api import RouterOSError
    client = _client_over([["!done"], ["!trap", "=message=no such command"],
                           ["!done"]])
    with pytest.raises(RouterOSError):
        await client.command("/lora/nope")
    await client.aclose()


@pytest.mark.asyncio
async def test_a_refused_login_is_an_auth_error():
    client = _client_over([["!trap", "=message=invalid user name or password"],
                           ["!done"]])
    with pytest.raises(RouterOSAuthError):
        await client.login()
    await client.aclose()


@pytest.mark.asyncio
async def test_an_absent_box_is_unreachable_not_auth():
    client = RouterOSClient("10.0.0.1", "admin", "admin", timeout=0.2)

    async def boom() -> None:
        raise RouterOSUnreachableError("no route to host")
    client._connect = boom  # type: ignore[method-assign]
    with pytest.raises(RouterOSUnreachableError):
        await client.login()
    await client.aclose()


# --- the source -----------------------------------------------------------

def _source_over(sentences: list[list[str]]) -> LoraSource:
    s = Settings(lora_enabled=True, lora_host="10.0.0.1",
                 lora_username="admin", lora_password="admin")
    src = LoraSource(s)
    src._client = _client_over(sentences)
    return src


@pytest.mark.asyncio
async def test_source_collects_one_ap_and_no_clients():
    src = _source_over([
        ["!done"],                                                  # login
        ["!re", *[f"={k}={v}" for k, v in LORA.items()]], ["!done"],  # /lora
        ["!re", *[f"={k}={v}" for k, v in ETH.items()]], ["!done"],   # ether
        ["!re", *[f"={k}={v}" for k, v in BOARD.items()]], ["!done"], # board
        ["!re", *[f"={k}={v}" for k, v in SERVERS[0].items()]],
        ["!re", *[f"={k}={v}" for k, v in SERVERS[1].items()]], ["!done"],
    ])
    aps, clients, placements = await src.collect("site1")
    assert len(aps) == 1 and aps[0].source == "lora"
    assert aps[0].mac == "2c:c8:1b:01:5f:a1"
    assert aps[0].model == "RBwAPR-2nD"
    assert clients == [], "a LoRaWAN gateway keeps no association table"
    assert src.status["online"] is True
    assert src.status["channel_plan"] == "us-915-1"
    assert "10.10.5.147" in (aps[0].radios[0].carrier_label or "")
    assert aps[0].mac in placements
    await src.aclose()


@pytest.mark.asyncio
async def test_an_unreachable_gateway_greys_out_the_last_known_ap():
    src = _source_over([
        ["!done"],
        ["!re", *[f"={k}={v}" for k, v in LORA.items()]], ["!done"],
        ["!re", *[f"={k}={v}" for k, v in ETH.items()]], ["!done"],
        ["!re", *[f"={k}={v}" for k, v in BOARD.items()]], ["!done"],
        ["!re", *[f"={k}={v}" for k, v in SERVERS[1].items()]], ["!done"],
    ])
    aps, _, _ = await src.collect("site1")
    assert aps[0].online is True
    known = aps[0].mac

    # Now the box stops answering. The AP should stay on the map, greyed out.
    async def boom() -> None:
        raise RouterOSUnreachableError("gone")
    src._client._reader = None
    src._client._writer = None
    src._client._connect = boom  # type: ignore[method-assign]

    aps2, clients2, placements2 = await src.collect("site1")
    assert len(aps2) == 1
    assert aps2[0].mac == known
    assert aps2[0].online is False
    assert aps2[0].radios == []
    assert clients2 == []
    assert src.error is not None
    await src.aclose()


@pytest.mark.asyncio
async def test_nothing_is_drawn_before_the_first_successful_poll():
    src = _source_over([])  # empty stream -> first read fails as unreachable
    src._client._reader = None
    src._client._writer = None

    async def boom() -> None:
        raise RouterOSUnreachableError("gone")
    src._client._connect = boom  # type: ignore[method-assign]

    aps, clients, placements = await src.collect("site1")
    assert aps == [] and clients == [] and placements == {}
    await src.aclose()


def test_spec_reads_placement_from_settings():
    s = Settings(lora_enabled=True, lora_host="10.0.0.1",
                 lora_anchor_ap="Lobby AP", lora_dx_px=30, lora_name="Gateway 1")
    spec = LoraSpec.from_settings(s)
    assert spec.name == "Gateway 1"
    assert spec.placement.anchor_ap == "Lobby AP"
    assert spec.placement.configured is True
