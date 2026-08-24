"""RouterOS ``/lora`` records -> the neutral :mod:`..models` an access point
speaks.

Pure functions over the dicts the RouterOS API returns, so the whole projection
is testable without a gateway on the network. A LoRaWAN gateway is a single
point on the map: it is a packet forwarder, not an association point, so unlike
the HaLow side there are no stations to enumerate — the gateway's own end
devices report through it but it keeps no list of them with per-device signal.
This package therefore emits exactly one access point and no clients.

Identity is the box's own ethernet MAC — a real, stable 48-bit address — rather
than the 64-bit gateway EUI, which is not a MAC. The EUI still keys the costume
channel and rides on the honest label.
"""

from __future__ import annotations

from ..models import AccessPoint, Radio
from ..unifi.normalize import normalize_mac, synth_serial
from . import rf

# Where a LoRa AP comes from, marked on every access point this package
# produces — the one field that tells it apart from a real UniFi radio, a
# costumed cell, or a HaLow AP. Kept as a constant so downstream filters name
# it once.
SOURCE = "lora"

DEFAULT_MODEL = "wAP LR8"


def _running(lora: dict) -> bool:
    """Whether the gateway is administratively up. RouterOS reports both a
    ``disabled`` flag and a ``status`` string; a box that is reachable but with
    the LoRa interface disabled is not transmitting, so it is not "online"."""
    disabled = (lora.get("disabled") or "false").strip().lower() == "true"
    status = (lora.get("status") or "").strip().lower()
    return not disabled and status not in ("disabled", "down")


def radio_for(lora: dict, gateway_id: str, server: str | None) -> Radio:
    """The single radio a LoRa gateway reports, wearing its 5 GHz costume.

    The band and channel reported downstream are the stable, out-of-the-way
    costume (see :mod:`.rf`). ``carrier_mhz`` is deliberately ``None`` — a
    LoRaWAN gateway spreads across a sub-band, not one centre frequency — and
    what it really is rides on ``carrier_label``. TX power is not reported by
    ``/lora`` (per-channel and set by the band plan), so it is left ``None``.
    """
    antenna_gain = lora.get("antenna-gain")
    return Radio(
        band=rf.COSTUME_BAND,
        channel=rf.costume_channel(gateway_id),
        channel_width_mhz=rf.costume_width_mhz(),
        tx_power_dbm=None,
        num_clients=0,
        technology=SOURCE,
        carrier_mhz=None,
        carrier_label=rf.carrier_label(
            lora.get("channel-plan"), gateway_id, server, lora.get("status")),
    )


def access_point(lora: dict, mac: str, site_id: str, *,
                 name: str | None = None, model: str | None = None,
                 server: str | None = None) -> AccessPoint:
    """One LoRa gateway, as the access point every downstream surface
    understands.

    ``lora`` is a ``/lora/print detail`` row; ``mac`` is the box's ethernet MAC,
    used as the stable identity. The display name defaults to the LoRa
    interface's own name, and the model to the RouterBOARD model when known.
    """
    gateway_id = (lora.get("gateway-id") or lora.get("hardware-id")
                  or "").strip()
    address = normalize_mac(mac)
    display = name or lora.get("name") or "LoRa Gateway"
    running = _running(lora)
    return AccessPoint(
        site_id=site_id,
        name=display,
        mac=address,
        serial=synth_serial(address),
        model_code=model or DEFAULT_MODEL,
        model=model or DEFAULT_MODEL,
        state="online" if running else "offline",
        online=running,
        num_clients=0,
        radios=[radio_for(lora, gateway_id, server)] if running else [],
        source=SOURCE,
    )


def offline_ap(mac: str, site_id: str, *, name: str | None = None,
               model: str | None = None) -> AccessPoint:
    """A LoRa gateway that is configured but not answering.

    A gateway that lost power or went off the network stays on the map greyed
    out, the same way a cell the core stopped reporting or a HaLow radio that
    went dark does, rather than vanishing — a gap on a coverage map reads as
    "no coverage here", which is a different and wronger thing than "this
    gateway is down".
    """
    address = normalize_mac(mac)
    return AccessPoint(
        site_id=site_id,
        name=name or "LoRa Gateway",
        mac=address,
        serial=synth_serial(address),
        model_code=model or DEFAULT_MODEL,
        model=model or DEFAULT_MODEL,
        state="offline",
        online=False,
        num_clients=0,
        radios=[],
        source=SOURCE,
    )
