"""Runtime configuration, loaded from environment / .env."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- UniFi console ----------------------------------------------------
    unifi_host: str = Field(default="https://192.168.1.1")
    unifi_username: str = Field(default="")
    unifi_password: str = Field(default="")
    unifi_verify_tls: bool = Field(default=False)
    unifi_sites: str = Field(
        default="",
        description="Comma-separated internal site names; empty = all sites.",
    )
    poll_interval_seconds: float = Field(default=30.0, ge=2.0)

    # Collect AP floor-plan placement (x,y) live from classic Maps / InnerSpace,
    # so an AP move flows through the live API instead of needing a full
    # OpenIntent rebuild. The OpenIntent zip then only carries the initial
    # import (floor-plan images + geometry).
    placement_enabled: bool = Field(default=True)

    # Experimental: subscribe to the controller's WebSocket event stream for
    # push updates (client connect/disconnect/roam, AP up/down). The periodic
    # poll stays on as the authoritative reconciler. Undocumented UniFi API.
    websocket_enabled: bool = Field(default=False)

    # --- Meraki-compatible facade ----------------------------------------
    meraki_compat_api_key: str = Field(default="")
    meraki_org_name: str = Field(default="UniFi")

    # --- OpenIntent refresh ----------------------------------------------
    openintent_refresh_enabled: bool = Field(default=False)
    openintent_exporter_path: str = Field(
        default="../unifi-hamina-export/unifi_export.py"
    )
    openintent_mode: str = Field(default="innerspace")
    # >0: regenerate the zip on that interval. 0: generate once at startup only
    # (initial import) — AP positions then flow live via the placement layer.
    openintent_refresh_seconds: float = Field(default=900.0, ge=0.0)
    openintent_output_dir: str = Field(default="./exports")
    # When a floor plan's structure changes (rescale/resize/replaced image/
    # added/removed plan) the exported zip goes stale. Default: flag it on
    # /openintent/status + log + optional webhook. Opt in to regenerate instead.
    openintent_auto_regenerate: bool = Field(default=False)
    openintent_stale_webhook: str = Field(
        default="", description="Optional URL to POST when the import goes stale."
    )

    # --- Server -----------------------------------------------------------
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8080)

    # --- Public exposure (Cloudflare Tunnel; used by docker compose) ------
    # Not consumed by the app itself — the `tunnel` compose profile reads it.
    cf_tunnel_token: str = Field(default="")

    @property
    def site_filter(self) -> list[str]:
        return [s.strip() for s in self.unifi_sites.split(",") if s.strip()]


_settings: Settings | None = None


def get_settings() -> Settings:
    """Process-wide singleton so the FastAPI DI graph shares one instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
