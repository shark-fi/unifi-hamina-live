"""The LoRa poll: read a MikroTik gateway's RouterOS API, dress its ``/lora``
interface as an access point.

Runs inside the existing collector tick, exactly like the cellular and HaLow
sources and for the same reason: one snapshot has to be internally consistent,
so the LoRa gateway is read at the same instant as the Wi-Fi around it.

Nothing here raises into the collector. A gateway that is down must not stop the
console being polled — the failure modes are independent and the snapshot
records each. A gateway that was configured but is unreachable yields a single
*offline* access point, so a coverage map keeps it greyed out where it is rather
than dropping it (a gap reads as "no coverage", which is wronger than "down").

Unlike HaLow, a LoRa gateway has no stations to enumerate — it is a LoRaWAN
packet forwarder, not an association point — so this source only ever produces
one access point and never any clients.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from ..config import Settings
from ..models import AccessPoint, Client
from ..cellular.cells import PlacementSpec
from . import normalize
from .api import (RouterOSAuthError, RouterOSClient, RouterOSError,
                  RouterOSUnreachableError)

log = logging.getLogger("unifi_hamina_live.lora")


@dataclass(frozen=True)
class LoraSpec:
    """What the gateway cannot tell you, declared in config: where it sits on a
    floor plan, and optional cosmetics.

    Placement reuses the cellular :class:`~..cellular.cells.PlacementSpec` and
    its anchor form — name a UniFi AP that is already placed and the LoRa
    gateway rides on its position every poll — so a gateway bolted next to a
    UniFi AP needs no pixels in a file. See :func:`from_settings`.
    """

    name: str = ""
    model: str = ""
    interface: str = ""
    placement: PlacementSpec = PlacementSpec()

    @staticmethod
    def from_settings(settings: Settings) -> "LoraSpec":
        placement = PlacementSpec(
            anchor_ap=(settings.lora_anchor_ap or "").strip(),
            dx_px=settings.lora_dx_px,
            dy_px=settings.lora_dy_px,
            floorplan=(settings.lora_floorplan or "").strip(),
            x_px=settings.lora_x_px,
            y_px=settings.lora_y_px,
        )
        return LoraSpec(
            name=(settings.lora_name or "").strip(),
            model=(settings.lora_model or "").strip(),
            interface=(settings.lora_interface or "").strip(),
            placement=placement,
        )


class LoraSource:
    """Reads one MikroTik LoRa gateway's RouterOS API per poll."""

    def __init__(self, settings: Settings, spec: LoraSpec | None = None) -> None:
        self._settings = settings
        self.spec = spec if spec is not None else LoraSpec.from_settings(settings)
        host = (settings.lora_host or "").strip()
        self._client = RouterOSClient(
            host,
            settings.lora_username,
            settings.lora_password,
            port=settings.lora_port,
            timeout=settings.lora_timeout_seconds,
        ) if host else None
        # Remembered so an unreachable gateway can be drawn offline in the right
        # place: without the last-known MAC there is no stable identity to grey
        # out, and a freshly-synthesized one would import as a new AP that then
        # vanishes when the gateway returns.
        self._last_mac: str | None = None
        self.error: str | None = None
        self.status: dict = {"online": False}

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
        """The LoRa gateway (as an access point), no clients, and the placement
        it asked for keyed by MAC.

        Placement is not applied here — a gateway anchored to a UniFi AP needs
        that AP's live position, which only the collector has — so this returns
        the unplaced access point plus its placement spec and the collector pins
        it, exactly as it does for cells and HaLow APs.
        """
        self.error = None
        if self._client is None:
            return [], [], {}
        try:
            return await self._collect(site_id)
        except RouterOSAuthError as exc:
            self.error = str(exc)
            log.warning("lora login failed: %s", exc)
        except RouterOSUnreachableError as exc:
            self.error = str(exc)
            log.warning("lora gateway unreachable: %s", exc)
        except RouterOSError as exc:
            self.error = str(exc)
            log.warning("lora poll failed: %s", exc)
        except Exception as exc:  # defensive: never break the Wi-Fi poll
            self.error = repr(exc)
            log.exception("unexpected lora poll error")
        return self._offline(site_id)

    async def _collect(
        self, site_id: str
    ) -> tuple[list[AccessPoint], list[Client], dict[str, PlacementSpec]]:
        rows = await self._client.command("/lora/print", detail="")
        lora = self._pick_interface(rows)
        if lora is None:
            raise RouterOSError(
                "no /lora interface found"
                + (f" named {self.spec.interface!r}" if self.spec.interface
                   else "; is the lora package installed?"))

        mac = await self._identity(lora)
        model = self.spec.model or await self._board_model()
        server = await self._server_label(lora.get("servers"))

        ap = normalize.access_point(
            lora, mac, site_id,
            name=self.spec.name or None, model=model or None, server=server)
        self._last_mac = ap.mac

        self.status = {
            "online": ap.online,
            "name": lora.get("name"),
            "gateway_id": lora.get("gateway-id") or lora.get("hardware-id"),
            "channel_plan": lora.get("channel-plan"),
            "server": server,
            "status": lora.get("status"),
            "error": None,
        }
        return [ap], [], {ap.mac: self.spec.placement}

    def _pick_interface(self, rows: list[dict]) -> dict | None:
        """The LoRa interface to read: the one named in config, else the first."""
        if not rows:
            return None
        wanted = self.spec.interface.strip().casefold()
        if wanted:
            return next((r for r in rows
                         if (r.get("name") or "").strip().casefold() == wanted),
                        None)
        return rows[0]

    async def _identity(self, lora: dict) -> str:
        """A stable 48-bit MAC for the AP: the box's ethernet MAC when we can
        read one, else a locally-administered MAC synthesized from the gateway
        EUI (which is 64-bit and not itself a MAC)."""
        try:
            eths = await self._client.command(
                "/interface/ethernet/print", detail="")
        except RouterOSError as exc:
            log.debug("lora ethernet read unavailable: %s", exc)
            eths = []
        for eth in eths:
            mac = (eth.get("mac-address") or "").strip()
            if mac:
                return mac
        gateway_id = (lora.get("gateway-id") or lora.get("hardware-id")
                      or "").strip()
        return _synth_mac(gateway_id or (lora.get("name") or "lora"))

    async def _board_model(self) -> str | None:
        """The RouterBOARD model (e.g. ``RBwAPR-2nD``), best-effort."""
        try:
            rows = await self._client.command("/system/routerboard/print")
        except RouterOSError as exc:
            log.debug("lora routerboard read unavailable: %s", exc)
            return None
        if rows:
            return rows[0].get("model") or rows[0].get("board-name") or None
        return None

    async def _server_label(self, active: str | None) -> str | None:
        """The upstream network server, ``name (address)`` when the server list
        resolves — a courtesy for the honest label, allowed to fail."""
        name = (active or "").strip()
        if not name:
            return None
        try:
            servers = await self._client.command(
                "/lora/servers/print", detail="")
        except RouterOSError as exc:
            log.debug("lora servers read unavailable: %s", exc)
            return name
        for srv in servers:
            if (srv.get("name") or "").strip() == name:
                address = (srv.get("address") or "").strip()
                return f"{name} ({address})" if address else name
        return name

    def _offline(
        self, site_id: str
    ) -> tuple[list[AccessPoint], list[Client], dict[str, PlacementSpec]]:
        """The gateway greyed out, if we have ever seen its MAC. Before the
        first successful poll there is no identity to draw, so nothing is
        emitted — an AP invented from thin air would import and then vanish."""
        self.status = {"online": False, "error": self.error}
        if self._last_mac is None:
            return [], [], {}
        ap = normalize.offline_ap(
            self._last_mac, site_id,
            name=self.spec.name or None, model=self.spec.model or None)
        return [ap], [], {ap.mac: self.spec.placement}


def _synth_mac(seed: str) -> str:
    """A stable, locally-administered 48-bit MAC derived from a seed string.

    Used only when the box's real ethernet MAC cannot be read. The
    locally-administered bit (0x02 in the first octet) is set and multicast bit
    cleared, so it is a valid unicast MAC that cannot collide with a real OUI.
    """
    digest = hashlib.sha1(seed.strip().lower().encode()).digest()
    first = (digest[0] | 0x02) & 0xFE
    octets = [first] + list(digest[1:6])
    return ":".join(f"{b:02x}" for b in octets)
