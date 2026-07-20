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

    # --- Meraki-compatible facade ----------------------------------------
    meraki_compat_api_key: str = Field(default="")
    meraki_org_name: str = Field(default="UniFi")

    # --- OpenIntent refresh ----------------------------------------------
    openintent_refresh_enabled: bool = Field(default=False)
    openintent_exporter_path: str = Field(
        default="../unifi-hamina-export/unifi_export.py"
    )
    openintent_mode: str = Field(default="innerspace")
    openintent_refresh_seconds: float = Field(default=900.0, ge=30.0)
    openintent_output_dir: str = Field(default="./exports")

    # --- Server -----------------------------------------------------------
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8080)

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
