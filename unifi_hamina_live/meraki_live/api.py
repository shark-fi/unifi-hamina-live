"""A small async Meraki Dashboard API v1 client — GETs only.

Two things about the Dashboard API that bite a generic client:

* Array parameters are spelled with brackets (``productTypes[]=wireless``); a
  bare repeated key is rejected with 400 "'productTypes' must be an array".
* It rate-limits per organization (about 10 requests a second) with 429 and a
  ``Retry-After`` header, so a 429 waits and retries once rather than failing
  the poll.
"""

from __future__ import annotations

import asyncio

import httpx

DEFAULT_BASE = "https://api.meraki.com/api/v1"


class MerakiError(RuntimeError):
    pass


class MerakiAuthError(MerakiError):
    pass


class MerakiUnreachableError(MerakiError):
    pass


def encode_params(params: dict) -> list[tuple[str, str]]:
    """Query pairs, with list values spelled the Dashboard way (``key[]``)."""
    out: list[tuple[str, str]] = []
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            out += [(f"{key}[]", str(v)) for v in value]
        else:
            out.append((key, str(value)))
    return out


class MerakiClient:
    def __init__(self, api_key: str, *, base_url: str = DEFAULT_BASE,
                 timeout: float = 10.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout, transport=transport,
            headers={"Authorization": f"Bearer {api_key}",
                     "Accept": "application/json"})

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get(self, path: str, **params):
        for attempt in (1, 2):
            try:
                r = await self._http.get(path, params=encode_params(params))
            except httpx.HTTPError as exc:
                raise MerakiUnreachableError(f"{path}: {exc!r}") from exc
            if r.status_code == 429 and attempt == 1:
                await asyncio.sleep(min(float(r.headers.get("Retry-After", 1)), 5.0))
                continue
            break
        if r.status_code in (401, 403):
            # 403 is also what an org with lapsed licences answers with
            raise MerakiAuthError(f"{path}: HTTP {r.status_code} {r.text[:200]}")
        if r.status_code >= 400:
            raise MerakiError(f"{path}: HTTP {r.status_code} {r.text[:200]}")
        return r.json() if r.content else None
