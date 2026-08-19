"""Background poller: fetch UniFi live state on an interval, normalize it, and
keep the latest :class:`Snapshot` available in memory for the API layers."""

from __future__ import annotations

import asyncio
import logging
import time
import zipfile

from ..config import Settings
from ..models import AccessPoint, FloorPlan, Site, Snapshot
from ..plan import load_openintent_plan, norm_name
from . import normalize, placement
from .client import (UniFiAuthError, UniFiClient, UniFiError,
                     UniFiUnreachableError)

log = logging.getLogger("unifi_hamina_live.collector")

# Backoff after a failed login, doubling per consecutive failure. A rate-limited
# console only clears if we stop knocking, so retrying at the poll interval is
# self-defeating: it keeps the limiter fed and the bridge locked out.
LOGIN_BACKOFF_START = 60.0
LOGIN_BACKOFF_MAX = 900.0


class Collector:
    """Owns the poll loop and the current snapshot.

    A ``client_factory`` can be injected (tests, alternative transports). By
    default it builds a real :class:`UniFiClient` from settings.
    """

    def __init__(self, settings: Settings, client_factory=None) -> None:
        self._settings = settings
        self._client_factory = client_factory or self._default_factory
        self._client: UniFiClient | None = None
        # consecutive login failures, and the time before which we refuse to
        # try again. Reset by any successful login.
        self._login_failures = 0
        self._login_block_until = 0.0
        self._login_error = ""
        self._snapshot = Snapshot(generated_at=0.0, ok=False, error="no poll yet")
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # cache of InnerSpace floor-plan image dimensions, keyed by image url,
        # so we fetch each image once and update positions cheaply thereafter.
        self._img_dims: dict[str, tuple[float, float]] = {}
        # cache of the raw floor-plan image bytes, keyed by plan id, so the
        # Catalyst maps/export archive can embed the real image without a
        # re-fetch. Populated alongside the dimension cache.
        self._img_bytes: dict[str, bytes] = {}
        # APs the OpenIntent plan places, keyed by casefolded name, as
        # (plan_id, x, y). These anchor cells that UniFi has never heard of —
        # a private cellular radio is drawn on the plan in Hamina like any
        # other AP and named in cells.json. Rebuilt each poll.
        self._plan_anchors: dict[str, tuple[str, float, float]] = {}
        # LTE/5G cells, when a core is configured. Built here rather than in
        # the app so a collector constructed by a test or a script carries it
        # too, and so the two sources share one tick and one snapshot.
        self.cellular = None
        self.cellular_note: str | None = None
        # Warned once, not per poll: a placement that cannot resolve says the
        # same thing every 30 seconds for as long as it is wrong.
        self._placement_warned: set[str] = set()
        if settings.open5gs_enabled:
            from ..cellular.source import CellularSource, load_inventory

            inventory, self.cellular_note = load_inventory(settings.open5gs_cells_path)
            if self.cellular_note:
                log.warning("open5gs: %s", self.cellular_note)
            self.cellular = CellularSource(settings, inventory)
            if not self.cellular.configured:
                self.cellular_note = (
                    "OPEN5GS_ENABLED is on but neither OPEN5GS_AMF_URL nor "
                    "OPEN5GS_MME_URL is set, so no core is being read")
                log.warning("open5gs: %s", self.cellular_note)

    def floor_image(self, plan_id: str) -> bytes | None:
        """Raw image bytes for a floor plan id, if the collector has fetched it."""
        return self._img_bytes.get(plan_id)

    def _default_factory(self) -> UniFiClient:
        s = self._settings
        return UniFiClient(
            s.unifi_host,
            s.unifi_username,
            s.unifi_password,
            verify_tls=s.unifi_verify_tls,
        )

    @property
    def snapshot(self) -> Snapshot:
        return self._snapshot

    async def apply_events(self, site_id: str, events: list[dict]) -> bool:
        """Apply a batch of WebSocket events to the current snapshot in place.
        Returns True if anything changed. Safe against a concurrent poll swap."""
        from . import events as ev_mod

        if not events:
            return False
        async with self._lock:
            snap = self._snapshot
            serial_by_mac = {
                a.mac: a.serial for a in snap.access_points if a.site_id == site_id
            }
            changed = False
            for ev in events:
                if ev_mod.apply_event(snap, ev, site_id, serial_by_mac):
                    changed = True
            if changed:
                snap.generated_at = time.time()
                # keep site rollup counts consistent for the touched site
                for s in snap.sites:
                    if s.id == site_id:
                        s.num_clients = len(snap.clients_for_site(site_id))
        return changed

    async def _session(self) -> "UniFiClient":
        """A logged-in client, reused across polls.

        This used to build a client and authenticate on EVERY poll — roughly 120
        logins an hour at the default interval. UniFi OS rate-limits
        authentication, so the bridge eventually locked itself out with
        "login failed: HTTP 429" and then kept hammering at the same rate, which
        is why it did not recover on its own. A session cookie is good for far
        longer than a poll interval; the only reason to re-authenticate is that
        the console stopped accepting the one we hold.
        """
        if self._client is None:
            now = time.time()
            if now < self._login_block_until:
                # Deliberately not attempting. Reusing sessions stops us
                # *creating* a rate limit; only backing off gets us out of one.
                raise UniFiError(
                    f"{self._login_error} — not retrying for "
                    f"{self._login_block_until - now:.0f}s after "
                    f"{self._login_failures} consecutive login failures"
                )
            client = self._client_factory()
            try:
                await client.login()
            except UniFiUnreachableError:
                # Not an auth failure: nothing was rejected, and nothing is
                # counting attempts. Backing off here would only delay
                # reconnecting when the host returns, and describing it as a
                # login failure sends the next person to check credentials that
                # were never presented.
                try:
                    await client.aclose()
                except Exception:  # pragma: no cover - best effort
                    pass
                raise
            except UniFiError as exc:
                self._login_failures += 1
                delay = min(
                    LOGIN_BACKOFF_START * 2 ** (self._login_failures - 1),
                    LOGIN_BACKOFF_MAX,
                )
                self._login_block_until = time.time() + delay
                self._login_error = str(exc)
                log.warning(
                    "login failed (%d in a row): %s — backing off %.0fs",
                    self._login_failures, exc, delay,
                )
                try:
                    await client.aclose()
                except Exception:  # pragma: no cover - best effort
                    pass
                raise
            self._login_failures = 0
            self._login_block_until = 0.0
            self._login_error = ""
            self._client = client
        return self._client

    async def _drop_session(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # pragma: no cover - best effort
                pass

    async def poll_once(self) -> Snapshot:
        """Fetch and normalize one full snapshot. Never raises; failures are
        recorded on the returned (and stored) snapshot."""
        started = time.time()
        try:
            try:
                snap = await self._collect(await self._session())
            except UniFiAuthError:
                # The session expired or was invalidated. Re-authenticate ONCE
                # and retry; if that fails too it is a real credentials problem,
                # not a stale cookie, and retrying further only feeds the rate
                # limiter that caused this fix.
                await self._drop_session()
                snap = await self._collect(await self._session())
        except UniFiError as exc:
            if "429" in str(exc):
                log.warning(
                    "poll failed: %s — the console is rate-limiting logins. "
                    "It clears once we stop trying, which the backoff above "
                    "now does; check nothing else is polling this console with "
                    "the same account.", exc)
            else:
                log.warning("poll failed: %s", exc)
            await self._drop_session()
            snap = Snapshot(
                generated_at=started, ok=False, error=str(exc),
                # keep last good data visible if we had any
                sites=self._snapshot.sites,
                access_points=self._snapshot.access_points,
                clients=self._snapshot.clients,
            )
        except Exception as exc:  # defensive: keep the loop alive
            log.exception("unexpected poll error")
            snap = Snapshot(generated_at=started, ok=False, error=repr(exc))
            await self._drop_session()

        # Cells are merged whether or not the console answered. A UniFi outage
        # must not blank the cellular half of the map, and a core outage must
        # not blank the Wi-Fi half — the sources fail independently and the
        # snapshot has to survive either one alone.
        await self._merge_cellular(snap)

        async with self._lock:
            self._snapshot = snap
        return snap

    async def _collect(self, client: UniFiClient) -> Snapshot:
        wanted = set(self._settings.site_filter)
        sites_raw = await client.sites()

        sites: list[Site] = []
        aps: list[AccessPoint] = []
        clients = []
        floorplans: list[FloorPlan] = []
        ap_by_mac: dict[str, AccessPoint] = {}
        for site in sites_raw:
            site_id = site.get("name")
            if not site_id or (wanted and site_id not in wanted):
                continue
            desc = site.get("desc") or site_id

            devices = await client.devices(site_id)
            site_aps = [
                normalize.access_point(d, site_id)
                for d in devices
                if normalize.is_access_point(d)
            ]
            serial_by_mac = {a.mac: a.serial for a in site_aps}

            stas = normalize.wireless_clients_only(await client.clients(site_id))
            site_clients = [
                normalize.client(s, site_id, serial_by_mac) for s in stas
            ]

            aps.extend(site_aps)
            clients.extend(site_clients)
            for a in site_aps:
                ap_by_mac.setdefault(a.mac, a)
            sites.append(
                Site(id=site_id, name=desc, num_aps=len(site_aps),
                     num_clients=len(site_clients))
            )

            # classic Maps placement is site-scoped and comes cheap from data
            # we already have (device x,y). Best-effort.
            if self._settings.placement_enabled:
                try:
                    fps, positions = placement.legacy_placement(
                        site_id, await client.maps(site_id), devices
                    )
                    floorplans.extend(fps)
                    self._apply_positions(ap_by_mac, positions)
                except UniFiError as exc:
                    log.debug("legacy placement unavailable for %s: %s", site_id, exc)

        # InnerSpace placement is console-global; only consult it for APs not
        # already placed via classic Maps.
        if self._settings.placement_enabled:
            if self._settings.plan_source == "openintent":
                self._openintent_placement(aps, floorplans, sites)
            else:
                await self._innerspace_placement(client, ap_by_mac, floorplans, sites)

        for fp in floorplans:
            fp.num_aps = sum(1 for a in aps if a.floorplan_id == fp.id)

        return Snapshot(
            generated_at=time.time(), ok=True, sites=sites,
            access_points=aps, clients=clients, floorplans=floorplans,
        )

    @staticmethod
    def _apply_positions(ap_by_mac, positions) -> None:
        for mac, pos in positions.items():
            ap = ap_by_mac.get(mac)
            if ap and ap.floorplan_id is None:  # don't overwrite an earlier source
                ap.floorplan_id = pos.floorplan_id
                ap.x = pos.x
                ap.y = pos.y

    def _openintent_placement(self, aps, floorplans, sites) -> None:
        """Plans and placements from a Hamina export instead of the console.

        Joined by MAC where the export publishes one and by name otherwise.
        Name alone is what the OpenIntent importer has always had to use in the
        other direction, and it is brittle: a 122-wall plan once imported with
        0 of 4 APs matched because Hamina had named them "Access Point 1..4".
        MAC first removes that failure for any export that carries one, and an
        AP that still matches nothing is named in the log rather than dropped,
        because the fix is a rename and that is not deducible from an AP which
        simply never appears on the map.
        """
        path = self._settings.plan_openintent_zip
        if not path:
            log.warning("PLAN_SOURCE=openintent but PLAN_OPENINTENT_ZIP is unset — "
                        "no floor plans, so nothing positional will work")
            return
        site_id = sites[0].id if sites else "default"
        try:
            plan = load_openintent_plan(path, site_id)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            log.error("floor plans unavailable: cannot read %s (%s). Export "
                      "OpenIntent from Hamina and mount it at that path.", path, exc)
            return

        floorplans.extend(plan.floorplans)
        self._img_bytes.update(plan.images)

        self._plan_anchors = {
            norm_name(p.name): (p.plan_id, p.x, p.y) for p in plan.aps}
        by_mac = {(a.mac or "").lower(): a for a in aps if a.mac}
        by_name = {norm_name(a.name): a for a in aps}
        placed, unmatched = 0, []
        for pap in plan.aps:
            ap = (by_mac.get(pap.mac) if pap.mac else None) or by_name.get(
                norm_name(pap.name))
            if ap is None:
                unmatched.append(pap.name)
                continue
            if ap.floorplan_id is None:  # don't overwrite an earlier source
                ap.floorplan_id = pap.plan_id
                ap.x, ap.y = round(pap.x, 2), round(pap.y, 2)
                placed += 1
        log.info("openintent: %d plan(s), %d of %d AP(s) placed from %s",
                 len(plan.floorplans), placed, len(plan.aps), path)
        if unmatched:
            # Not necessarily a mistake: anything drawn on the plan that is
            # not a UniFi device — a private cellular radio, say — lands here
            # by design, so this says what the names are good for rather than
            # only that they failed.
            log.warning("%d AP(s) in the plan match no live UniFi device by MAC "
                        "or name: %s — rename to match a console device, or use "
                        "the name as a cells.json anchor_ap for something UniFi "
                        "does not manage",
                        len(unmatched), ", ".join(sorted(unmatched)[:8]))

    async def _innerspace_placement(self, client, ap_by_mac, floorplans, sites) -> None:
        try:
            project = await client.innerspace_project()
        except UniFiError as exc:
            # A 403 here is almost always a permissions problem rather than a
            # broken console: InnerSpace is a separate UniFi application, and an
            # admin scoped to Network alone authenticates fine, reads APs and
            # clients fine, and is refused only here. Swallowing that silently
            # leaves /api/health saying ok:true and /api/floorplans returning [],
            # with nothing anywhere explaining why everything positional is
            # missing — the extension has nothing to pin to, the OpenIntent
            # export has no plans, and Hamina gets no floors.
            if "403" in str(exc):
                log.warning(
                    "InnerSpace refused this account (403) — floor plans and AP "
                    "placement will be unavailable. Grant %s access to the "
                    "InnerSpace application as well as Network "
                    "(UniFi -> Settings -> Admins & Users).",
                    self._settings.unifi_username or "the configured admin")
            else:
                log.info("InnerSpace placement unavailable: %s", exc)
            project = None
        if not project:
            return
        # fetch image dimensions once per plan image (cached across polls)
        dims_by_plan: dict[str, tuple[float, float]] = {}
        for plan_id, url in placement.innerspace_image_urls(project).items():
            if url not in self._img_dims:
                blob = await client.get_bytes(url)
                size = placement.image_size(blob) if blob else None
                if size:
                    self._img_dims[url] = (float(size[0]), float(size[1]))
                if blob:
                    self._img_bytes[plan_id] = blob
            if url in self._img_dims:
                dims_by_plan[plan_id] = self._img_dims[url]

        fps, positions = placement.innerspace_placement("", project, dims_by_plan)
        self._apply_positions(ap_by_mac, positions)
        # assign each InnerSpace plan to the site of an AP placed on it
        default_site = sites[0].id if sites else ""
        for fp in fps:
            owner = next((a for a in ap_by_mac.values() if a.floorplan_id == fp.id), None)
            fp.site_id = owner.site_id if owner else default_site
            if not any(existing.id == fp.id for existing in floorplans):
                floorplans.append(fp)

    # -- cellular ----------------------------------------------------------
    async def _merge_cellular(self, snap: Snapshot) -> None:
        """Fold the cells into the snapshot the console produced."""
        if self.cellular is None or not self.cellular.configured:
            return
        # Imported here, not at module scope, so the package is only loaded
        # when a core is actually configured — as the websocket and Catalyst
        # layers are.
        from ..cellular import normalize as cell_normalize

        # A failed console poll carries the last good access points forward,
        # and those already include the cells merged on the previous tick. Drop
        # them before merging again or every failed poll doubles the estate.
        snap.access_points[:] = [a for a in snap.access_points
                                 if a.source != "cellular"]
        snap.clients[:] = [
            c for c in snap.clients
            if not (c.ap_mac or "").startswith(cell_normalize.CELL_MAC_PREFIX)]

        site_id = self._cellular_site(snap)
        aps, clients, placements = await self.cellular.collect(site_id)
        if not aps:
            return

        anchors = {a.name.strip().casefold(): a for a in snap.access_points}
        for ap in aps:
            placement = placements.get(ap.mac)
            if placement is None:
                continue
            problem = cell_normalize.place(ap, placement, anchors,
                                           self._plan_anchors)
            if problem and problem not in self._placement_warned:
                self._placement_warned.add(problem)
                log.warning("open5gs placement: %s", problem)

        snap.access_points.extend(aps)
        snap.clients.extend(clients)
        if not any(s.id == site_id for s in snap.sites):
            # A console that has not answered yet — or a bridge pointed at a
            # core and nothing else — still needs a site for the cells to hang
            # off, or every projection downstream drops them.
            snap.sites.append(Site(id=site_id, name=site_id))
        for site in snap.sites:
            if site.id == site_id:
                site.num_aps = len(snap.aps_for_site(site_id))
                site.num_clients = len(snap.clients_for_site(site_id))
        for fp in snap.floorplans:
            fp.num_aps = sum(1 for a in snap.access_points if a.floorplan_id == fp.id)

    def _cellular_site(self, snap: Snapshot) -> str:
        """Which UniFi site the cells belong to.

        OPEN5GS_SITE_ID wins, then the inventory's own ``site_id``, then the
        first site polled — which is what a single-site console wants and the
        only guess worth making. The site decides which floor plans a cell may
        be placed on, so on a multi-site console this is not a detail.
        """
        configured = (self._settings.open5gs_site_id or "").strip()
        if configured:
            return configured
        declared = (self.cellular.inventory.site_id or "").strip()
        if declared:
            return declared
        return snap.sites[0].id if snap.sites else "default"

    # -- lifecycle ---------------------------------------------------------
    async def _loop(self) -> None:
        interval = self._settings.poll_interval_seconds
        while not self._stop.is_set():
            await self.poll_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="unifi-poller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
            self._task = None
        # the session outlives a poll now, so shutdown has to close it
        await self._drop_session()
        if self.cellular is not None:
            await self.cellular.aclose()
