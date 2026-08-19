"""Carrier arithmetic, and the Wi-Fi costume a cell wears downstream.

Two jobs, kept apart on purpose:

1. **Real RF.** Turn an NR-ARFCN or E-UTRA EARFCN into a centre frequency in
   MHz, per 3GPP TS 38.104 §5.4.2.1 and TS 36.101 §5.7.3. This is arithmetic
   with a right answer, and it is tested against the published tables.

2. **The costume.** Choose the Wi-Fi band token and channel number the cell
   reports to anything that only understands Wi-Fi. This has no right answer —
   n48 is not a Wi-Fi channel and never will be — so the goal is only that it is
   *stable* (the same cell reports the same channel every poll, so nothing
   downstream sees a radio hopping) and *out of the way* (it defaults to
   channels a UniFi estate is least likely to be sitting on, so the fake radio
   does not read as interference on a real one).

Keeping them in one module is the point: the caller that wants the truth and the
caller that wants the costume take it from the same place, so they cannot drift.
"""

from __future__ import annotations

import hashlib

# --- NR-ARFCN -> MHz (TS 38.104 Table 5.4.2.1-1) --------------------------
# (N_REF-Offs, F_REF-Offs MHz, delta-F_Global MHz) for each N range.
_NR_RANGES = (
    (0, 0.0, 0.005),                 # 0 .. 2999.995 MHz
    (600000, 3000.0, 0.015),         # 3000 .. 24250 MHz
    (2016667, 24250.08, 0.060),      # 24250.08 MHz and up
)


def nr_arfcn_to_mhz(arfcn: int) -> float:
    """Centre frequency of an NR-ARFCN, in MHz.

    >>> round(nr_arfcn_to_mhz(636667), 3)      # n48 / CBRS
    3550.005
    >>> round(nr_arfcn_to_mhz(632628), 3)      # n78, a common 3.5 GHz SSB point
    3489.42
    """
    n_offs, f_offs, step = _NR_RANGES[0]
    for cand_n_offs, cand_f_offs, cand_step in _NR_RANGES:
        if arfcn >= cand_n_offs:
            n_offs, f_offs, step = cand_n_offs, cand_f_offs, cand_step
    return f_offs + step * (arfcn - n_offs)


# --- EARFCN -> MHz (TS 36.101 Table 5.7.3-1) ------------------------------
# band -> (F_DL_low MHz, N_Offs-DL). Downlink only: an EARFCN alone is
# ambiguous without its band, which is why cells.json asks for both.
#
# Not the whole table — the bands a private LTE / CBRS deployment actually uses,
# plus the common macro bands. Anything absent is not a silent wrong answer:
# `earfcn_to_mhz` returns None and `cells.json` asks you for `frequency_mhz`.
EUTRA_BANDS: dict[int, tuple[float, int]] = {
    1: (2110.0, 0), 2: (1930.0, 600), 3: (1805.0, 1200), 4: (2110.0, 1950),
    5: (869.0, 2400), 7: (2620.0, 2750), 8: (925.0, 3450), 12: (729.0, 5010),
    13: (746.0, 5180), 14: (758.0, 5280), 17: (734.0, 5730), 20: (791.0, 6150),
    25: (1930.0, 8040), 26: (859.0, 8690), 28: (758.0, 9210), 30: (2350.0, 9770),
    38: (2570.0, 37750), 40: (2300.0, 38650), 41: (2496.0, 39650),
    42: (3400.0, 41590), 43: (3600.0, 43590), 48: (3550.0, 55240),
    66: (2110.0, 66436), 71: (617.0, 68586),
}


def earfcn_to_mhz(earfcn: int, band: int) -> float | None:
    """Downlink centre frequency of an EARFCN in a band, or None if the band is
    not in :data:`EUTRA_BANDS`.

    >>> earfcn_to_mhz(55240, 48)               # bottom of CBRS
    3550.0
    >>> earfcn_to_mhz(56000, 48)
    3626.0
    """
    entry = EUTRA_BANDS.get(band)
    if entry is None:
        return None
    f_low, n_offs = entry
    return f_low + 0.1 * (earfcn - n_offs)


# --- the costume ----------------------------------------------------------
# Which Wi-Fi band token a real carrier is dressed as. Boundaries chosen by
# where the carrier actually sits, not by convenience: 3.5 GHz CBRS behaves far
# more like 5 GHz Wi-Fi than like 2.4, so that is what it wears.
def wifi_band_for(mhz: float) -> str:
    if mhz < 3000.0:
        return "2.4"
    if mhz < 5925.0:
        return "5"
    return "6"


# Channels the costume draws from, per band. The 5 GHz pool is UNII-2C/DFS on
# purpose: a UniFi estate on auto-channel rarely lands there, so a cell wearing
# one of these does not show up as a co-channel neighbour of a real AP in
# whatever is reading this. Override per cell with `wifi_channel` when you want
# it somewhere specific.
CHANNEL_POOL: dict[str, tuple[int, ...]] = {
    "2.4": (1, 6, 11),
    "5": (100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140),
    "6": (37, 53, 69, 85, 101, 117, 133, 149, 165, 181, 197, 213),
}


def costume_channel(band: str, key: str) -> int:
    """A stable Wi-Fi channel for a cell, drawn from that band's pool.

    ``key`` is any stable identity for the cell (its synthetic MAC). Same key,
    same channel, for the life of the deployment — a channel that moved between
    polls would read downstream as a radio retuning itself.
    """
    pool = CHANNEL_POOL.get(band) or CHANNEL_POOL["5"]
    digest = hashlib.sha1(key.encode()).digest()
    return pool[int.from_bytes(digest[:4], "big") % len(pool)]


# Cellular bandwidths are not Wi-Fi widths (10, 15, 50, 100 MHz have no Wi-Fi
# equivalent), so round down to a legal one and keep the real figure on the
# radio beside it.
_WIFI_WIDTHS = (20, 40, 80, 160, 320)


def costume_width(bandwidth_mhz: float | None) -> int | None:
    """The largest legal Wi-Fi channel width that does not overstate the
    carrier. A 10 MHz LTE carrier is reported as 20 (the narrowest Wi-Fi has),
    a 100 MHz NR carrier as 80, not 160."""
    if not bandwidth_mhz or bandwidth_mhz <= 0:
        return None
    fits = [w for w in _WIFI_WIDTHS if w <= bandwidth_mhz]
    return fits[-1] if fits else _WIFI_WIDTHS[0]
