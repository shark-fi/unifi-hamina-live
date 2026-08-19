"""Vendor-neutral, normalized models for live Wi-Fi state.

These are the internal representation the collector produces. Both the
Meraki-compatible facade and the neutral REST API are projections of these.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Radio(BaseModel):
    """One radio (band) on an access point."""

    band: str = Field(description="One of '2.4', '5', '6' (GHz).")
    channel: int | None = None
    channel_width_mhz: int | None = None
    tx_power_dbm: float | None = None
    num_clients: int = 0
    # Live RF health, when the console reports it.
    channel_utilization_pct: float | None = None
    tx_retries_pct: float | None = None
    # What this radio actually is. Wi-Fi radios leave these at their defaults;
    # a cellular radio dressed as Wi-Fi (see cellular/rf.py) sets them, so the
    # true carrier survives beside the band/channel it is wearing. A reader that
    # only knows Wi-Fi ignores them and sees a normal radio; one that cares can
    # tell a costume from the real thing.
    technology: str = Field(
        default="wifi", description="'wifi', 'lte' or 'nr'.")
    carrier_mhz: float | None = Field(
        default=None,
        description="Real centre frequency, when this is not a Wi-Fi radio.")
    carrier_label: str | None = Field(
        default=None,
        description="Human summary of the real carrier, e.g. "
        "'NR n48 ARFCN 636667 (3550.0 MHz, 40 MHz)'.")


class AccessPoint(BaseModel):
    site_id: str
    name: str
    mac: str = Field(description="Colon-separated lowercase MAC.")
    serial: str = Field(description="Synthesized stable pseudo-serial.")
    model_code: str = Field(description="Raw UniFi model code, e.g. 'U7PRO'.")
    model: str = Field(description="Human model, e.g. 'u7-pro'.")
    ip: str | None = None
    state: str = "unknown"
    online: bool = False
    uptime_seconds: int | None = None
    firmware: str | None = None
    num_clients: int = 0
    radios: list[Radio] = Field(default_factory=list)
    # Floor-plan placement, filled only when InnerSpace/Maps data is available.
    floorplan_id: str | None = None
    x: float | None = None
    y: float | None = None
    # Where this came from: 'unifi' for a real console device, 'cellular' for an
    # LTE/5G cell presented as an access point (see the cellular package).
    # Additive on purpose — every existing reader keeps working and sees the
    # default — but it is the one field that tells a real AP from a costumed
    # cell, so anything that reports hardware should read it.
    source: str = Field(default="unifi", description="'unifi' or 'cellular'.")


class Client(BaseModel):
    """A wireless station currently associated to an AP."""

    mac: str
    hostname: str | None = None
    name: str | None = Field(
        default=None,
        description="User-set alias in UniFi, when there is one. Distinct from "
        "hostname, which is what the device calls itself.",
    )
    ip: str | None = None
    site_id: str
    ap_mac: str | None = Field(default=None, description="MAC of the AP it is on.")
    ap_serial: str | None = None
    essid: str | None = None
    band: str | None = None
    channel: int | None = None
    rssi: int | None = None
    signal_dbm: int | None = None
    noise_dbm: int | None = None
    tx_rate_kbps: int | None = None
    rx_rate_kbps: int | None = None
    tx_bytes: int | None = None
    rx_bytes: int | None = None
    uptime_seconds: int | None = None
    is_guest: bool = False
    dev_id: int | None = Field(
        default=None,
        description="UniFi fingerprint device-type id. Its icon is served at "
        "https://static.ui.com/fingerprint/0/<dev_id>_101x101.png — a device "
        "type, not a device.",
    )
    vendor: str | None = Field(
        default=None, description="Vendor from the MAC OUI, when UniFi resolved one."
    )


class FloorPlan(BaseModel):
    """A floor plan an AP can be placed on. Pixel dimensions match the image
    that was imported into Hamina, so live x,y line up with the import."""

    id: str
    site_id: str
    name: str
    source: str = Field(description="'legacy' (classic Maps), "
                    "'innerspace', or 'openintent' (a Hamina export, for a "
                    "console with no InnerSpace).")
    width_px: float | None = None
    height_px: float | None = None
    meters_per_px: float | None = None
    image_ref: str | None = Field(
        default=None,
        description="Image identity (InnerSpace urlImage); changes when the "
        "floor-plan image is replaced. Used for staleness detection.",
    )
    num_aps: int = 0


class Site(BaseModel):
    id: str = Field(description="UniFi internal site name (stable id).")
    name: str = Field(description="Human site description.")
    num_aps: int = 0
    num_clients: int = 0


class Snapshot(BaseModel):
    """One consistent poll of the whole console."""

    generated_at: float = Field(description="Unix epoch seconds of this poll.")
    ok: bool = True
    error: str | None = None
    sites: list[Site] = Field(default_factory=list)
    access_points: list[AccessPoint] = Field(default_factory=list)
    clients: list[Client] = Field(default_factory=list)
    floorplans: list[FloorPlan] = Field(default_factory=list)

    def floorplans_for_site(self, site_id: str) -> list[FloorPlan]:
        return [f for f in self.floorplans if f.site_id == site_id]

    def aps_for_site(self, site_id: str) -> list[AccessPoint]:
        return [a for a in self.access_points if a.site_id == site_id]

    def clients_for_site(self, site_id: str) -> list[Client]:
        return [c for c in self.clients if c.site_id == site_id]

    def ap_by_serial(self, serial: str) -> AccessPoint | None:
        return next((a for a in self.access_points if a.serial == serial), None)

    def clients_for_ap(self, ap_mac: str) -> list[Client]:
        return [c for c in self.clients if c.ap_mac == ap_mac]
