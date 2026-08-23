"""The Wi-Fi costume an 802.11ah HaLow AP wears downstream, and the honest
label that rides beside it.

HaLow is sub-GHz 802.11 — 902-928 MHz in the US, with 1/2/4/8 MHz channels.
None of that fits the 2.4/5/6 GHz band axis Hamina draws, so, exactly as in the
cellular package, the band and channel reported downstream are a **costume**:

* **Stable** — the same radio reports the same channel every poll (keyed on its
  BSSID), so nothing downstream sees a radio hopping.
* **Out of the way** — drawn from 5 GHz DFS channels a UniFi estate on
  auto-channel rarely lands on, so a HaLow AP does not read as a co-channel
  neighbour of a real one.

What the driver's ``iwinfo`` reports (a legacy 2.4 GHz channel/frequency the
Morse Micro S1G mapping produces) is preserved verbatim in the human label, not
promoted to a real centre frequency — that number is where the *mapping* sits,
not where the radio transmits.
"""

from __future__ import annotations

import hashlib

# HaLow occupies sub-GHz spectrum; nothing it does belongs on 2.4 GHz, where a
# real UniFi estate lives densely. The costume defaults to 5 GHz DFS (UNII-2C),
# the same "least likely to collide with a live AP" reasoning the cellular
# costume uses.
COSTUME_BAND = "5"
CHANNEL_POOL: tuple[int, ...] = (
    100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140,
)

# HaLow channel widths (1/2/4/8 MHz) are narrower than any Wi-Fi width. Reported
# as 20 (the narrowest Wi-Fi has) so the costume never *overstates* the carrier,
# with the true width kept in the label.
_MIN_WIFI_WIDTH = 20


def costume_channel(bssid: str) -> int:
    """A stable 5 GHz channel for a HaLow AP, drawn from the DFS pool.

    Keyed on the BSSID, which is real and does not change, so the AP keeps its
    costume channel for the life of the deployment — a channel that moved
    between polls would read downstream as a radio retuning itself.
    """
    digest = hashlib.sha1(bssid.strip().lower().encode()).digest()
    return CHANNEL_POOL[int.from_bytes(digest[:4], "big") % len(CHANNEL_POOL)]


def costume_width_mhz(htmode: str | None) -> int:
    """The narrowest legal Wi-Fi width. HaLow's real width is 1-8 MHz, all of
    which are below 20, so this is always 20 — kept as a function so the label
    can still report the true S1G width the driver named."""
    return _MIN_WIFI_WIDTH


def s1g_width_mhz(htmode: str | None) -> int | None:
    """The real S1G channel width the driver's ``htmode`` implies, in MHz.

    Morse Micro maps each S1G bandwidth onto a legacy HT/VHT mode string, so the
    ``htmode`` iwinfo reports is a stand-in: ``HT20`` for the 8 MHz operating
    channel's primary, and so on. Only used to enrich the honest label; it never
    drives the costume, which is always the 20 MHz floor.
    """
    if not htmode:
        return None
    mapping = {"HT20": 8, "HT40": 8, "VHT80": 8, "VHT40": 4, "VHT20": 2}
    return mapping.get(htmode.upper())


def carrier_label(country: str | None, mapped_channel: int | None,
                  mapped_mhz: int | None, htmode: str | None) -> str:
    """A one-line summary of the real carrier — 802.11ah, its regulatory
    domain, and the driver's mapped channel — for the reader who wants to know
    what a ``source="halow"`` access point actually is.

    Deliberately says "mapped", because that legacy channel/frequency is the
    Morse Micro S1G-to-mac80211 mapping, not a measured centre frequency.
    """
    parts = ["802.11ah HaLow (S1G, sub-GHz)"]
    if country:
        parts.append(country)
    width = s1g_width_mhz(htmode)
    if width is not None:
        parts.append(f"{width} MHz")
    if mapped_channel is not None and mapped_mhz is not None:
        parts.append(f"driver maps to ch{mapped_channel}/{mapped_mhz} MHz")
    return " — ".join(parts[:1]) + (
        " (" + ", ".join(parts[1:]) + ")" if len(parts) > 1 else "")
