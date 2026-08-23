"""The HaLow poll: read the radio's ``ubus`` API, dress the result as an access
point and its clients.

Runs inside the existing collector tick, exactly like the cellular source and
for the same reason: one snapshot has to be internally consistent, so the HaLow
AP and its stations are read at the same instant as the Wi-Fi around them.

Nothing here raises into the collector. A radio that is down must not stop the
console being polled — the failure modes are independent and the snapshot
records each. A radio that was configured but is unreachable yields a single
*offline* access point, so a coverage map keeps the AP greyed out where it is
rather than dropping it (a gap reads as "no coverage", which is wronger than
"down").
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..config import Settings
from ..models import AccessPoint, Client
from ..cellular.cells import PlacementSpec
from . import normalize
from .ubus import UbusAuthError, UbusClient, UbusError, UbusUnreachableError

log = logging.getLogger("unifi_hamina_live.halow")


@dataclass(frozen=True)
class HalowSpec:
    """What the radio cannot tell you, declared in config: where it sits on a
    floor plan, and optional cosmetics.

    Placement reuses the cellular :class:`~..cellular.cells.PlacementSpec` and
    its anchor form — name a UniFi AP that is already placed and the HaLow AP
    rides on its position every poll — so a HaLow radio bolted next to a UniFi
    one needs no pixels in a file. See :func:`from_settings`.
    """

    name: str = ""
    model: str = ""
    placement: PlacementSpec = PlacementSpec()

    @staticmethod
    def from_settings(settings: Settings) -> "HalowSpec":
        placement = PlacementSpec(
            anchor_ap=(settings.halow_anchor_ap or "").strip(),
            dx_px=settings.halow_dx_px,
            dy_px=settings.halow_dy_px,
            floorplan=(settings.halow_floorplan or "").strip(),
            x_px=settings.halow_x_px,
            y_px=settings.halow_y_px,
        )
        return HalowSpec(
            name=(settings.halow_name or "").strip(),
            model=(settings.halow_model or "").strip(),
            placement=placement,
        )


class HalowSource:
    """Reads one HaLow radio's ``ubus`` API per poll."""

    def __init__(self, settings: Settings, spec: HalowSpec | None = None) -> None:
        self._settings = settings
        self.spec = spec if spec is not None else HalowSpec.from_settings(settings)
        self._device = (settings.halow_device or "wlan0").strip() or "wlan0"
        host = (settings.halow_host or "").strip()
        self._client = UbusClient(
            host,
            settings.halow_username,
            settings.halow_password,
            timeout=settings.halow_timeout_seconds,
            verify_tls=settings.halow_verify_tls,
        ) if host else None
        # Remembered so an unreachable radio can be drawn offline in the right
        # place: without the last-known BSSID there is no stable identity to
        # grey out, and a freshly-synthesized one would import as a new AP that
        # then vanishes when the radio returns.
        self._last_bssid: str | None = None
        self.error: str | None = None
        self.status: dict = {"online": False, "clients": 0}

    @property
    def configured(self) -> bool:
        return self._client is not None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    # -- the poll ----------------------------------------------------------
    async def collect(
        self, site_id: str
    ) -> tuple[list[AccessPoint], list[Client], dict[str, PlacementSpec]]:
        """The HaLow AP (as an access point), its stations (as clients), and
        the placement the AP asked for, keyed by MAC.

        Placement is not applied here — a HaLow AP anchored to a UniFi AP needs
        that AP's live position, which only the collector has — so this returns
        the unplaced access point plus its placement spec and the collector pins
        it, exactly as it does for cells.
        """
        self.error = None
        if self._client is None:
            return [], [], {}
        try:
            return await self._collect(site_id)
        except UbusAuthError as exc:
            self.error = str(exc)
            log.warning("halow login failed: %s", exc)
        except UbusUnreachableError as exc:
            self.error = str(exc)
            log.warning("halow radio unreachable: %s", exc)
        except UbusError as exc:
            self.error = str(exc)
            log.warning("halow poll failed: %s", exc)
        except Exception as exc:  # defensive: never break the Wi-Fi poll
            self.error = repr(exc)
            log.exception("unexpected halow poll error")
        return self._offline(site_id)

    async def _collect(
        self, site_id: str
    ) -> tuple[list[AccessPoint], list[Client], dict[str, PlacementSpec]]:
        info = await self._client.call("iwinfo", "info", {"device": self._device})
        if not info.get("bssid"):
            raise UbusError(
                f"iwinfo info for {self._device!r} carried no BSSID; is that "
                f"the HaLow interface?")

        assoc = await self._client.call(
            "iwinfo", "assoclist", {"device": self._device})
        stations = assoc.get("results") or []

        # ARP is a courtesy: it turns a station MAC into the IP it holds on the
        # bridge, which is genuinely useful on a map. It is allowed to fail
        # without taking the poll down — a client with no IP is still a client.
        arp = await self._arp_by_mac()

        ap = normalize.access_point(
            info, site_id, stations,
            name=self.spec.name or None, model=self.spec.model or None)
        self._last_bssid = ap.mac

        essid = info.get("ssid") or ""
        clients = [
            normalize.client(sta, ap, site_id, essid,
                             ip=arp.get(normalize.normalize_mac(sta.get("mac") or "")))
            for sta in stations
            if sta.get("mac")
        ]

        self.status = {"online": True, "clients": len(clients),
                       "ssid": essid, "bssid": ap.mac, "error": None}
        return [ap], clients, {ap.mac: self.spec.placement}

    def _offline(
        self, site_id: str
    ) -> tuple[list[AccessPoint], list[Client], dict[str, PlacementSpec]]:
        """The AP greyed out, if we have ever seen its BSSID. Before the first
        successful poll there is no identity to draw, so nothing is emitted —
        an AP invented from thin air would import and then vanish."""
        self.status = {"online": False, "clients": 0, "error": self.error}
        if self._last_bssid is None:
            return [], [], {}
        ap = normalize.offline_ap(
            self._last_bssid, site_id,
            name=self.spec.name or None, model=self.spec.model or None)
        return [ap], [], {ap.mac: self.spec.placement}

    async def _arp_by_mac(self) -> dict[str, str]:
        """``normalized-mac -> ip`` from the radio's ARP table, best-effort."""
        try:
            table = await self._client.call("rpc-oui", "arp_table", {})
        except UbusError as exc:
            log.debug("halow arp_table unavailable: %s", exc)
            return {}
        out: dict[str, str] = {}
        for entry in table.get("entries") or []:
            mac = normalize.normalize_mac(entry.get("macaddr") or "")
            ip = entry.get("ipaddr")
            if mac and ip:
                out[mac] = ip
        return out
