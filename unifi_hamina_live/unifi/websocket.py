"""Experimental WebSocket listener for the UniFi controller event stream.

Opens one connection per site to ``…/wss/s/<site>/events`` and feeds decoded
events into the :class:`Collector`, so client connect/disconnect/roam and AP
up/down surface in near real time instead of at the poll interval. Best-effort
by design: any failure logs and backs off, and the periodic poll reconciles
state regardless. Only active when ``WEBSOCKET_ENABLED`` is set.
"""

from __future__ import annotations

import asyncio
import logging
import ssl

from ..config import Settings
from . import events as ev_mod
from .client import UniFiClient, UniFiError
from .collector import Collector

log = logging.getLogger("unifi_hamina_live.websocket")

try:  # optional dependency; only needed when the listener is enabled
    import websockets
except Exception:  # pragma: no cover - import guard
    websockets = None


class WebSocketListener:
    def __init__(self, settings: Settings, collector: Collector, client_factory=None) -> None:
        self._s = settings
        self._collector = collector
        self._client_factory = client_factory or (
            lambda: UniFiClient(
                settings.unifi_host,
                settings.unifi_username,
                settings.unifi_password,
                verify_tls=settings.unifi_verify_tls,
            )
        )
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if websockets is None:
            log.warning("websocket disabled: the 'websockets' package is not installed")
            return
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="unifi-ws")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
            self._task = None

    # -- connection management --------------------------------------------
    async def _run(self) -> None:
        """Log in once, discover sites, then hold a listener per site with
        reconnect/backoff until asked to stop."""
        backoff = 2.0
        while not self._stop.is_set():
            client = self._client_factory()
            try:
                await client.login()
                sites = await self._sites(client)
                if not sites:
                    raise UniFiError("no sites visible for websocket")
                log.info("websocket: listening on %d site(s)", len(sites))
                await asyncio.gather(
                    *(self._listen_site(client, site) for site in sites)
                )
                backoff = 2.0  # clean exit (stop requested)
            except Exception as exc:  # noqa: BLE001 - stay alive, poll reconciles
                if self._stop.is_set():
                    break
                log.warning("websocket error (%s); retrying in %.0fs", exc, backoff)
                await self._sleep(backoff)
                backoff = min(backoff * 2, 60.0)
            finally:
                await client.aclose()

    async def _sites(self, client: UniFiClient) -> list[str]:
        wanted = set(self._s.site_filter)
        names = [s.get("name") for s in await client.sites() if s.get("name")]
        return [n for n in names if not wanted or n in wanted]

    def _ssl_context(self, url: str):
        if not url.startswith("wss"):
            return None
        ctx = ssl.create_default_context()
        if not self._s.unifi_verify_tls:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def _listen_site(self, client: UniFiClient, site: str) -> None:
        url = client.events_ws_url(site)
        headers = client.auth_headers()
        async with websockets.connect(
            url,
            additional_headers=headers,
            ssl=self._ssl_context(url),
            open_timeout=15,
            ping_interval=20,
            max_size=2**22,
        ) as ws:
            log.info("websocket connected: %s", site)
            while not self._stop.is_set():
                raw = await ws.recv()
                if isinstance(raw, bytes):
                    raw = raw.decode(errors="replace")
                evs = ev_mod.parse_message(raw)
                if evs:
                    await self._collector.apply_events(site, evs)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
