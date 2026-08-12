"""RSSI multilateration: where a transmitter is, from how loudly several
sensors hear it.

Ported from shark-fi/wlanpi-rssi-locate, which ran this as its own service. It
lives here because the bridge already knows what that tool could not: a floor
plan's ``meters_per_px``, its pixel dimensions, and where every adopted AP sits
in that pixel space. Standalone, the solver emitted metres in a frame defined by
a hand-written ``area`` with no relationship to anyone's floor plan, and putting
a result on a map meant deriving a transform from correspondences. Expressed in
the plan's own space, the answer lands in the same coordinates as
``/api/access-points`` and every existing consumer already understands it.

This module is the solver and the sample store only -- no HTTP, no config
loading, no sensor protocol. Those arrive with the ingest endpoint.
"""

from .solve import multilaterate, pathloss_distance, px_to_m, m_to_px
from .store import Anchor, PathLoss, Sensor, Store, Target

__all__ = [
    "Anchor", "PathLoss", "Sensor", "Store", "Target",
    "multilaterate", "pathloss_distance", "px_to_m", "m_to_px",
]
