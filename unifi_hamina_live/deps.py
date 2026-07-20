"""Shared FastAPI dependencies wiring the app state to request handlers."""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request

from .config import Settings, get_settings
from .models import Snapshot
from .unifi.collector import Collector


def collector(request: Request) -> Collector:
    return request.app.state.collector


def snapshot(request: Request) -> Snapshot:
    return request.app.state.collector.snapshot


def settings(request: Request) -> Settings:
    """Prefer the settings injected into the app (tests, explicit config);
    fall back to the process-wide singleton."""
    return getattr(request.app.state, "settings", None) or get_settings()


def require_meraki_key(
    x_cisco_meraki_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
    cfg: Settings = Depends(settings),
) -> None:
    """Enforce the Meraki-style API key when one is configured.

    Accepts either ``X-Cisco-Meraki-API-Key: <key>`` (the classic header) or
    ``Authorization: Bearer <key>`` (the v1 style). If no key is configured the
    facade runs open — convenient for local testing, flagged in the docs.
    """
    expected = cfg.meraki_compat_api_key.strip()
    if not expected:
        return
    presented = (x_cisco_meraki_api_key or "").strip()
    if not presented and authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer":
            presented = token.strip()
    if presented != expected:
        raise HTTPException(status_code=401, detail={"errors": ["Invalid API key."]})
