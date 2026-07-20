"""Async UniFi console client.

Handles both UniFi OS consoles (UDM/UDR/CloudKey Gen2 — login at
``/api/auth/login`` with an ``X-CSRF-Token`` handshake, Network app served under
``/proxy/network``) and classic software controllers (login at ``/api/login``,
no proxy prefix). Mirrors the auth flow of the companion ``unifi_export.py``.

Every call this client makes is a GET except the login POST — it is a
read-only telemetry reader.
"""

from __future__ import annotations

import httpx


class UniFiError(RuntimeError):
    pass


class UniFiAuthError(UniFiError):
    pass


class UniFiClient:
    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        verify_tls: bool = False,
        timeout: float = 30.0,
    ) -> None:
        self._base = host.rstrip("/")
        self._username = username
        self._password = password
        self._verify = verify_tls
        # Network app path prefix: "/proxy/network" on UniFi OS, "" on classic.
        self._prefix: str | None = None
        self._client = httpx.AsyncClient(
            base_url=self._base,
            verify=verify_tls,
            timeout=timeout,
            follow_redirects=True,
            headers={"Accept": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "UniFiClient":
        await self.login()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- auth --------------------------------------------------------------
    async def login(self) -> None:
        """Authenticate, discovering the console type. Idempotent-ish: safe to
        call again to refresh the session."""
        body = {"username": self._username, "password": self._password}
        # UniFi OS first.
        try:
            resp = await self._client.post("/api/auth/login", json=body)
        except httpx.HTTPError as exc:  # network / TLS
            raise UniFiError(f"cannot reach {self._base}: {exc}") from exc

        if resp.status_code < 400:
            self._capture_csrf(resp)
            self._prefix = "/proxy/network"
            return
        if resp.status_code in (401, 403):
            # Could be a classic controller (which 404/redirects that path), or
            # genuinely bad credentials on UniFi OS. Try classic before failing.
            classic = await self._try_classic_login(body)
            if classic:
                return
            raise UniFiAuthError(
                "login rejected. Use a LOCAL admin account (not a ui.com cloud "
                "account with MFA)."
            )
        # Non-auth error on the UniFi OS path — fall back to classic.
        if await self._try_classic_login(body):
            return
        raise UniFiError(f"login failed: HTTP {resp.status_code}")

    async def _try_classic_login(self, body: dict) -> bool:
        try:
            resp = await self._client.post("/api/login", json=body)
        except httpx.HTTPError:
            return False
        if resp.status_code < 400:
            self._capture_csrf(resp)
            self._prefix = ""
            return True
        return False

    def _capture_csrf(self, resp: httpx.Response) -> None:
        csrf = resp.headers.get("X-CSRF-Token") or resp.headers.get("x-csrf-token")
        if csrf:
            self._client.headers["X-CSRF-Token"] = csrf

    # -- requests ----------------------------------------------------------
    @property
    def _net(self) -> str:
        if self._prefix is None:
            raise UniFiError("not logged in — call login() first")
        return f"{self._prefix}/api"

    async def _get(self, path: str) -> dict:
        resp = await self._client.get(path)
        if resp.status_code in (401, 403):
            # Session expired — re-login once and retry.
            await self.login()
            resp = await self._client.get(path)
        # UniFi OS surfaces a fresh CSRF token on responses; keep it current.
        self._capture_csrf(resp)
        if resp.status_code >= 400:
            raise UniFiError(f"GET {path} -> HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise UniFiError(f"GET {path} -> non-JSON response") from exc

    @staticmethod
    def _data(body: dict) -> list:
        if isinstance(body, dict):
            data = body.get("data")
            return data if isinstance(data, list) else []
        return body if isinstance(body, list) else []

    # -- telemetry endpoints ----------------------------------------------
    async def sites(self) -> list[dict]:
        """Sites visible to the account. Each has 'name' (internal id) and
        'desc' (human label)."""
        return self._data(await self._get(f"{self._net}/self/sites"))

    async def devices(self, site: str) -> list[dict]:
        """Full per-device state incl. radio_table_stats, num_sta, uptime."""
        return self._data(await self._get(f"{self._net}/s/{site}/stat/device"))

    async def clients(self, site: str) -> list[dict]:
        """Active wireless/wired stations (stat/sta)."""
        return self._data(await self._get(f"{self._net}/s/{site}/stat/sta"))

    # -- placement sources -------------------------------------------------
    async def maps(self, site: str) -> dict:
        """Classic Maps floor plans, keyed by _id. {} on Network versions that
        dropped classic Maps. The collection name varies by version."""
        for coll in ("rest/map", "list/map", "stat/map"):
            try:
                data = self._data(await self._get(f"{self._net}/s/{site}/{coll}"))
            except UniFiError:
                continue
            if data:
                return {m["_id"]: m for m in data if m.get("_id")}
        return {}

    async def innerspace_project(self) -> dict | None:
        """The full InnerSpace project (floor plans + AP placement), or None if
        InnerSpace is not present on this console."""
        try:
            body = await self._get("/proxy/innerspace/api/project?mode=2D")
        except UniFiError:
            return None
        data = body.get("data") if isinstance(body, dict) else None
        return data if isinstance(data, dict) and "shapes" in data else None

    async def get_bytes(self, path: str) -> bytes | None:
        """Fetch raw bytes (e.g. a floor-plan image). Path may be absolute-on-host."""
        try:
            resp = await self._client.get(path)
        except httpx.HTTPError:
            return None
        return resp.content if resp.status_code < 400 else None

    # -- websocket event stream (experimental, undocumented) ---------------
    def events_ws_url(self, site: str) -> str:
        """wss:// URL for the controller event stream of a site."""
        if self._prefix is None:
            raise UniFiError("not logged in — call login() first")
        scheme = "wss" if self._base.lower().startswith("https") else "ws"
        host = self._base.split("://", 1)[-1]
        return f"{scheme}://{host}{self._prefix}/wss/s/{site}/events?clients=v2"

    def auth_headers(self) -> dict[str, str]:
        """Cookie + CSRF headers to authenticate a websocket handshake."""
        cookie = "; ".join(f"{c.name}={c.value}" for c in self._client.cookies.jar)
        headers = {}
        if cookie:
            headers["Cookie"] = cookie
        csrf = self._client.headers.get("X-CSRF-Token")
        if csrf:
            headers["X-CSRF-Token"] = csrf
        return headers

    @property
    def verify_tls(self) -> bool:
        return self._verify
