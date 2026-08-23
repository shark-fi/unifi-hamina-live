"""A tiny async client for an OpenWrt ``ubus`` JSON-RPC endpoint.

The Alfa HaLow radios run OpenWrt (MatrixPro), and the web UI talks to the box
over ``POST /ubus`` with the ``uhttpd-mod-ubus`` JSON-RPC bridge. This is the
same transport the UI's own ``$ubus.call`` uses, reduced to the two verbs this
integration needs: log in for a session token, and call a method.

Session handling is the one subtlety. A ``ubus`` session expires (300 s here),
and an expired one answers not with an application error but with a *transport*
error — JSON-RPC ``-32002 "Access denied"`` — which is easy to mistake for the
method being unavailable. So a call that hits that re-logs-in once and retries,
and only then gives up. Every method reply is itself ``[rc, data]``: ``rc == 0``
is success, and a non-zero ``rc`` (notably ``6``, permission denied) is a real
``ubus`` status, distinct from the transport layer refusing the session.

Read-only by intent: this integration only ever calls ``session.login`` and the
``iwinfo`` / ``uci get`` readers, exactly as the cellular side only reads a core.
"""

from __future__ import annotations

import httpx

# The all-zero session id ubus accepts for the unauthenticated `login` call.
_ANON_SESSION = "00000000000000000000000000000000"

# JSON-RPC transport code for "your session is not valid" — as opposed to a
# method-level `rc`. uhttpd-mod-ubus answers an expired/*wrong* session with
# this at the JSON-RPC layer, before the call is ever dispatched.
_ACCESS_DENIED = -32002

# ubus status code UBUS_STATUS_PERMISSION_DENIED, returned *as* an `rc` when the
# session is valid but its ACL does not grant the method.
UBUS_PERMISSION_DENIED = 6


class UbusError(RuntimeError):
    """A ubus call did not return successfully."""


class UbusAuthError(UbusError):
    """Login was refused — wrong username/password."""


class UbusUnreachableError(UbusError):
    """The radio did not answer at all — DNS, routing, refused, timed out.

    Kept apart from an auth failure for the same reason the UniFi client keeps
    them apart: a box that is simply absent is not counting failed logins, so
    there is nothing to back off from; a box rejecting credentials might be.
    """


class UbusClient:
    """One radio's ``ubus`` endpoint, holding a session across calls."""

    def __init__(self, host: str, username: str, password: str,
                 *, timeout: float = 5.0, verify_tls: bool = False) -> None:
        # A bare host becomes https — these boxes serve the UI on https and
        # redirect http, and https is the one both ports answer.
        base = host.strip().rstrip("/")
        if not base.startswith(("http://", "https://")):
            base = "https://" + base
        self._url = base + "/ubus"
        self._username = username
        self._password = password
        self._client = httpx.AsyncClient(timeout=timeout, verify=verify_tls)
        self._session: str | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _rpc(self, method: str, params: list) -> dict:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            resp = await self._client.post(self._url, json=payload)
        except httpx.HTTPError as exc:
            raise UbusUnreachableError(f"{self._url}: {exc}") from exc
        if resp.status_code != 200:
            raise UbusError(f"HTTP {resp.status_code} from {self._url}")
        try:
            return resp.json()
        except ValueError as exc:
            raise UbusError(f"non-JSON reply from {self._url}") from exc

    async def login(self) -> str:
        """Establish a session, returning its token."""
        body = await self._rpc("call", [
            _ANON_SESSION, "session", "login",
            {"username": self._username, "password": self._password},
        ])
        if "error" in body:
            raise UbusError(str(body["error"]))
        result = body.get("result")
        # A refused login is `[6]` (permission denied) with no session object;
        # a good one is `[0, {"ubus_rpc_session": ...}]`.
        if not isinstance(result, list) or not result or result[0] != 0:
            raise UbusAuthError(
                "login refused (check HALOW_USERNAME / HALOW_PASSWORD)")
        session = result[1].get("ubus_rpc_session") if len(result) > 1 else None
        if not session:
            raise UbusAuthError("login returned no session token")
        self._session = session
        return session

    async def call(self, obj: str, method: str, args: dict | None = None) -> dict:
        """Call ``obj.method`` with ``args``, re-logging-in once if the session
        has expired. Returns the method's ``data`` object (the second element of
        the ``[rc, data]`` result)."""
        if self._session is None:
            await self.login()
        body = await self._call_once(obj, method, args or {})
        # A transport-level "Access denied" means the session lapsed. Renew it
        # once and retry; a second refusal is a real permission problem.
        if self._is_session_error(body):
            await self.login()
            body = await self._call_once(obj, method, args or {})

        if "error" in body:
            raise UbusError(f"{obj}.{method}: {body['error']}")
        result = body.get("result")
        if not isinstance(result, list) or not result:
            raise UbusError(f"{obj}.{method}: malformed result {result!r}")
        rc = result[0]
        if rc != 0:
            if rc == UBUS_PERMISSION_DENIED:
                raise UbusError(
                    f"{obj}.{method}: permission denied — the account's ACL "
                    f"does not grant it")
            raise UbusError(f"{obj}.{method}: ubus rc {rc}")
        return result[1] if len(result) > 1 else {}

    async def _call_once(self, obj: str, method: str, args: dict) -> dict:
        assert self._session is not None
        return await self._rpc("call", [self._session, obj, method, args])

    @staticmethod
    def _is_session_error(body: dict) -> bool:
        err = body.get("error")
        return isinstance(err, dict) and err.get("code") == _ACCESS_DENIED
