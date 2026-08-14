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

    # --- Catalyst Center (DNA Center) compatible facade ------------------
    # Hamina's "Cisco Catalyst (DNA) Center API" connector takes an Instance
    # URL + username/password and can disable TLS verification — so, unlike
    # Meraki, it can be pointed at this bridge. Enable the facade and set the
    # username/password Hamina will authenticate with.
    catalyst_enabled: bool = Field(default=False)
    catalyst_username: str = Field(default="")
    catalyst_password: str = Field(default="")
    # Record every /dna/* request (matched or not) so you can see exactly what
    # Hamina calls and implement to match. Served at /catalyst/_captured.
    catalyst_log_requests: bool = Field(default=True)
    # Debug bisect for the site hierarchy Hamina consumes: 1=area only,
    # 2=+buildings, 3=+floors (default). Lets you narrow which level a strict
    # client chokes on without rebuilding the image.
    catalyst_site_max_depth: int = Field(default=3, ge=1, le=3)
    # A real Catalyst maps/export job takes seconds to build the archive, so a
    # client polling the task sees it "running" before it goes "done". Simulate
    # that window (ms) — an instant-done task can trip a client that waits for
    # the running->done transition. 0 = complete immediately (used in tests).
    # A real appliance's maps/export ran for ~270ms: the first poll showed
    # "running" (progress counts, no endTime), the next showed "finished" with
    # the download path. Reproduce that running->done transition for a short
    # window so the client observes it as it would on a real box. 0 = instant.
    catalyst_export_delay_ms: int = Field(default=1500, ge=0)
    # Advertise floors WITH a map (mapGeometry/mapsSummary), which makes Hamina
    # attempt the maps/export image download on import. That auto-import can't
    # be completed against the facade yet (see docs/CATALYST.md), and Hamina
    # blocks Import until a floor plan is selected — so by default floors are
    # advertised WITHOUT a map: the floor + live AP data import cleanly and the
    # floor image is added once by hand. Flip to True if maps/export is fixed.
    catalyst_advertise_floor_maps: bool = Field(default=False)
    # Hamina's Catalyst connector treats a successful maps/export archive
    # DOWNLOAD as mandatory before it will sync device (AP) data, and that
    # download step can't be reproduced against the facade (see docs/CATALYST.md):
    #   * report success -> Hamina polls the task then never downloads (timeout)
    #   * report failure -> Hamina retries the export, then gives up
    # Neither reaches the device sync, so the Catalyst LIVE path is blocked on
    # Hamina's side. Left configurable for experimentation; default reports the
    # faithful success-shaped task.
    catalyst_maps_export_error: bool = Field(default=False)
    # Force every AP to report this model string, whatever it actually is.
    #
    # Purely diagnostic. Hamina refuses UniFi APs at import with "Some AP models
    # (...) aren't yet supported" for every spelling tried — UniFi's code, our
    # slug, Hamina's catalog display name, and Hamina's fully-qualified catalog
    # id (see catalyst/mapping.py). That points at its Catalyst connector
    # resolving models through a Cisco-only mapping rather than the catalog its
    # planner uses, but "points at" is not "proves".
    #
    # Set this to a real Cisco AP (e.g. "C9130AXI") and re-sync: if the import
    # completes, the vendor path is Cisco-only and no UniFi string could ever
    # have worked. NOT for normal use — every AP would then be labelled as
    # hardware you do not own, with the wrong antenna pattern driving the RF
    # model. Leave empty.
    catalyst_model_override: str = Field(default="")

    # --- RSSI sensors (WLAN Pi multilateration) ---------------------------
    # OFF by default, and not merely as a courtesy: this is the only thing in
    # the service that ACCEPTS data. Everything else is read-only, and the
    # browser extension says so in as many words ("GETs only, no writes,
    # ever"). Turning this on opens a POST endpoint, so it is opt-in and
    # refuses to start without a token.
    sensors_enabled: bool = Field(default=False)
    # Shared secret the sensors present as X-Sensor-Token. Required when
    # sensors_enabled — an ingest endpoint that anyone on the network can post
    # to is a way to move every located AP wherever an attacker likes.
    sensor_token: str = Field(default="")
    # JSON: which plan the sensors cover, and where each one sits ON THAT PLAN
    # in image pixels. Pixels rather than metres because that is what you can
    # actually read off a floor plan; the plan's meters_per_px converts them.
    sensor_config_path: str = Field(default="./sensors.json")
    # Path loss. Guesses until calibrated for the site (rssi_sensor.py
    # --calibrate); an uncalibrated exponent biases every distance the same
    # way, which a least-squares fit absorbs into a confident wrong position.
    sensor_rssi_at_1m: float = Field(default=-40.0)
    sensor_pathloss_exponent: float = Field(default=3.0, gt=0.0)
    # BLE transmits around 10 dBm where an AP runs about 20, so the same
    # distance reads roughly 10 dB weaker. Sharing the Wi-Fi intercept makes
    # every BLE fix read as further away than it is — consistently, which the
    # solver absorbs into a confident wrong position.
    sensor_ble_rssi_at_1m: float = Field(default=-50.0)
    # The exponent describes the BUILDING, not the radio, so BLE shares the
    # Wi-Fi one by default. 0 means exactly that; set it only if you have
    # measured BLE separately.
    sensor_ble_pathloss_exponent: float = Field(default=0.0, ge=0.0)
    # Samples older than this do not count toward a fix.
    sensor_window_seconds: float = Field(default=6.0, gt=0.0)
    # Below this many sensors hearing a transmitter, no position is reported.
    # Two circles intersect at two points; three are needed to choose.
    sensor_min_sensors: int = Field(default=3, ge=3)
    # Forget a transmitter unheard this long. A busy site produces a steady
    # stream of MACs heard once, and this service runs for months.
    sensor_forget_seconds: float = Field(default=300.0, gt=0.0)

    # --- OpenIntent refresh ----------------------------------------------
    openintent_refresh_enabled: bool = Field(default=False)
    # Baked into the image at a pinned commit (see the Dockerfile), so the
    # refresh works with nothing mounted. Running from a source checkout rather
    # than the image, point this at your own copy.
    openintent_exporter_path: str = Field(
        default="/opt/exporter/unifi_export.py"
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
