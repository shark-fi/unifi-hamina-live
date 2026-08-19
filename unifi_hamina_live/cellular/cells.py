"""The cell inventory — what the core cannot tell you, declared in ``cells.json``.

Open5GS is a core. It knows a gNB by the id that gNB announced over NG, which
UEs are on it and what TAC it serves; it has never seen the radio. Band, ARFCN,
bandwidth, TX power and where the thing is bolted to a wall are RAN and site
facts, so they are configuration here rather than telemetry — and this module is
the only place that reads them, so nothing else has to guess.

A spec matches a cell the core reported, by any subset of gNB/eNB id, cell id,
PLMN and TAC. A spec with no ``match`` is a fallback for cells nothing else
claimed, which is what a single-cell proof of concept wants: declare the radio
once and it applies whatever id the gNB turns up with. A spec whose ``match``
names a specific id but which the core is not reporting yields an **offline**
cell rather than nothing at all, the same way a UniFi AP that lost power stays
on the map greyed out instead of vanishing from it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

log = logging.getLogger("unifi_hamina_live.cellular")

_TECHNOLOGIES = ("nr", "lte")


@dataclass(frozen=True)
class RadioSpec:
    """The carrier a cell transmits, as declared. All optional: a cell with no
    radio still reports identity and its attached UEs, it just cannot be drawn
    with a channel."""

    technology: str = "nr"
    band: str = ""                      # "n48", "48", "n78" — as written
    band_number: int | None = None      # the numeric part, for the EARFCN table
    arfcn: int | None = None            # NR-ARFCN or EARFCN, per technology
    frequency_mhz: float | None = None  # wins over arfcn when both are given
    bandwidth_mhz: float | None = None
    tx_power_dbm: float | None = None
    wifi_channel: int | None = None     # pin the costume channel; see rf.py

    @property
    def is_nr(self) -> bool:
        return self.technology == "nr"


@dataclass(frozen=True)
class PlacementSpec:
    """Where the cell sits on a floor plan.

    Two ways in, and ``anchor_ap`` is the one to reach for: name a UniFi device
    that is already placed on the console's own map and the cell inherits its
    position every poll. Dragging the anchor in UniFi moves the cell, with no
    file to edit and no re-import anywhere — the console stays the single place
    anything is positioned.

    The explicit form (a plan id plus pixels) is for a cell with no UniFi device
    anywhere near it. Pixels rather than metres because that is what you can
    read off a floor plan, and /sensors will let you click them out.

    ``dx_px``/``dy_px`` nudge an anchored cell off its anchor. Without them the
    two land on identical coordinates and draw as one icon on top of another —
    which looks, on a map, exactly like the cell failed to import.
    """

    anchor_ap: str = ""
    dx_px: float = 0.0
    dy_px: float = 0.0
    floorplan: str = ""
    x_px: float | None = None
    y_px: float | None = None

    @property
    def configured(self) -> bool:
        return bool(self.anchor_ap) or bool(
            self.floorplan and self.x_px is not None and self.y_px is not None
        )


@dataclass(frozen=True)
class CellSpec:
    id: str
    name: str
    model: str
    match: dict = field(default_factory=dict)
    radio: RadioSpec = field(default_factory=RadioSpec)
    placement: PlacementSpec = field(default_factory=PlacementSpec)
    network_name: str = ""

    @property
    def is_fallback(self) -> bool:
        return not self.match

    def matches(self, cell: dict) -> bool:
        """Does this spec describe the cell the core reported?

        Every key present in ``match`` must agree. Ids are compared as ints
        where both sides look numeric so ``100`` and ``"100"`` are the same gNB;
        PLMN and TAC compare as strings because a TAC is written both ways
        ("000001" and 1) and the hex form is the one the core prints.
        """
        for key, want in self.match.items():
            got = cell.get(key)
            if got is None:
                return False
            if not _same(want, got):
                return False
        return True


def _same(want, got) -> bool:
    try:
        return int(str(want), 0) == int(str(got), 0)
    except (TypeError, ValueError):
        return str(want).strip().lower() == str(got).strip().lower()


@dataclass(frozen=True)
class CellInventory:
    site_id: str
    network_name: str
    specs: tuple[CellSpec, ...]

    @classmethod
    def empty(cls) -> "CellInventory":
        return cls(site_id="", network_name="", specs=())

    def spec_for(self, cell: dict) -> CellSpec | None:
        """The best spec for a reported cell: the first explicit match wins,
        then the first fallback. Explicit before fallback regardless of file
        order, so adding a fallback for the rest of an estate cannot quietly
        take over a cell you had already described precisely."""
        for spec in self.specs:
            if not spec.is_fallback and spec.matches(cell):
                return spec
        for spec in self.specs:
            if spec.is_fallback:
                return spec
        return None

    @property
    def declared_cells(self) -> tuple[CellSpec, ...]:
        """Specs that name a specific cell — the ones we can show as offline
        when the core is not reporting them."""
        return tuple(s for s in self.specs if not s.is_fallback)

    @classmethod
    def load(cls, path: str) -> "CellInventory":
        with open(path) as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            raise ValueError("cell inventory must be a JSON object")
        site_id = str(raw.get("site_id") or "").strip()
        network_name = str(raw.get("network_name") or "").strip()
        entries = raw.get("cells")
        if entries is None:
            raise ValueError("cell inventory needs a 'cells' array")
        if not isinstance(entries, list):
            raise ValueError("'cells' must be an array")
        specs = [_spec(e, i, network_name) for i, e in enumerate(entries)]
        ids = [s.id for s in specs]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError("duplicate cell id(s) in %s: %s"
                             % (path, ", ".join(sorted(dupes))))
        return cls(site_id=site_id, network_name=network_name,
                   specs=tuple(specs))


def _spec(entry, index: int, default_network: str) -> CellSpec:
    if not isinstance(entry, dict):
        raise ValueError("cells[%d] must be an object" % index)
    match = entry.get("match") or {}
    if not isinstance(match, dict):
        raise ValueError("cells[%d].match must be an object" % index)
    ident = str(entry.get("id") or "").strip() or _derive_id(match, index)
    name = str(entry.get("name") or "").strip() or ident
    return CellSpec(
        id=ident,
        name=name,
        model=str(entry.get("model") or "").strip(),
        match={k: v for k, v in match.items() if v is not None},
        radio=_radio(entry.get("radio") or {}, index),
        placement=_placement(entry.get("placement") or {}, index),
        network_name=str(entry.get("network_name") or "").strip() or default_network,
    )


def _derive_id(match: dict, index: int) -> str:
    parts = [str(match[k]) for k in ("gnb_id", "enb_id", "cell_id") if k in match]
    return "-".join(parts) if parts else "cell-%d" % index


def _radio(raw, index: int) -> RadioSpec:
    if not isinstance(raw, dict):
        raise ValueError("cells[%d].radio must be an object" % index)
    tech = str(raw.get("technology") or "nr").strip().lower()
    if tech not in _TECHNOLOGIES:
        raise ValueError("cells[%d].radio.technology must be one of %s, got %r"
                         % (index, "/".join(_TECHNOLOGIES), tech))
    band = str(raw.get("band") or "").strip()
    return RadioSpec(
        technology=tech,
        band=band,
        band_number=_band_number(band),
        arfcn=_int(raw.get("arfcn"), "cells[%d].radio.arfcn" % index),
        frequency_mhz=_float(raw.get("frequency_mhz"),
                             "cells[%d].radio.frequency_mhz" % index),
        bandwidth_mhz=_float(raw.get("bandwidth_mhz"),
                             "cells[%d].radio.bandwidth_mhz" % index),
        tx_power_dbm=_float(raw.get("tx_power_dbm"),
                            "cells[%d].radio.tx_power_dbm" % index),
        wifi_channel=_int(raw.get("wifi_channel"),
                          "cells[%d].radio.wifi_channel" % index),
    )


def _band_number(band: str) -> int | None:
    """The numeric part of a band written any of the ways people write it —
    "n48", "48", "b48", "B48". Only used to look up the EARFCN table."""
    digits = "".join(c for c in band if c.isdigit())
    return int(digits) if digits else None


def _placement(raw, index: int) -> PlacementSpec:
    if not isinstance(raw, dict):
        raise ValueError("cells[%d].placement must be an object" % index)
    anchor = str(raw.get("anchor_ap") or "").strip()
    plan = str(raw.get("floorplan") or "").strip()
    x = _float(raw.get("x_px"), "cells[%d].placement.x_px" % index)
    y = _float(raw.get("y_px"), "cells[%d].placement.y_px" % index)
    if plan and (x is None or y is None):
        raise ValueError("cells[%d].placement names a floorplan but no x_px/y_px"
                         % index)
    if (x is not None or y is not None) and not plan and not anchor:
        raise ValueError("cells[%d].placement has x_px/y_px but no floorplan "
                         "(a position without a plan cannot be drawn)" % index)
    return PlacementSpec(
        anchor_ap=anchor,
        dx_px=_float(raw.get("dx_px"), "cells[%d].placement.dx_px" % index) or 0.0,
        dy_px=_float(raw.get("dy_px"), "cells[%d].placement.dy_px" % index) or 0.0,
        floorplan=plan, x_px=x, y_px=y)


def _int(value, where: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError("%s must be a whole number, got %r" % (where, value))


def _float(value, where: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError("%s must be a number, got %r" % (where, value))
