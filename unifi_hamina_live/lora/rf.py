"""The Wi-Fi costume a LoRaWAN gateway wears downstream, and the honest label
that rides beside it.

A LoRa gateway is not a Wi-Fi radio at all — it is a sub-GHz LoRaWAN packet
forwarder, listening across a *band plan* of eight-or-so narrow channels
(125/250/500 kHz) somewhere in 863-928 MHz depending on region. None of that
fits the 2.4/5/6 GHz band axis Hamina draws, so, exactly as in the cellular and
HaLow packages, the band and channel reported downstream are a **costume**:

* **Stable** — the same gateway reports the same channel every poll (keyed on
  its gateway EUI), so nothing downstream sees a radio hopping.
* **Out of the way** — drawn from 5 GHz DFS channels a UniFi estate on
  auto-channel rarely lands on, so the gateway does not read as a co-channel
  neighbour of a real AP.

``carrier_mhz`` is deliberately ``None``: a LoRaWAN gateway does not sit on one
centre frequency — it spreads across a whole sub-band — so there is no single
carrier to report, and inventing one would be the plausible-but-wrong number
this project keeps refusing to draw. What is real — that it is a LoRaWAN
gateway, its region/channel-plan and the band that plan occupies, its EUI and
the network server it forwards to — rides on the human ``carrier_label``.
"""

from __future__ import annotations

import hashlib

# LoRa occupies sub-GHz spectrum; nothing it does belongs on 2.4 GHz, where a
# real UniFi estate lives densely. The costume defaults to 5 GHz DFS (UNII-2C),
# the same "least likely to collide with a live AP" reasoning the cellular and
# HaLow costumes use.
COSTUME_BAND = "5"
CHANNEL_POOL: tuple[int, ...] = (
    100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140,
)

# LoRa channel widths (125/250/500 kHz) are far narrower than any Wi-Fi width.
# Reported as 20 (the narrowest Wi-Fi has) so the costume never *overstates* the
# carrier.
_MIN_WIFI_WIDTH = 20

# RouterOS channel-plan names -> the sub-GHz band that plan actually occupies.
# Matched by prefix, longest first, so "us-915-1" resolves through "us-915".
_BAND_RANGES: tuple[tuple[str, str], ...] = (
    ("eu-868", "863-870 MHz (EU868)"),
    ("eu-433", "433 MHz (EU433)"),
    ("us-915", "902-928 MHz (US915)"),
    ("au-915", "915-928 MHz (AU915)"),
    ("as-923", "915-928 MHz (AS923)"),
    ("kr-920", "920-923 MHz (KR920)"),
    ("in-865", "865-867 MHz (IN865)"),
    ("ru-864", "864-870 MHz (RU864)"),
    ("cn-470", "470-510 MHz (CN470)"),
)


def costume_channel(gateway_id: str) -> int:
    """A stable 5 GHz channel for a LoRa gateway, drawn from the DFS pool.

    Keyed on the gateway EUI, which is real and does not change, so the gateway
    keeps its costume channel for the life of the deployment — a channel that
    moved between polls would read downstream as a radio retuning itself.
    """
    digest = hashlib.sha1(gateway_id.strip().lower().encode()).digest()
    return CHANNEL_POOL[int.from_bytes(digest[:4], "big") % len(CHANNEL_POOL)]


def costume_width_mhz() -> int:
    """The narrowest legal Wi-Fi width. LoRa's real width is 125-500 kHz, all
    well below 20 MHz, so this is always the 20 MHz floor."""
    return _MIN_WIFI_WIDTH


def band_for_plan(channel_plan: str | None) -> str | None:
    """The sub-GHz band a RouterOS channel-plan occupies, human-readable."""
    if not channel_plan:
        return None
    key = channel_plan.strip().lower()
    for prefix, label in _BAND_RANGES:
        if key.startswith(prefix):
            return label
    return None


def carrier_label(channel_plan: str | None, gateway_id: str | None,
                  server: str | None, status: str | None) -> str:
    """A one-line summary of what a ``source="lora"`` access point really is —
    a LoRaWAN gateway, the band its channel-plan occupies, its EUI, and the
    network server it forwards to."""
    parts = ["LoRaWAN gateway (sub-GHz)"]
    detail: list[str] = []
    band = band_for_plan(channel_plan)
    if band:
        detail.append(band)
    if channel_plan:
        detail.append(f"plan {channel_plan}")
    if gateway_id:
        detail.append(f"EUI {gateway_id}")
    if server:
        detail.append(f"→ {server}")
    if status:
        detail.append(status)
    return parts[0] + (" (" + ", ".join(detail) + ")" if detail else "")
