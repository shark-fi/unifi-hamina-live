"""The Meraki poll: read one Dashboard organization's APs every collector tick.

Runs inside the existing tick, like the cellular, HaLow and LoRa sources, so the
Meraki APs are read at the same instant as the Wi-Fi around them. Nothing here
raises into the collector: a Dashboard outage must not stop the console being
polled. An AP seen before but not now (the API down, or the AP offline) stays on
the map greyed out rather than vanishing.

Cost per tick: device statuses, one ``wireless/status`` per online AP, and the
network client lists. The device list and channel utilisation change slowly and
are refreshed every few minutes, which keeps a small org well inside the
Dashboard's ~10 requests/second.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from ..cellular.cells import PlacementSpec
from ..config import Settings
from ..models import AccessPoint, Client
from . import normalize
from .api import MerakiAuthError, MerakiClient, MerakiError

log = logging.getLogger("unifi_hamina_live.meraki_live")

SLOW_REFRESH_S = 300


def parse_anchors(text: str) -> dict[str, PlacementSpec]:
    """``"Home-Lab=U7-Pro-Bedroom@30,0; Other AP=Kitchen"`` -> placement per AP name.

    Each Meraki AP rides on a placed UniFi AP, nudged by ``@dx,dy`` pixels so the
    two don't draw as one icon. Keyed by casefolded Meraki AP name.
    """
    out: dict[str, PlacementSpec] = {}
    for part in (text or "").split(";"):
        if "=" not in part:
            continue
        name, anchor = (s.strip() for s in part.split("=", 1))
        dx = dy = 0.0
        if "@" in anchor:
            anchor, off = anchor.rsplit("@", 1)
            try:
                dx, dy = (float(v) for v in off.split(","))
            except ValueError:
                pass
        if name and anchor.strip():
            out[name.casefold()] = PlacementSpec(anchor_ap=anchor.strip(), dx_px=dx, dy_px=dy)
    return out


@dataclass
class MerakiLiveSpec:
    org_id: str = ""
    network_ids: list[str] = field(default_factory=list)
    anchors: dict[str, PlacementSpec] = field(default_factory=dict)

    @staticmethod
    def from_settings(settings: Settings) -> "MerakiLiveSpec":
        return MerakiLiveSpec(
            org_id=(settings.meraki_live_org_id or "").strip(),
            network_ids=[n.strip() for n in (settings.meraki_live_network_ids or "").split(",") if n.strip()],
            anchors=parse_anchors(settings.meraki_live_anchors),
        )


class MerakiLiveSource:
    def __init__(self, settings: Settings, spec: MerakiLiveSpec | None = None,
                 client: MerakiClient | None = None) -> None:
        self.spec = spec if spec is not None else MerakiLiveSpec.from_settings(settings)
        key = (settings.meraki_live_api_key or "").strip()
        self._client = client or (MerakiClient(
            key, base_url=settings.meraki_live_base_url,
            timeout=settings.meraki_live_timeout_seconds) if key else None)
        self._devices: list[dict] = []
        self._networks: dict[str, str] = {}
        self._util: dict[tuple[str, str], float] = {}
        self._slow_at = 0.0
        self._last: dict[str, AccessPoint] = {}   # serial -> last AP seen, for greying out
        self.error: str | None = None
        self.status: dict = {}

    @property
    def configured(self) -> bool:
        return self._client is not None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def collect(
        self, site_id: str
    ) -> tuple[list[AccessPoint], list[Client], dict[str, PlacementSpec]]:
        """Meraki APs (as access points), their clients, and placement by AP MAC."""
        self.error = None
        if self._client is None:
            return [], [], {}
        try:
            aps, clients = await self._collect(site_id)
        except MerakiAuthError as exc:
            self.error = str(exc)
            log.warning("meraki: access refused: %s", exc)
            aps, clients = self._offline(site_id), []
        except MerakiError as exc:
            self.error = str(exc)
            log.warning("meraki poll failed: %s", exc)
            aps, clients = self._offline(site_id), []
        except Exception as exc:  # defensive: never break the Wi-Fi poll
            self.error = repr(exc)
            log.exception("unexpected meraki poll error")
            aps, clients = self._offline(site_id), []
        placements = {a.mac: self.spec.anchors[a.name.casefold()]
                      for a in aps if a.name.casefold() in self.spec.anchors}
        return aps, clients, placements

    async def _org(self) -> str:
        if not self.spec.org_id:
            orgs = await self._client.get("/organizations")
            usable = [o for o in orgs if (o.get("api") or {}).get("enabled", True)]
            if len(usable) != 1:
                raise MerakiError("the API key sees several organizations; set MERAKI_LIVE_ORG_ID to one of: "
                                  + ", ".join(f"{o['name']}={o['id']}" for o in orgs))
            self.spec.org_id = usable[0]["id"]
        return self.spec.org_id

    async def _slow(self, org: str) -> None:
        """Device list, network names and busy time: refreshed every few minutes."""
        if time.time() - self._slow_at < SLOW_REFRESH_S and self._devices:
            return
        self._networks = {n["id"]: n["name"] for n in await self._client.get(
            f"/organizations/{org}/networks", perPage=1000)}
        devices = await self._client.get(f"/organizations/{org}/devices",
                                         productTypes=["wireless"], perPage=1000)
        self._devices = [d for d in devices
                         if not self.spec.network_ids or d.get("networkId") in self.spec.network_ids]
        util: dict[tuple[str, str], float] = {}
        try:
            for row in await self._client.get(
                    f"/organizations/{org}/wireless/devices/channelUtilization/byDevice",
                    timespan=600, interval=600, perPage=1000):
                for b in row.get("byBand") or []:
                    band = normalize.band_of(b.get("band"))
                    pct = normalize.number((b.get("total") or {}).get("percentage"))
                    if band and pct is not None:
                        util[(row.get("serial"), band)] = round(pct, 1)
        except MerakiError as exc:
            log.debug("meraki channel utilisation unavailable: %s", exc)
        self._util = util
        self._slow_at = time.time()

    async def _collect(self, site_id: str) -> tuple[list[AccessPoint], list[Client]]:
        org = await self._org()
        await self._slow(org)
        statuses = {s["serial"]: s.get("status") for s in await self._client.get(
            f"/organizations/{org}/devices/statuses", productTypes=["wireless"], perPage=1000)}
        aps: list[AccessPoint] = []
        for d in self._devices:
            online = statuses.get(d["serial"]) in ("online", "alerting")
            status = await self._client.get(f"/devices/{d['serial']}/wireless/status") if online else None
            util = {band: pct for (serial, band), pct in self._util.items() if serial == d["serial"]}
            ap = normalize.access_point(d, site_id, online=online, status=status, utilization=util)
            aps.append(ap)
            self._last[d["serial"]] = ap

        clients: list[Client] = []
        by_serial = {a.serial: a for a in aps}
        for net in sorted({d.get("networkId") for d in self._devices if d.get("networkId")}):
            for row in await self._client.get(f"/networks/{net}/clients", timespan=300, perPage=1000):
                ap = by_serial.get(row.get("recentDeviceSerial"))
                if ap is None or not ap.online or (row.get("status") or "Online") != "Online" \
                        or row.get("recentDeviceConnection", "Wireless") != "Wireless":
                    continue
                clients.append(normalize.client(row, ap))
        for ap in aps:
            ap.num_clients = sum(1 for c in clients if c.ap_mac == ap.mac)

        self.status = {
            "org_id": org, "networks": {n: self._networks.get(n) for n in
                                        {d.get("networkId") for d in self._devices}},
            "access_points": len(aps), "online": sum(a.online for a in aps),
            "clients": len(clients), "error": None,
        }
        return aps, clients

    def _offline(self, site_id: str) -> list[AccessPoint]:
        """Every AP seen before, greyed out, while the Dashboard can't be read."""
        self.status = {**self.status, "error": self.error}
        return [ap.model_copy(update={"site_id": site_id, "online": False, "state": "offline",
                                      "radios": [], "num_clients": 0})
                for ap in self._last.values()]
