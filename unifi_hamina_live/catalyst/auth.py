"""DNA Center auth: Basic-auth token issuance + X-Auth-Token validation."""

from __future__ import annotations

import base64
import secrets
import time


class TokenStore:
    """In-memory issued-token store with a TTL. Single-process; tokens are
    forgotten on restart (Hamina simply re-authenticates)."""

    def __init__(self, ttl: float = 3600.0) -> None:
        self._ttl = ttl
        self._tokens: dict[str, float] = {}

    def issue(self) -> str:
        self._prune()
        tok = secrets.token_urlsafe(32)
        self._tokens[tok] = time.time()
        return tok

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        issued = self._tokens.get(token)
        if issued is None:
            return False
        if time.time() - issued > self._ttl:
            self._tokens.pop(token, None)
            return False
        return True

    def _prune(self) -> None:
        now = time.time()
        for tok, ts in list(self._tokens.items()):
            if now - ts > self._ttl:
                self._tokens.pop(tok, None)


def check_basic(header: str | None, username: str, password: str) -> bool:
    """Validate an ``Authorization: Basic`` header against configured creds.

    If no username is configured the facade runs in dev mode and accepts any
    non-empty username (still flagged in the docs).
    """
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        raw = base64.b64decode(header.split(" ", 1)[1]).decode()
    except Exception:
        return False
    user, _, pw = raw.partition(":")
    if not username:
        return bool(user)
    return user == username and pw == password
