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
from .open5g2go import (Open5G2GOClient, cells_from_enodeb_status,
                        cells_from_gnodeb_status, ues_from_connections)
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
        # An Open5G2GO backend, when there is one. Preferred over the core's own
        # endpoints where both are configured, because it answers strictly more:
        # the same cells and UEs, plus the RF read off the radio by SNMP and the
        # device names out of the subscriber database. See .open5g2go.
        url = (settings.open5g2go_url or "").strip()
        self._o5g2go = Open5G2GOClient(
            url, timeout=timeout, verify_tls=verify,
            prefix=settings.open5g2go_api_prefix) if url else None
        self.error: str | None = None
        self.status: dict = {"cells": 0, "ues": 0, "ue_detail": False}
        # Warn once per condition, not once per poll: the poll runs every 30s
        # for months and these say the same thing every time.
        self._warned: set[str] = set()
        # cell key -> the real MAC a source once read off that radio. SNMP is
        # the only thing that reports one, and it does not always answer, so
        # without this the cell's identity oscillates between its real MAC and
        # the synthetic one every time a poll times out. Everything downstream
        # is keyed on that MAC — client joins, placement, and the id Hamina
        # syncs against — so an oscillating identity presents as a device that
        # appears, vanishes and reappears as a different one. Seen live: an AP
        # that reached Hamina mid-outage, with the synthetic MAC and no radios,
        # and failed to synchronise.
        self._known_macs: dict[str, str] = {}

    @staticmethod
    def _client(url: str, timeout: float, verify: bool) -> Open5GSClient | None:
        url = (url or "").strip()
        return Open5GSClient(url, timeout=timeout, verify_tls=verify) if url else None

    @property
    def configured(self) -> bool:
        return any((self._o5g2go, self._amf, self._mme))

    @property
    def via_open5g2go(self) -> bool:
        return self._o5g2go is not None

    async def aclose(self) -> None:
        for client in (self._o5g2go, self._amf, self._mme, self._smf):
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
        cells, per_cell_ues = await self._cells()
        if self._settings.open5gs_include_ues:
            ues = await self._ues()
        else:
            ues = None
        sessions = await self._sessions()

        by_cell: dict[str, list[dict]] = {}
        floating: list[dict] = []
        for ue in ues or []:
            if per_cell_ues and _has_cell(ue):
                by_cell.setdefault(normalize.ue_cell_key(ue), []).append(ue)
            else:
                # A UE the source could not tie to a cell. Open5G2GO's
                # connection list is like this by design: it tracks a single
                # radio, so it never had a cell to name.
                floating.append(ue)

        # Floating UEs are only attributable when there is one cell to attribute
        # them to. With several, guessing would put clients on a radio that is
        # not carrying them — the same refusal the pre-2.7.7 fallback makes.
        single = len(cells) == 1
        # The UE list is the authority for a cell's client count only when every
        # UE could be placed on a cell — otherwise the number on the access
        # point and the clients drawn around it would be different sets. When it
        # is not, fall back to the count the source measured per cell (SNMP's
        # ue_count, or the core's num_connected_ues). Decided BEFORE the
        # unattributable UEs are dropped: emptying the list first would make it
        # look complete and throw away the count that was actually measured.
        attributable = per_cell_ues or single or not floating
        if floating and not attributable:
            self._warn_once(
                "floating-ues",
                "cellular: %d attached UE(s) arrived with no cell association "
                "and %d cells are live, so they are not shown on any of them. "
                "Read the core's /ue-info directly (OPEN5GS_AMF_URL / "
                "OPEN5GS_MME_URL) to attribute UEs per cell.",
                len(floating), len(cells))
            floating = []
        ue_detail = ues is not None and attributable

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
            if cell.get("mac"):
                self._known_macs[key] = cell["mac"]
            elif key in self._known_macs:
                cell = {**cell, "mac": self._known_macs[key]}
            spec = self.inventory.spec_for(cell)
            if spec is not None and not spec.is_fallback:
                used.add(spec.id)
            attached = list(by_cell.get(key, []))
            if single and floating:
                attached.extend(floating)
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
            "source": "open5g2go" if self._o5g2go is not None else "open5gs",
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
        """Every cell in the deployment, and whether the UE list that goes with
        it names a cell per UE (it does not on a core older than 2.7.7, and not
        on the Open5G2GO path at all)."""
        if self._o5g2go is not None:
            return await self._cells_from_open5g2go(), False
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

    async def _cells_from_open5g2go(self) -> list[dict]:
        """Cells from an Open5G2GO backend: both radio modes, either present.

        A 4G-mode deployment 404s the gNodeB route and a 5G-mode one 404s the
        eNodeB route, so asking for both and keeping whatever answers is how
        this works out which mode it is looking at — no configuration for the
        operator to keep in sync with the stack they actually started.
        """
        cells: list[dict] = []
        body = await self._o5g2go.get("/enodeb/status")
        if body:
            cells.extend(cells_from_enodeb_status(body))
        body = await self._o5g2go.get("/gnodeb/status")
        if body:
            cells.extend(cells_from_gnodeb_status(body))
        return cells

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
        """Every attached UE, or None when nothing here can list them."""
        if self._o5g2go is not None:
            body = await self._o5g2go.get("/connections")
            if body is None:
                return None
            return self._drop_idle(ues_from_connections(body))
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
        return self._drop_idle(found)

    def _drop_idle(self, ues: list[dict]) -> list[dict]:
        """An idle UE is registered but has no RRC connection, so no cell is
        currently carrying it. Counting it as a client of the cell it was last
        on would overstate what that radio is doing right now."""
        if self._settings.open5gs_include_idle_ues:
            return ues
        return [u for u in ues if u.get("state") != "idle"]

    async def _sessions(self) -> dict:
        if self._smf is None:
            return {}
        items = await self._smf.info("/pdu-info")
        return sessions_from_pdu_info(items) if items else {}


def _has_cell(ue: dict) -> bool:
    return ue.get("gnb_id") is not None or ue.get("enb_id") is not None


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
