"""Reader for an Open5GS network function's metrics server.

Every Open5GS NF can run one HTTP server (``metrics.server`` in its YAML,
conventionally port 9090). It serves Prometheus text at ``/metrics``, and since
**Open5GS 2.7.7** the NFs also register JSON dumpers on it:

===========  ===============  ==================================================
NF           endpoint         what it answers
===========  ===============  ==================================================
AMF (5G)     ``/gnb-info``    every connected gNB: id, name, PLMN, NG/SCTP peer,
                              supported TAC list, slices, connected-UE count
AMF (5G)     ``/ue-info``     every UE: SUPI, PEI, CM state, the gNB and cell it
                              is on, NR-CGI/TAI, slices, session count
MME (4G)     ``/enb-info``    the same, per eNB, over S1
MME (4G)     ``/ue-info``     every UE: IMSI, eNB id + cell id, TAI, APN/bearers
SMF          ``/pdu-info``    per SUPI: PDU sessions with DNN, **UE IPv4**, QoS
                              flows, N3 peers, and an active/idle activity flag
===========  ===============  ==================================================

That last table is the whole reason this integration is possible without
touching the RAN: ``/gnb-info`` is a cell list with a live client count, and
``/ue-info`` is the UE-to-cell association. ``/metrics`` alone could never say
which UE is on which cell — its gauges (``ran_ue``, ``gnb``, ``amf_session``,
``enb_ue``, ``enb``) are core-wide totals with no per-cell labels.

**On an older core the dumpers are not there.** An unknown path on that server
answers ``400 Bad Request``, not 404, so this client treats both as "this build
does not have it", says so once, and falls back to the ``/metrics`` totals. You
still get a cell on the map with a client count; you do not get individual UEs.
"""

from __future__ import annotations

import logging

import httpx

from . import prom

log = logging.getLogger("unifi_hamina_live.cellular")

# Bound on how far we will follow the dumpers' pager. 100 items a page, so this
# is 5,000 UEs — far past anything a lab core holds, and it stops a malformed
# `next` from looping forever.
MAX_PAGES = 50


class Open5GSError(RuntimeError):
    pass


class Open5GSClient:
    """One NF's metrics server. Read-only: every call is a GET."""

    def __init__(self, base_url: str, timeout: float = 5.0,
                 verify_tls: bool = True) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url, timeout=timeout, verify=verify_tls,
            follow_redirects=True,
        )
        # endpoints this build answered "unknown path" for; asked once, then
        # left alone. A poll loop that re-probes a missing endpoint every
        # interval fills the log with a fact that will not change until the
        # core is upgraded.
        self._unavailable: set[str] = set()

    async def aclose(self) -> None:
        await self._client.aclose()

    def supports(self, path: str) -> bool:
        return path not in self._unavailable

    async def metrics(self) -> list[prom.Sample]:
        """The Prometheus gauges/counters, parsed. Raises on transport error."""
        try:
            resp = await self._client.get("/metrics")
        except httpx.HTTPError as exc:
            raise Open5GSError("%s: %s" % (self.base_url, exc)) from exc
        if resp.status_code != 200:
            raise Open5GSError("%s/metrics: HTTP %d"
                               % (self.base_url, resp.status_code))
        return prom.parse(resp.text)

    async def info(self, path: str) -> list[dict] | None:
        """All items from a JSON dumper, following its pager.

        Returns None — not an empty list — when this build does not serve the
        endpoint. The difference matters: no endpoint means "fall back to the
        totals", an empty list means "nothing is attached right now".
        """
        if path in self._unavailable:
            return None
        items: list[dict] = []
        page = 0
        while page < MAX_PAGES:
            body = await self._page(path, page)
            if body is None:
                return None
            batch = body.get("items")
            if isinstance(batch, list):
                items.extend(i for i in batch if isinstance(i, dict))
            pager = body.get("pager") or {}
            if not isinstance(pager, dict) or not pager.get("next"):
                return items
            page += 1
        log.warning("open5gs %s%s: stopped after %d pages", self.base_url, path,
                    MAX_PAGES)
        return items

    async def _page(self, path: str, page: int) -> dict | None:
        try:
            resp = await self._client.get(
                path, params={"page": page, "page_size": 100}
            )
        except httpx.HTTPError as exc:
            raise Open5GSError("%s%s: %s" % (self.base_url, path, exc)) from exc
        if resp.status_code in (400, 404):
            # The metrics server answers 400 for a path it has no dumper for,
            # which is how a pre-2.7.7 core presents itself.
            self._unavailable.add(path)
            log.warning(
                "open5gs %s has no %s (HTTP %d) — this needs Open5GS 2.7.7 or "
                "newer. Falling back to /metrics totals: cells will show a "
                "client count but individual UEs will not be listed.",
                self.base_url, path, resp.status_code)
            return None
        if resp.status_code != 200:
            raise Open5GSError("%s%s: HTTP %d"
                               % (self.base_url, path, resp.status_code))
        try:
            body = resp.json()
        except ValueError as exc:
            raise Open5GSError("%s%s: response was not JSON (%s)"
                               % (self.base_url, path, exc)) from exc
        if not isinstance(body, dict):
            raise Open5GSError("%s%s: expected a JSON object" % (self.base_url, path))
        return body


# --- reading the dumper payloads ------------------------------------------
# Kept as functions over plain dicts rather than models: these shapes belong to
# Open5GS, they differ between 4G and 5G, and pinning them into classes here
# would mean a schema to migrate every time the core adds a field. The neutral
# models are in ../models.py; this is the layer that flattens 4G and 5G into one
# vocabulary for `normalize.py` to work from.

def cells_from_gnb_info(items: list[dict]) -> list[dict]:
    """Flatten AMF ``/gnb-info`` into cell records.

    One record per gNB. A gNB serving several TACs still gets one record — the
    TAC list rides along, because a cell in this integration is the thing on the
    wall, not the tracking area it announces.
    """
    cells = []
    for item in items:
        tas = item.get("supported_ta_list") or []
        tacs = [str(t.get("tac")) for t in tas if isinstance(t, dict) and t.get("tac")]
        cells.append({
            "technology": "nr",
            "gnb_id": item.get("gnb_id"),
            "name": (item.get("name") or "").strip(),
            "plmn": str(item.get("plmn") or ""),
            "tac": tacs[0] if tacs else "",
            "tacs": tacs,
            "peer": ((item.get("ng") or {}).get("sctp") or {}).get("peer") or "",
            "connected": bool((item.get("ng") or {}).get("setup_success")),
            "num_ues": _int(item.get("num_connected_ues")) or 0,
            "slices": _slices(tas),
        })
    return cells


def cells_from_enb_info(items: list[dict]) -> list[dict]:
    """Flatten MME ``/enb-info`` into the same cell record shape."""
    cells = []
    for item in items:
        tas = item.get("supported_ta_list") or []
        tacs = [str(t.get("tac")) for t in tas if isinstance(t, dict) and t.get("tac")]
        cells.append({
            "technology": "lte",
            "enb_id": item.get("enb_id"),
            "name": (item.get("name") or "").strip(),
            "plmn": str(item.get("plmn") or ""),
            "tac": tacs[0] if tacs else "",
            "tacs": tacs,
            "peer": ((item.get("s1") or {}).get("sctp") or {}).get("peer") or "",
            "connected": bool((item.get("s1") or {}).get("setup_success")),
            "num_ues": _int(item.get("num_connected_ues")) or 0,
            "slices": [],
        })
    return cells


def _slices(tas: list) -> list[str]:
    out: list[str] = []
    for ta in tas:
        if not isinstance(ta, dict):
            continue
        for bplmn in ta.get("bplmns") or []:
            if not isinstance(bplmn, dict):
                continue
            for sn in bplmn.get("snssai") or []:
                if isinstance(sn, dict) and sn.get("sst") is not None:
                    label = "sst=%s" % sn["sst"]
                    if sn.get("sd"):
                        label += "/sd=%s" % sn["sd"]
                    if label not in out:
                        out.append(label)
    return out


def ues_from_ue_info(items: list[dict]) -> list[dict]:
    """Flatten ``/ue-info`` from either AMF or MME into one UE record shape.

    The two differ in more than field names: the AMF reports an NR-CGI (so the
    cell is unambiguous) while the MME reports an eNB id plus an E-CGI cell id,
    and the AMF's SUPI is prefixed (``imsi-…``) where the MME's is bare. Both
    come out of here the same way, keyed on the same ``gnb_id``/``cell_id`` pair
    the cell records use.
    """
    ues = []
    for item in items:
        ran = item.get("gnb") or item.get("enb") or {}
        location = item.get("location") or {}
        cgi = location.get("nr_cgi") or {}
        tai = location.get("nr_tai") or location.get("tai") or {}
        supi = str(item.get("supi") or "").strip()
        ues.append({
            "supi": supi,
            "pei": str(item.get("pei") or "").strip(),
            "state": str(item.get("cm_state") or "").strip().lower(),
            "technology": "lte" if item.get("enb") else "nr",
            "gnb_id": _int(cgi.get("gnb_id")) if cgi else _int(ran.get("gnb_id")),
            "enb_id": _int(ran.get("enb_id")),
            "cell_id": _int(cgi.get("cell_id")) if cgi else _int(ran.get("cell_id")),
            "nci": _int(cgi.get("nci")),
            "plmn": str(cgi.get("plmn") or tai.get("plmn") or ""),
            "tac": str(tai.get("tac_hex") or tai.get("tac") or ""),
            "sessions": _int(item.get("pdu_sessions_count"))
            or _int(item.get("pdn_count")) or 0,
            "dnn": _first_dnn(item),
        })
    return ues


def _first_dnn(item: dict) -> str:
    for sess in item.get("pdu_sessions") or item.get("pdn") or []:
        if not isinstance(sess, dict):
            continue
        name = sess.get("dnn") or sess.get("apn")
        if name:
            return str(name)
    return ""


def sessions_from_pdu_info(items: list[dict]) -> dict:
    """SUPI -> the session facts the AMF/MME does not hold.

    The UE's IP address is assigned by the SMF, so ``/ue-info`` cannot report
    it and ``/pdu-info`` can. Joining the two on SUPI is what gives a client an
    address in the dashboard, the same as a Wi-Fi client has.
    """
    out: dict = {}
    for item in items:
        supi = str(item.get("supi") or "").strip()
        if not supi:
            continue
        ipv4, dnn, state = "", "", ""
        for pdu in item.get("pdu") or []:
            if not isinstance(pdu, dict):
                continue
            ipv4 = ipv4 or str(pdu.get("ipv4") or "")
            dnn = dnn or str(pdu.get("dnn") or pdu.get("apn") or "")
            state = state or str(pdu.get("pdu_state") or "")
        out[_bare_supi(supi)] = {
            "ip": ipv4,
            "dnn": dnn,
            "pdu_state": state,
            "activity": str(item.get("ue_activity") or ""),
        }
    return out


def _bare_supi(supi: str) -> str:
    """``imsi-001010000056492`` and ``001010000056492`` are the same subscriber.
    The AMF prefixes, the MME does not, and /pdu-info follows whichever core
    created the session — so every join on SUPI goes through here."""
    value = supi.strip().lower()
    for prefix in ("imsi-", "nai-", "supi-"):
        if value.startswith(prefix):
            return value[len(prefix):]
    return value


def _int(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
