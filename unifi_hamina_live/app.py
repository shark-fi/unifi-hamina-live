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

import hashlib
import logging
import os
import platform
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


def _pkg_version() -> str:
    try:
        from importlib.metadata import version as _v
        return _v("unifi-hamina-live")
    except Exception:
        return "unknown"


def code_fingerprint() -> str:
    """A short hash of the Python actually loaded in this process.

    `sha` below is stamped from a build arg that only CI passes, so every
    locally built image reports "unknown" — and a local build is what the
    runbook tells people to do. An endpoint that exists to answer "am I running
    the fix?" answered it for CI images only, which is the case that needed it
    least.

    Hashing the source on disk needs no build arg, no git metadata in the image,
    and no cooperation from whoever built it. It is not a git SHA and cannot be
    read as one; it is a value you can reproduce against a checkout to see
    whether the two match:

        find unifi_hamina_live -name '*.py' | sort | xargs cat | shasum -a 256

    Same digest, same code. Different digest, the container is not running what
    you are reading.
    """
    root = Path(__file__).resolve().parent
    h = hashlib.sha256()
    for f in sorted(root.rglob("*.py")):
        h.update(f.read_bytes())
    return h.hexdigest()[:12]


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
    # computed once: the files cannot change under a running process
    app.state.code_fingerprint = code_fingerprint()

    @app.get("/version", include_in_schema=False)
    def version():
        """Which build is this, really.

        Every "the fix didn't work" report against this project has begun with a
        container that predated the fix — three times in one day at worst — and
        answering it meant exec'ing in and inferring from behaviour. A build that
        cannot say what it is makes every other diagnosis provisional.

        `sha` is stamped by CI and reads "unknown" on any local build, so
        `code` is the field to compare when you built the image yourself — see
        :func:`code_fingerprint`.
        """
        return {
            "name": "unifi-hamina-live",
            "version": _pkg_version(),
            "sha": os.environ.get("BUILD_SHA", "unknown"),
            "code": app.state.code_fingerprint,
            "built_at": os.environ.get("BUILD_TIME", "unknown"),
            "python": platform.python_version(),
        }

    # Sensor ingest: opt-in, and refuses to start without a token. A running
    # service that quietly accepts unauthenticated writes because a variable
    # was blank is worse than one that will not start.
    if settings.sensors_enabled:
        from .api.locate import router as locate_router
        from .locate.service import LocateService, Layout

        if not (settings.sensor_token or "").strip():
            raise RuntimeError(
                "SENSORS_ENABLED=true needs SENSOR_TOKEN set — ingest writes "
                "positions that get drawn on a floor plan, so it is not left "
                "open")
        try:
            layout = Layout.load(settings.sensor_config_path)
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                "SENSORS_ENABLED=true but %s could not be read: %s"
                % (settings.sensor_config_path, exc)) from exc
        app.state.locate = LocateService(settings, layout)
        app.include_router(locate_router)
        logging.getLogger(__name__).info(
            "sensor ingest on: %d sensor(s) on plan %s",
            len(layout.sensors), layout.plan_id)

    app.include_router(neutral_router)
    app.include_router(meraki_router)
    app.include_router(openintent_router)

    if settings.catalyst_enabled:
        from collections import deque

        from .catalyst.auth import TokenStore
        from .catalyst.maps import MapExportJobs
        from .catalyst.router import router as catalyst_router
        from .catalyst import mapping as catalyst_mapping

        # diagnostic: force every AP's reported model (see Settings)
        catalyst_mapping.MODEL_OVERRIDE = settings.catalyst_model_override
        if settings.catalyst_model_override:
            logging.getLogger(__name__).warning("catalyst: MODEL OVERRIDE active — every AP reports %r. "
                        "Diagnostic only; unset CATALYST_MODEL_OVERRIDE.",
                        settings.catalyst_model_override)

        app.state.catalyst_tokens = TokenStore()
        app.state.catalyst_maps = MapExportJobs()
        app.state.catalyst_captured = deque(maxlen=500)
        app.include_router(catalyst_router)

        # Our own debug/doc routes, and the NEUTRAL API's paths — dashboard and
        # extension polling would otherwise drown the log.
        #
        # Listed one by one rather than skipping "/api/" wholesale, because the
        # Catalyst facade also serves routes under /api/: /api/assurance/... (the
        # device sync) and /api/v1/task|file/... . A blanket "/api/" skip hid the
        # assurance call completely — it looked, across six captures, as though
        # Hamina never asked for device data, when the log simply could not see
        # the request. Anything added to the neutral router belongs here; anything
        # the facade serves must not.
        _skip = (
            "/catalyst/_captured", "/version", "/docs", "/openapi.json", "/redoc",
            "/report", "/api/located",
            "/api/health", "/api/sites", "/api/access-points", "/api/clients",
            "/api/floorplans", "/api/summary", "/api/map", "/api/refresh",
        )

        @app.middleware("http")
        async def _capture_dna(request, call_next):
            path = request.url.path
            response = await call_next(request)
            # Capture EVERY request (not just /dna/*) so any call Hamina makes —
            # including on an unexpected path — is visible; skip our own debug /
            # health / docs noise. Record a couple of client headers to reveal
            # which DNA Center client/version Hamina is emulating.
            if settings.catalyst_log_requests and not (
                path == "/" or any(path.startswith(p) for p in _skip)
            ):
                h = request.headers
                app.state.catalyst_captured.append({
                    "method": request.method,
                    "path": path,
                    "query": request.url.query,
                    "status": response.status_code,
                    "implemented": response.status_code != 404,
                    "authenticated": "X-Auth-Token" in h,
                    "user_agent": h.get("user-agent"),
                    "accept": h.get("accept"),
                })
            return response

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard() -> str:
        return _DASHBOARD

    return app


app = create_app()
