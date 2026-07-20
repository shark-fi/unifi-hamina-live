"""FastAPI application factory.

Wires the UniFi collector (background poll loop) and, when enabled, the
scheduled OpenIntent refresher into the app lifespan, and mounts:

  * ``/``          live dashboard
  * ``/api``       vendor-neutral REST API
  * ``/api/v1``    Meraki Dashboard API v1 compatible facade
  * ``/openintent``scheduled OpenIntent artifact (when enabled)
  * ``/docs``      OpenAPI docs
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from .api.router import router as neutral_router
from .config import Settings, get_settings
from .meraki.router import router as meraki_router
from .refresh.openintent import OpenIntentRefresher
from .refresh.router import router as openintent_router
from .unifi.collector import Collector

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

_DASHBOARD = (Path(__file__).parent / "web" / "dashboard.html").read_text(encoding="utf-8")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    collector: Collector = app.state.collector
    collector.start()
    if settings.websocket_enabled:
        from .unifi.websocket import WebSocketListener

        app.state.ws_listener = WebSocketListener(settings, collector)
        app.state.ws_listener.start()
    if settings.openintent_refresh_enabled:
        app.state.refresher = OpenIntentRefresher(settings, collector=collector)
        app.state.refresher.start()
    try:
        yield
    finally:
        listener = getattr(app.state, "ws_listener", None)
        if listener is not None:
            await listener.stop()
        await collector.stop()
        refresher = getattr(app.state, "refresher", None)
        if refresher is not None:
            await refresher.stop()


def create_app(settings: Settings | None = None, collector: Collector | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="unifi-hamina-live",
        version="0.1.0",
        summary="Live UniFi Wi-Fi telemetry in a Meraki-compatible shape for Hamina Live.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.collector = collector or Collector(settings)

    app.include_router(neutral_router)
    app.include_router(meraki_router)
    app.include_router(openintent_router)

    if settings.catalyst_enabled:
        from collections import deque

        from .catalyst.auth import TokenStore
        from .catalyst.router import router as catalyst_router

        app.state.catalyst_tokens = TokenStore()
        app.state.catalyst_captured = deque(maxlen=500)
        app.include_router(catalyst_router)

        @app.middleware("http")
        async def _capture_dna(request, call_next):
            is_dna = request.url.path.startswith("/dna/")
            response = await call_next(request)
            if is_dna and settings.catalyst_log_requests:
                app.state.catalyst_captured.append({
                    "method": request.method,
                    "path": request.url.path,
                    "query": request.url.query,
                    "status": response.status_code,
                    "implemented": response.status_code != 404,
                    "authenticated": "X-Auth-Token" in request.headers,
                })
            return response

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard() -> str:
        return _DASHBOARD

    return app


app = create_app()
