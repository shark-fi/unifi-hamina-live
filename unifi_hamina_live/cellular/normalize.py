"""Cell and UE records -> the neutral :mod:`..models` an access point speaks.

Pure functions over dicts and specs, so the whole projection — including the
costume — is testable without a core, a console or a network.
"""

from __future__ import annotations

import hashlib

from ..models import AccessPoint, Client, Radio
from ..unifi.normalize import synth_serial
from . import rf
from .cells import CellSpec, PlacementSpec, RadioSpec

# Locally administered MAC prefixes: the second-least-significant bit of the
# first octet is set, so these can never collide with a real vendor OUI — no
# cell or UE here can be mistaken for hardware someone actually shipped. The
# second octet separates the two at a glance: 5c for a cell, 5d for subscriber
# equipment.
CELL_MAC_PREFIX = "02:5c"
UE_MAC_PREFIX = "02:5d"

DEFAULT_MODELS = {"nr": "NR-CELL", "lte": "LTE-CELL"}

# How far an anchored cell sits from its anchor, in floor-plan pixels, when the
# placement does not say. Enough to read as two radios rather than one; small
# enough that the cell is still plainly "at" the anchor. Set dx_px/dy_px to 0
# only if you genuinely want them coincident.
DEFAULT_ANCHOR_OFFSET_PX = 24.0


def cell_key(cell: dict) -> str:
    """The identity a cell keeps across polls, and that a UE joins to.

    A gNB/eNB id is unique within a PLMN, which is as far as this needs to go:
    one radio unit is one record here even when it announces several cells, so
    the join is on the RAN node, not the NR-CGI. A UE reports both, and the node
    id is the half that both 4G and 5G agree on.
    """
    tech = cell.get("technology") or "nr"
    node = cell.get("gnb_id") if tech == "nr" else cell.get("enb_id")
    return "%s:%s:%s" % (tech, cell.get("plmn") or "", node)


def ue_cell_key(ue: dict) -> str:
    tech = ue.get("technology") or "nr"
    node = ue.get("gnb_id") if tech == "nr" else ue.get("enb_id")
    return "%s:%s:%s" % (tech, ue.get("plmn") or "", node)


def _mac(prefix: str, key: str) -> str:
    digest = hashlib.sha1(key.encode()).digest()
    tail = ":".join("%02x" % b for b in digest[:4])
    return "%s:%s" % (prefix, tail)


def cell_mac(cell: dict) -> str:
    return _mac(CELL_MAC_PREFIX, cell_key(cell))


def ue_mac(supi: str) -> str:
    return _mac(UE_MAC_PREFIX, supi.strip().lower())


# --- the radio ------------------------------------------------------------

def carrier_mhz(spec: RadioSpec) -> float | None:
    """The real centre frequency, from whichever of the declared fields can
    give one. An explicit ``frequency_mhz`` wins — it is a measurement, where
    an ARFCN is an index that needs a table we may not have."""
    if spec.frequency_mhz:
        return float(spec.frequency_mhz)
    if spec.arfcn is None:
        return None
    if spec.is_nr:
        return rf.nr_arfcn_to_mhz(spec.arfcn)
    if spec.band_number is None:
        return None
    return rf.earfcn_to_mhz(spec.arfcn, spec.band_number)


def carrier_label(spec: RadioSpec, mhz: float | None) -> str | None:
    """One line describing the real carrier, for anything that displays it."""
    bits = [spec.technology.upper()]
    if spec.band:
        bits.append(spec.band)
    if spec.arfcn is not None:
        bits.append("%s %d" % ("ARFCN" if spec.is_nr else "EARFCN", spec.arfcn))
    detail = []
    if mhz is not None:
        detail.append("%.1f MHz" % mhz)
    if spec.bandwidth_mhz:
        detail.append("%g MHz wide" % spec.bandwidth_mhz)
    line = " ".join(bits)
    if detail:
        line += " (%s)" % ", ".join(detail)
    return line or None


def radio_for(spec: RadioSpec, mac: str, num_clients: int) -> Radio | None:
    """The single radio a cell reports, wearing its Wi-Fi costume.

    Returns None when the cell has no declared carrier at all — a radio with an
    invented band *and* an invented frequency would be pure fiction, and an
    access point with no radios is a truthful way to say "this cell is up, its
    RF was never declared".
    """
    mhz = carrier_mhz(spec)
    if mhz is None and not spec.band:
        return None
    band = rf.wifi_band_for(mhz) if mhz is not None else "5"
    channel = spec.wifi_channel or rf.costume_channel(band, mac)
    return Radio(
        band=band,
        channel=channel,
        channel_width_mhz=rf.costume_width(spec.bandwidth_mhz),
        tx_power_dbm=spec.tx_power_dbm,
        num_clients=num_clients,
        technology=spec.technology,
        carrier_mhz=round(mhz, 3) if mhz is not None else None,
        carrier_label=carrier_label(spec, mhz),
    )


# --- the access point -----------------------------------------------------

def _peer_ip(peer: str) -> str | None:
    """``[192.168.168.100]:60110`` -> ``192.168.168.100``. The RAN's own
    address, which is genuinely useful on a map when a cell misbehaves."""
    if not peer:
        return None
    value = peer.strip()
    if value.startswith("["):
        inner, _, _ = value[1:].partition("]")
        return inner or None
    host, _, _ = value.rpartition(":")
    return host or value or None


def default_name(cell: dict) -> str:
    tech = cell.get("technology") or "nr"
    node = cell.get("gnb_id") if tech == "nr" else cell.get("enb_id")
    label = "gNB" if tech == "nr" else "eNB"
    return "%s %s" % (label, node if node is not None else "?")


def access_point(cell: dict, spec: CellSpec | None, site_id: str,
                 num_clients: int | None = None) -> AccessPoint:
    """One cell, as the access point every downstream surface understands."""
    mac = cell_mac(cell)
    tech = cell.get("technology") or "nr"
    spec_name = (spec.name if spec and spec.name else "")
    name = spec_name or cell.get("name") or default_name(cell)
    model = (spec.model if spec and spec.model else DEFAULT_MODELS.get(tech, "CELL"))
    clients = cell.get("num_ues", 0) if num_clients is None else num_clients
    radio = radio_for(spec.radio, mac, clients) if spec else None
    online = bool(cell.get("connected", True))
    return AccessPoint(
        site_id=site_id,
        name=name,
        mac=mac,
        serial=synth_serial(mac),
        model_code=model,
        model=model,
        ip=_peer_ip(cell.get("peer") or ""),
        state="online" if online else "offline",
        online=online,
        num_clients=clients,
        radios=[radio] if radio else [],
        source="cellular",
    )


def offline_cell(spec: CellSpec, site_id: str) -> AccessPoint:
    """A cell that is declared but that the core is not reporting.

    A small cell whose NG/S1 link dropped should go grey on the map where it
    stands, not disappear from it — the same thing a UniFi AP does when it loses
    power. A cell that vanishes reads as "no such site"; one that goes offline
    reads as "go and look at that one".
    """
    tech = spec.radio.technology
    cell = {
        "technology": tech,
        "gnb_id": spec.match.get("gnb_id"),
        "enb_id": spec.match.get("enb_id"),
        "plmn": str(spec.match.get("plmn") or ""),
        "connected": False,
        "num_ues": 0,
        "peer": "",
    }
    return access_point(cell, spec, site_id, num_clients=0)


# --- the clients ----------------------------------------------------------

def mask_supi(supi: str) -> str:
    """``001010000056492`` -> ``00101…6492``.

    A SUPI is an IMSI, which identifies a person's SIM rather than a device, and
    this ends up on a dashboard that gets screen-shared in demos. The leading
    digits are the PLMN (which is not secret and is the useful half for telling
    networks apart) and the last four are enough to pick a test SIM out of a
    handful; the middle is the part worth not projecting onto a wall.
    """
    digits = supi.strip()
    if len(digits) <= 9:
        return digits
    return "%s…%s" % (digits[:5], digits[-4:])


def client(ue: dict, cell_ap: AccessPoint, site_id: str, essid: str,
           session: dict | None = None, mask: bool = True) -> Client:
    """One UE, as a wireless client of the cell it is camped on.

    Radio measurements are left empty on purpose. A Wi-Fi controller reports a
    client's RSSI because the AP measured it; a core never sees the air, so any
    signal figure here would be invented, and an invented RSSI is the one thing
    that would make a Hamina heatmap actively wrong rather than merely
    costumed.
    """
    session = session or {}
    supi = ue.get("supi") or ""
    bare = supi.split("-")[-1]
    radio = cell_ap.radios[0] if cell_ap.radios else None
    return Client(
        mac=ue_mac(bare),
        hostname=mask_supi(bare) if mask else bare,
        name=None,
        ip=session.get("ip") or None,
        site_id=site_id,
        ap_mac=cell_ap.mac,
        ap_serial=cell_ap.serial,
        essid=essid or None,
        band=radio.band if radio else None,
        channel=radio.channel if radio else None,
        is_guest=False,
    )


# --- placement ------------------------------------------------------------

def place(ap: AccessPoint, placement: PlacementSpec,
          anchors: dict[str, AccessPoint]) -> str | None:
    """Pin a cell to a floor plan. Returns why it could not be, or None.

    The anchor form is the one that makes the UniFi console the only place
    anything is positioned: name a device that is already on the console's own
    map and the cell rides on its coordinates every poll, so dragging the anchor
    moves the cell with no file to edit and nothing to re-import. That is worth
    the indirection because the alternative — pixels in a config file — goes
    stale the moment someone rescales the plan.

    ``anchors`` is keyed by casefolded AP name: people type the name from the
    console UI, and matching it case-sensitively fails in a way that looks like
    the cell is simply missing.
    """
    if placement.anchor_ap:
        anchor = anchors.get(placement.anchor_ap.strip().casefold())
        if anchor is None:
            return ("anchor AP %r is not on this console, so %s has no position"
                    % (placement.anchor_ap, ap.name))
        if anchor.floorplan_id is None:
            return ("anchor AP %r is not placed on a floor plan, so %s has no "
                    "position — drag it onto the plan in UniFi"
                    % (placement.anchor_ap, ap.name))
        ap.floorplan_id = anchor.floorplan_id
        # Offset by default, not only when asked: a cell drawn at exactly its
        # anchor's pixel is hidden underneath it, which on a map is
        # indistinguishable from the cell never having imported.
        dx = placement.dx_px or DEFAULT_ANCHOR_OFFSET_PX
        dy = placement.dy_px or DEFAULT_ANCHOR_OFFSET_PX
        ap.x = None if anchor.x is None else anchor.x + dx
        ap.y = None if anchor.y is None else anchor.y + dy
        return None
    if placement.floorplan and placement.x_px is not None:
        ap.floorplan_id = placement.floorplan
        ap.x = placement.x_px
        ap.y = placement.y_px
        return None
    return "%s has no placement configured, so it cannot be drawn on a plan" % ap.name
