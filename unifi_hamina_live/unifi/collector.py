"""Background poller: fetch UniFi live state on an interval, normalize it, and
keep the latest :class:`Snapshot` available in memory for the API layers."""

from __future__ import annotations

import asyncio
import logging
import time

from ..config import Settings
from ..models import Site, Snapshot
from . import normalize
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
        aps = []
        clients = []
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
            sites.append(
                Site(
                    id=site_id,
                    name=desc,
                    num_aps=len(site_aps),
                    num_clients=len(site_clients),
                )
            )

        return Snapshot(
            generated_at=time.time(),
            ok=True,
            sites=sites,
            access_points=aps,
            clients=clients,
        )

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
