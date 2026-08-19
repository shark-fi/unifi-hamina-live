"""Reader for an **Open5G2GO** deployment's own API.

[Open5G2GO](https://github.com/Waveriders-Collective/open5G2GO) is a private
4G/5G toolkit around Open5GS: the core, a subscriber database, and a management
backend that already knows things the core never will. Reading *it* instead of
the core is the better source wherever it is available, and not by a small
margin — most of what :mod:`.cells` otherwise asks you to declare by hand comes
back live:

``GET /api/v1/enodeb/status``
    Per eNodeB, from SNMP against the radio itself: **band class, EARFCN,
    bandwidth, TX power**, PCI, cell id, TAC, S1/RF state, connected UE count,
    throughput, and **PRB utilisation** — which is a genuine load figure, not a
    costume, and lands on the radio as ``channel_utilization_pct`` where a Wi-Fi
    controller would put channel utilisation. Also the radio's real MAC, serial,
    model and firmware, plus the name and location from ``enodebs.yaml``.

``GET /api/v1/gnodeb/status``
    The 5G equivalent. No SNMP layer today, so it gives identity and NGAP state;
    the carrier still has to be declared in ``cells.json``.

``GET /api/v1/connections``
    Attached UEs with IMSI, device name, IP and CM state — already resolved
    against the subscriber database, so a UE arrives named rather than as a
    bare SUPI.

**One caveat, and it decides when to use this.** These connections carry no cell
association: Open5G2GO tracks a single eNodeB/gNodeB, so there is nothing to
attribute. With one cell that is exact. With several, this reader refuses to
split UEs across them and says so — go via the core's own ``/ue-info`` instead,
which reports the NR-CGI per UE (see :mod:`.open5gs`).
"""

from __future__ import annotations

import logging

import httpx

from .open5gs import Open5GSError, describe

log = logging.getLogger("unifi_hamina_live.cellular")


class Open5G2GOClient:
    """One Open5G2GO backend. Read-only: every call is a GET."""

    def __init__(self, base_url: str, timeout: float = 5.0,
                 verify_tls: bool = True, prefix: str = "/api/v1") -> None:
        self.base_url = base_url.rstrip("/")
        self.prefix = "/" + prefix.strip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url, timeout=timeout, verify=verify_tls,
            follow_redirects=True,
        )
        self._unavailable: set[str] = set()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(self, path: str) -> dict | None:
        """A JSON object from the backend, or None if it does not serve it.

        404 rather than an exception for a missing route: the 5G endpoints are
        absent on a 4G-mode deployment and vice versa, and asking for both is
        how this works out which one it is talking to.
        """
        if path in self._unavailable:
            return None
        url = self.prefix + path
        try:
            resp = await self._client.get(url)
        except httpx.HTTPError as exc:
            raise Open5GSError(
                "%s%s: %s" % (self.base_url, url, describe(exc))) from exc
        if resp.status_code == 404:
            self._unavailable.add(path)
            log.info("open5g2go %s does not serve %s", self.base_url, url)
            return None
        if resp.status_code != 200:
            raise Open5GSError("%s%s: HTTP %d"
                               % (self.base_url, url, resp.status_code))
        try:
            body = resp.json()
        except ValueError as exc:
            raise Open5GSError("%s%s: response was not JSON (%s)"
                               % (self.base_url, url, exc)) from exc
        if not isinstance(body, dict):
            raise Open5GSError("%s%s: expected a JSON object"
                               % (self.base_url, url))
        return body


# --- reading the payloads -------------------------------------------------

def cells_from_enodeb_status(body: dict) -> list[dict]:
    """``/enodeb/status`` -> cell records, SNMP facts included.

    The SNMP list is preferred and the S1AP list fills in for a radio SNMP
    could not reach — an eNodeB with a live S1 connection but no SNMP answer is
    up, and dropping it because one of two readers failed would take a working
    cell off the map.
    """
    network = body.get("network") or {}
    plmn = str(network.get("plmn") or "")
    snmp = (body.get("snmp") or {}).get("enodebs") or []
    s1ap = (body.get("s1ap") or {}).get("enodebs") or []

    live = {str(i.get("serial_number") or ""): i for i in s1ap
            if isinstance(i, dict) and i.get("connected")}

    cells: list[dict] = []
    seen_serials: set[str] = set()
    for item in snmp:
        if not isinstance(item, dict):
            continue
        cell = _snmp_cell(item, plmn)
        serial = str(cell.get("serial") or item.get("serial_number") or "")
        if not item.get("reachable") and serial in live:
            # A radio SNMP cannot reach still appears in the SNMP list, as an
            # entry with reachable:false — so the S1AP fallback below never
            # fires for it, and the cell was drawn offline while carrying its
            # attached UEs. The S1 link is the authority on whether the eNodeB
            # is up; SNMP only adds RF detail, and its absence leaves radios
            # empty, which is honest. Seen live: SNMP timing out because it was
            # configured with a different address than the one S1AP connected
            # from.
            cell["connected"] = True
            # Prefer the address S1AP connected FROM over the one SNMP was
            # configured with: when SNMP is timing out, the configured address
            # is frequently the reason, and showing it as the radio's IP sends
            # whoever debugs this next to the wrong host.
            cell["peer"] = live[serial].get("ip_address") or cell.get("peer") or ""
        cells.append(cell)
        if serial:
            seen_serials.add(serial)

    for item in s1ap:
        if not isinstance(item, dict):
            continue
        serial = str(item.get("serial_number") or "")
        if serial and serial in seen_serials:
            continue
        cells.append({
            "technology": "lte",
            "source": "open5g2go",
            "name": item.get("config_name") or item.get("serial_number") or "eNodeB",
            "location": item.get("location") or "",
            "serial": serial,
            "plmn": plmn,
            "peer": item.get("ip_address") or "",
            "connected": bool(item.get("connected")),
            "num_ues": 0,
        })
    return cells


def _snmp_cell(item: dict, plmn: str) -> dict:
    identity = item.get("identity") or {}
    radio = item.get("cell") or {}
    conn = item.get("connection") or {}
    perf = item.get("performance") or {}
    power = item.get("tx_power") or {}
    reachable = bool(item.get("reachable"))
    # "Up" means the core has it: S1 established and the RF actually on. An
    # eNodeB answering SNMP with its transmitter off is not serving anybody, and
    # showing it green would be the map lying about coverage.
    online = reachable and bool(conn.get("s1_link_up")) and bool(conn.get("rf_enabled"))
    return {
        "technology": "lte",
        "source": "open5g2go",
        "name": (item.get("config_name") or identity.get("enodeb_name")
                 or item.get("serial_number") or "eNodeB"),
        "location": item.get("location") or "",
        "serial": identity.get("serial_number") or item.get("serial_number") or "",
        "mac": _mac(identity.get("mac_address")),
        "model": identity.get("product_type") or "",
        "firmware": identity.get("software_version") or "",
        "plmn": plmn,
        "tac": _str(radio.get("tac")),
        "cell_id": _int(radio.get("cell_id")),
        "pci": _int(radio.get("pci")),
        "peer": item.get("ip_address") or "",
        "connected": online,
        "reachable": reachable,
        "num_ues": _int(conn.get("ue_count")) or 0,
        # live RF — these override anything declared in cells.json, because a
        # figure read off the radio beats a figure typed into a file
        "band_number": _int(radio.get("band_class")),
        "arfcn": _int(radio.get("earfcn")),
        "bandwidth_mhz": _bandwidth_mhz(radio.get("bandwidth")),
        "tx_power_dbm": _float(power.get("current_dbm")),
        # PRB utilisation is a real load measurement and goes where a Wi-Fi
        # controller would report channel utilisation. Downlink, because that is
        # the direction a coverage/capacity view is about.
        "utilization_pct": _float(perf.get("dl_prb_pct")),
        "dl_throughput_kbps": _int(perf.get("dl_throughput_kbps")),
        "ul_throughput_kbps": _int(perf.get("ul_throughput_kbps")),
    }


def cells_from_gnodeb_status(body: dict) -> list[dict]:
    """``/gnodeb/status`` -> cell records.

    No SNMP layer on the 5G side today, so this carries identity and NGAP state
    only; the carrier comes from ``cells.json``. The shape is defensive on
    purpose — the endpoint is newer than the 4G one and its key names have less
    history behind them.
    """
    network = body.get("network") or {}
    plmn = str(network.get("plmn") or "")
    items = body.get("gnodebs")
    if items is None:
        items = (body.get("ngap") or {}).get("gnodebs") or []
    cells = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        cells.append({
            "technology": "nr",
            "source": "open5g2go",
            "name": (item.get("config_name") or item.get("name")
                     or item.get("gnb_id") or "gNodeB"),
            "location": item.get("location") or "",
            "plmn": plmn,
            "gnb_id": _int(item.get("gnb_id")),
            "peer": item.get("ip_address") or "",
            "connected": bool(item.get("connected", True)),
            "num_ues": _int(item.get("ue_count")) or 0,
        })
    return cells


def ues_from_connections(body: dict) -> list[dict]:
    """``/connections`` -> UE records.

    Names come out of the subscriber database, so a UE is "Camera 3" rather
    than an IMSI — worth more on a shared screen than anything the core alone
    could give, and it is why this reader keeps the name separate from the
    (masked) identifier rather than overwriting one with the other.
    """
    ues = []
    for item in body.get("connections") or []:
        if not isinstance(item, dict):
            continue
        state = str(item.get("cm_state") or "").strip().lower()
        ues.append({
            "supi": str(item.get("imsi") or "").strip(),
            "name": str(item.get("name") or "").strip(),
            "state": "connected" if state == "connected" else state or "idle",
            "ip": str(item.get("ip") or "").strip(),
            "dnn": str(item.get("apn") or "").strip(),
            # No cell association: Open5G2GO tracks one radio, so there is
            # nothing to attribute. The source assigns them to the single cell
            # and refuses to guess when there is more than one.
            "gnb_id": None,
            "enb_id": None,
            "cell_id": None,
        })
    return ues


# --- small readers --------------------------------------------------------

def _bandwidth_mhz(value) -> float | None:
    """``"20 MHz"`` -> ``20.0``. Open5G2GO maps the Baicells' PRB-count encoding
    (25/50/75/100) to a human string before it leaves the backend, so this parses
    the string rather than re-deriving it — one interpretation of that OID, in
    the project that owns the MIB."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    digits = "".join(c for c in str(value) if c.isdigit() or c == ".")
    try:
        return float(digits) if digits else None
    except ValueError:
        return None


def _mac(value) -> str:
    """A real MAC off the radio, normalised. Empty means fall back to the
    synthetic one — a cell must have a stable MAC either way, because that is
    the key every client joins to."""
    from ..unifi.normalize import normalize_mac

    return normalize_mac(str(value)) if value else ""


def _str(value) -> str:
    return "" if value is None else str(value)


def _int(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
