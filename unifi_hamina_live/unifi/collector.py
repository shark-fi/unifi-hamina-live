"""Background poller: fetch UniFi live state on an interval, normalize it, and
keep the latest :class:`Snapshot` available in memory for the API layers."""

from __future__ import annotations

import asyncio
import logging
import time

from ..config import Settings
from ..models import AccessPoint, FloorPlan, Site, Snapshot
from . import normalize, placement
from .client import UniFiClient, UniFiError

log = logging.getLogger("unifi_hamina_live.collector")


class Collector:
    """Owns the poll loop and the current snapshot.

    A ``client_factory`` can be injected (tests, alternative transports). By
    default it builds a real :class:`UniFiClient` from settings.
    """

    def __init__(self, settings: Settings, client_factory=None) -> None:
        self._settings = settings
        self._client_factory = client_factory or self._default_factory
        self._snapshot = Snapshot(generated_at=0.0, ok=False, error="no poll yet")
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # cache of InnerSpace floor-plan image dimensions, keyed by image url,
        # so we fetch each image once and update positions cheaply thereafter.
        self._img_dims: dict[str, tuple[float, float]] = {}

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

    async def poll_once(self) -> Snapshot:
        """Fetch and normalize one full snapshot. Never raises; failures are
        recorded on the returned (and stored) snapshot."""
        started = time.time()
        client = self._client_factory()
        try:
            await client.login()
            snap = await self._collect(client)
        except UniFiError as exc:
            log.warning("poll failed: %s", exc)
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
        finally:
            await client.aclose()

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

    async def _innerspace_placement(self, client, ap_by_mac, floorplans, sites) -> None:
        try:
            project = await client.innerspace_project()
        except UniFiError:
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
