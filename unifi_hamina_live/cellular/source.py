"""The cellular poll: read the core, dress the result as access points.

Runs inside the existing collector tick rather than on a loop of its own. That
is deliberate — one snapshot has to be internally consistent, and a cell whose
client list came from a different instant than the Wi-Fi around it would show up
as clients orbiting an AP that no longer has them.

Nothing here raises into the collector. A core that is down must not stop the
console being polled, exactly as a console that is down must not stop the cells
being read: the two failure modes are independent and the snapshot records each.
"""

from __future__ import annotations

import logging

from ..config import Settings
from ..models import AccessPoint, Client
from . import normalize, prom
from .cells import CellInventory, CellSpec, PlacementSpec
from .open5gs import (Open5GSClient, Open5GSError, cells_from_enb_info,
                      cells_from_gnb_info, sessions_from_pdu_info,
                      ues_from_ue_info)

log = logging.getLogger("unifi_hamina_live.cellular")


class CellularSource:
    """Reads one Open5GS core (AMF and/or MME, optionally SMF) per poll."""

    def __init__(self, settings: Settings,
                 inventory: CellInventory | None = None) -> None:
        self._settings = settings
        self.inventory = inventory if inventory is not None else CellInventory.empty()
        timeout = settings.open5gs_timeout_seconds
        verify = settings.open5gs_verify_tls
        self._amf = self._client(settings.open5gs_amf_url, timeout, verify)
        self._mme = self._client(settings.open5gs_mme_url, timeout, verify)
        self._smf = self._client(settings.open5gs_smf_url, timeout, verify)
        self.error: str | None = None
        self.status: dict = {"cells": 0, "ues": 0, "ue_detail": False}
        # Warn once per condition, not once per poll: the poll runs every 30s
        # for months and these say the same thing every time.
        self._warned: set[str] = set()

    @staticmethod
    def _client(url: str, timeout: float, verify: bool) -> Open5GSClient | None:
        url = (url or "").strip()
        return Open5GSClient(url, timeout=timeout, verify_tls=verify) if url else None

    @property
    def configured(self) -> bool:
        return any((self._amf, self._mme))

    async def aclose(self) -> None:
        for client in (self._amf, self._mme, self._smf):
            if client is not None:
                await client.aclose()

    def _warn_once(self, key: str, message: str, *args) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.warning(message, *args)

    # -- the poll ----------------------------------------------------------
    async def collect(
        self, site_id: str
    ) -> tuple[list[AccessPoint], list[Client], dict[str, PlacementSpec]]:
        """Cells (as access points), UEs (as clients), and where each cell goes.

        Placement is *not* applied here: a cell anchored to a UniFi AP needs
        that AP's live position, which only the collector has. This returns
        unplaced access points plus the placement each one asked for, keyed by
        MAC, and the collector pins them.
        """
        self.error = None
        try:
            return await self._collect(site_id)
        except Open5GSError as exc:
            self.error = str(exc)
            log.warning("open5gs poll failed: %s", exc)
        except Exception as exc:  # defensive: never break the Wi-Fi poll
            self.error = repr(exc)
            log.exception("unexpected open5gs poll error")
        # A core that went away should not blank the map it was drawn on, but
        # neither should it keep claiming clients it can no longer see.
        offline = [(normalize.offline_cell(spec, site_id), spec)
                   for spec in self.inventory.declared_cells]
        self.status = {"cells": len(offline), "ues": 0, "ue_detail": False,
                       "error": self.error}
        return ([ap for ap, _ in offline], [],
                {ap.mac: spec.placement for ap, spec in offline})

    async def _collect(
        self, site_id: str
    ) -> tuple[list[AccessPoint], list[Client], dict[str, PlacementSpec]]:
        cells, ue_detail = await self._cells()
        if self._settings.open5gs_include_ues:
            ues = await self._ues()
            if ues is None:      # no core here can list them
                ues, ue_detail = [], False
        else:
            # Asked not to list UEs. That must fall back to the core's own
            # tally rather than counting the (empty) list we did not fetch —
            # otherwise switching subscriber listing off silently reports every
            # cell as having no clients at all.
            ues, ue_detail = [], False
        sessions = await self._sessions()

        by_cell: dict[str, list[dict]] = {}
        for ue in ues:
            by_cell.setdefault(normalize.ue_cell_key(ue), []).append(ue)

        aps: list[AccessPoint] = []
        clients: list[Client] = []
        placements: dict[str, PlacementSpec] = {}
        # Which declared specs a live cell claimed. Tracked by spec id rather
        # than by rebuilding a cell key from the spec: a spec matches on any
        # subset of the identity (usually just the gNB id), so a key built from
        # it would not equal the key built from the core's fuller record, and
        # every live cell would also be reported offline.
        used: set[str] = set()
        for cell in cells:
            key = normalize.cell_key(cell)
            spec = self.inventory.spec_for(cell)
            if spec is not None and not spec.is_fallback:
                used.add(spec.id)
            attached = by_cell.get(key, [])
            # When the UE list is available it is the authority for the count,
            # so the number on the AP and the clients drawn around it are the
            # same set. Without it, fall back to the core's own tally.
            count = len(attached) if ue_detail else cell.get("num_ues", 0)
            ap = normalize.access_point(cell, spec, site_id, num_clients=count)
            aps.append(ap)
            if spec is not None:
                placements[ap.mac] = spec.placement
            if spec is None:
                self._warn_once(
                    "unspecced:" + key,
                    "open5gs: %s is not described in the cell inventory — it "
                    "will appear with no radio and no position. Add a cells.json "
                    "entry with match %s.", ap.name, self._match_hint(cell))
            essid = (spec.network_name if spec else "") or self._essid(cell)
            for ue in attached:
                session = sessions.get(_bare(ue.get("supi") or ""))
                clients.append(normalize.client(
                    ue, ap, site_id, essid, session=session,
                    mask=self._settings.open5gs_mask_supi))

        for ap, spec in self._missing(used, site_id):
            aps.append(ap)
            placements[ap.mac] = spec.placement
        self.status = {
            "cells": len(aps),
            "ues": len(clients),
            "ue_detail": ue_detail,
            "error": None,
        }
        return aps, clients, placements

    @staticmethod
    def _match_hint(cell: dict) -> str:
        for key in ("gnb_id", "enb_id"):
            if cell.get(key) is not None:
                return '{"%s": %s}' % (key, cell[key])
        return "{}"

    @staticmethod
    def _essid(cell: dict) -> str:
        """What a client's 'network' reads as when nothing named it. The PLMN is
        the honest answer — it is the network the UE actually joined."""
        plmn = cell.get("plmn") or ""
        return "PLMN %s" % plmn if plmn else ""

    def _missing(self, used: set[str],
                 site_id: str) -> list[tuple[AccessPoint, CellSpec]]:
        """Declared cells no live cell claimed — shown offline, not dropped."""
        return [(normalize.offline_cell(spec, site_id), spec)
                for spec in self.inventory.declared_cells
                if spec.id not in used]

    # -- the three reads ---------------------------------------------------
    async def _cells(self) -> tuple[list[dict], bool]:
        """Every cell the core is talking to, and whether per-UE detail is
        available at all (it is not on a core older than 2.7.7)."""
        cells: list[dict] = []
        detail = False
        if self._amf is not None:
            items = await self._amf.info("/gnb-info")
            if items is None:
                cells.extend(await self._cells_from_metrics(self._amf, "nr"))
            else:
                cells.extend(cells_from_gnb_info(items))
                detail = True
        if self._mme is not None:
            items = await self._mme.info("/enb-info")
            if items is None:
                cells.extend(await self._cells_from_metrics(self._mme, "lte"))
            else:
                cells.extend(cells_from_enb_info(items))
                detail = True
        return cells, detail

    async def _cells_from_metrics(self, client: Open5GSClient,
                                  technology: str) -> list[dict]:
        """The pre-2.7.7 fallback: ``/metrics`` totals against declared cells.

        The gauges here (``gnb``/``ran_ue``, ``enb``/``enb_ue``) are core-wide
        with no per-cell labels, so this can only be honest about one cell. With
        several declared it refuses to guess which of them the UEs are on and
        reports the count on none of them, rather than inventing a split.
        """
        samples = await client.metrics()
        ues = prom.total(samples, "ran_ue" if technology == "nr" else "enb_ue")
        declared = [s for s in self.inventory.declared_cells
                    if s.radio.technology == technology]
        if not declared:
            self._warn_once(
                "nofallback:" + technology,
                "open5gs %s: no per-cell endpoint on this core and no %s cell "
                "declared in the inventory, so there is nothing to draw. "
                "Declare the cell with an explicit match, or upgrade the core "
                "to 2.7.7+.", client.base_url, technology.upper())
            return []
        if len(declared) > 1:
            self._warn_once(
                "ambiguous:" + technology,
                "open5gs %s: this core cannot say which cell a UE is on and %d "
                "%s cells are declared, so client counts are left at zero. "
                "Upgrade to 2.7.7+ for per-cell counts.",
                client.base_url, len(declared), technology.upper())
        share = int(ues or 0) if len(declared) == 1 else 0
        node_key = "gnb_id" if technology == "nr" else "enb_id"
        return [{
            "technology": technology,
            node_key: spec.match.get(node_key),
            "plmn": str(spec.match.get("plmn") or ""),
            "name": spec.name,
            "connected": True,
            "num_ues": share,
            "peer": "",
        } for spec in declared]

    async def _ues(self) -> list[dict] | None:
        """Every attached UE, or None when no core here can list them."""
        found: list[dict] = []
        any_detail = False
        for client in (self._amf, self._mme):
            if client is None:
                continue
            items = await client.info("/ue-info")
            if items is None:
                continue
            any_detail = True
            found.extend(ues_from_ue_info(items))
        if not any_detail:
            return None
        if not self._settings.open5gs_include_idle_ues:
            # An idle UE is registered but has no RRC connection, so no cell is
            # currently carrying it. Counting it as a client of the cell it was
            # last on would overstate what the radio is doing right now.
            found = [u for u in found if u.get("state") != "idle"]
        return found

    async def _sessions(self) -> dict:
        if self._smf is None:
            return {}
        items = await self._smf.info("/pdu-info")
        return sessions_from_pdu_info(items) if items else {}


def _bare(supi: str) -> str:
    value = supi.strip().lower()
    for prefix in ("imsi-", "nai-", "supi-"):
        if value.startswith(prefix):
            return value[len(prefix):]
    return value


def load_inventory(path: str) -> tuple[CellInventory, str | None]:
    """The inventory, plus why it is empty if it is.

    A missing or broken cells.json is not fatal — the same call the sensor
    layout makes. Cells still appear with their identity and client counts;
    they just have no radio and no position, and the reason has to be visible
    somewhere other than a stack trace, so it is returned rather than raised.
    """
    try:
        return CellInventory.load(path), None
    except FileNotFoundError:
        return CellInventory.empty(), (
            "no cell inventory at %s — cells will appear with no radio and no "
            "position. Copy cells.example.json to %s and describe your cells."
            % (path, path))
    except (OSError, ValueError) as exc:
        return CellInventory.empty(), "cell inventory %s is unusable: %s" % (path, exc)
