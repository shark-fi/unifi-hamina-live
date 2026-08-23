"""``iwinfo`` records off a HaLow radio -> the neutral :mod:`..models` an access
point and its clients speak.

Pure functions over the dicts ``ubus`` returns, so the whole projection is
testable without a radio on the network. A HaLow AP carries a real BSSID and its
stations carry real MACs, so there is far less to synthesize than on the
cellular side — the identities here are the hardware's own.
"""

from __future__ import annotations

from ..models import AccessPoint, Client, Radio
from ..unifi.normalize import normalize_mac, synth_serial
from . import rf

# Where a HaLow AP comes from, marked on every access point this package
# produces — the one field that tells it apart from a real UniFi radio or a
# costumed cell. Kept as a constant so downstream filters name it once.
SOURCE = "halow"

DEFAULT_MODEL = "AHM27292U"


def radio_for(info: dict, bssid: str, num_clients: int) -> Radio:
    """The single radio a HaLow AP reports, wearing its 5 GHz costume.

    TX power and the mapped channel/frequency are read straight off the radio;
    the band and channel reported downstream are the stable, out-of-the-way
    costume (see :mod:`.rf`). The true carrier — that it is 802.11ah, its
    country, and the driver's mapped channel — rides along on ``carrier_label``.
    ``carrier_mhz`` is deliberately ``None``: the permitted ``ubus`` surface
    does not expose the real S1G centre, and the mapped 2.4 GHz figure is not a
    measurement of it.
    """
    mapped_channel = _int(info.get("channel"))
    mapped_mhz = _int(info.get("frequency"))
    htmode = info.get("htmode")
    return Radio(
        band=rf.COSTUME_BAND,
        channel=rf.costume_channel(bssid),
        channel_width_mhz=rf.costume_width_mhz(htmode),
        tx_power_dbm=_float(info.get("txpower")),
        num_clients=num_clients,
        technology=SOURCE,
        carrier_mhz=None,
        carrier_label=rf.carrier_label(
            info.get("country"), mapped_channel, mapped_mhz, htmode),
    )


def access_point(info: dict, site_id: str, stations: list[dict],
                 *, name: str | None = None, model: str | None = None,
                 ) -> AccessPoint:
    """One HaLow AP, as the access point every downstream surface understands.

    ``info`` is an ``iwinfo info`` reply; ``stations`` is the ``iwinfo
    assoclist`` results. The BSSID is the radio's own, so the MAC and the
    serial derived from it are stable without any of the remembering the
    cellular side has to do.
    """
    bssid = normalize_mac(info.get("bssid") or "")
    ssid = info.get("ssid") or ""
    display = name or ssid or "HaLow AP"
    num_clients = len(stations)
    return AccessPoint(
        site_id=site_id,
        name=display,
        mac=bssid,
        serial=synth_serial(bssid),
        model_code=model or DEFAULT_MODEL,
        model=model or DEFAULT_MODEL,
        state="online",
        online=True,
        num_clients=num_clients,
        radios=[radio_for(info, bssid, num_clients)],
        source=SOURCE,
    )


def offline_ap(bssid: str, site_id: str, *, name: str | None = None,
               model: str | None = None) -> AccessPoint:
    """A HaLow AP that is configured but not answering.

    A radio that lost power or went off the network stays on the map greyed
    out, the same way a cell the core stopped reporting does, rather than
    vanishing — a gap on a coverage map reads as "no coverage here", which is a
    different and wronger thing than "this AP is down".
    """
    mac = normalize_mac(bssid)
    return AccessPoint(
        site_id=site_id,
        name=name or "HaLow AP",
        mac=mac,
        serial=synth_serial(mac),
        model_code=model or DEFAULT_MODEL,
        model=model or DEFAULT_MODEL,
        state="offline",
        online=False,
        num_clients=0,
        radios=[],
        source=SOURCE,
    )


def client(sta: dict, ap: AccessPoint, site_id: str, essid: str,
           *, ip: str | None = None) -> Client:
    """One associated HaLow station, as a wireless client.

    ``signal`` off the radio is a real dBm and lands on both ``rssi`` and
    ``signal_dbm``; the rates ``iwinfo`` reports are already in kbit/s and pass
    straight through. Anything the radio did not report stays ``None`` rather
    than becoming a zero — a 0 dBm RSSI would draw as a client sitting on top of
    the AP.
    """
    mac = normalize_mac(sta.get("mac") or "")
    radio = ap.radios[0] if ap.radios else None
    return Client(
        mac=mac,
        ip=ip,
        site_id=site_id,
        ap_mac=ap.mac,
        ap_serial=ap.serial,
        essid=essid or None,
        band=radio.band if radio else None,
        channel=radio.channel if radio else None,
        rssi=_int(sta.get("signal")),
        signal_dbm=_int(sta.get("signal")),
        noise_dbm=_int(sta.get("noise")),
        tx_rate_kbps=_rate(sta.get("tx")),
        rx_rate_kbps=_rate(sta.get("rx")),
        tx_bytes=_int(sta.get("tx_bytes")),
        rx_bytes=_int(sta.get("rx_bytes")),
        uptime_seconds=None,
    )


def _rate(direction) -> int | None:
    """The kbit/s rate out of an ``iwinfo`` ``rx``/``tx`` sub-object."""
    if isinstance(direction, dict):
        return _int(direction.get("rate"))
    return None


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
