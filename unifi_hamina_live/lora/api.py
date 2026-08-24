"""A tiny async client for the MikroTik RouterOS binary API.

The wAP LR8 gateway runs RouterOS 6, which has no REST API — the web UI's
``/rest`` returns 404 — so the honest way in is the binary API on TCP 8728, the
same protocol Winbox and ``librouteros`` speak. It is small enough to hand-roll
here, exactly as the HaLow side hand-rolls its ``ubus`` bridge, and hand-rolling
keeps this project's dependency list (httpx and FastAPI) untouched.

The protocol is a stream of *sentences*; each sentence is a list of
length-prefixed *words* terminated by a zero-length word. A command is one
sentence (``/lora/print`` plus optional ``=key=value`` attributes); the reply is
a run of ``!re`` sentences (one per row) closed by ``!done``. ``!trap`` carries
an application error (bad command, permission denied) and ``!fatal`` means the
connection is being torn down. Login on 6.43+ is a single ``/login`` with the
password in the clear over the wire; the pre-6.43 MD5 challenge is kept as a
fallback so this works against an older box too.

Read-only by intent: this integration only ever issues ``print`` commands, the
same way the cellular side only reads a core and the HaLow side only reads
``iwinfo``. One connection is opened, logged in, and reused across a poll's
handful of commands; a dropped connection is re-established once and the command
retried, so a box that closed an idle session mid-poll does not read as an error.
"""

from __future__ import annotations

import asyncio
import hashlib


class RouterOSError(RuntimeError):
    """A RouterOS API command returned a ``!trap`` — a real application error."""


class RouterOSAuthError(RouterOSError):
    """Login was refused — wrong username/password."""


class RouterOSUnreachableError(RouterOSError):
    """The box did not answer at all — DNS, routing, refused, timed out.

    Kept apart from an auth failure for the same reason the UniFi and HaLow
    clients keep them apart: a box that is simply absent is not counting failed
    logins, so there is nothing to back off from; one rejecting credentials
    might be.
    """


def _encode_length(n: int) -> bytes:
    """RouterOS word-length prefix: a variable-width big-endian integer whose
    high bits announce its own width."""
    if n < 0x80:
        return bytes([n])
    if n < 0x4000:
        return (n | 0x8000).to_bytes(2, "big")
    if n < 0x200000:
        return (n | 0xC00000).to_bytes(3, "big")
    if n < 0x10000000:
        return (n | 0xE0000000).to_bytes(4, "big")
    return b"\xF0" + n.to_bytes(4, "big")


class RouterOSClient:
    """One RouterOS box's API endpoint, holding a connection across commands."""

    def __init__(self, host: str, username: str, password: str,
                 *, port: int = 8728, timeout: float = 5.0) -> None:
        self._host = host.strip()
        self._port = port
        self._username = username
        self._password = password
        self._timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def aclose(self) -> None:
        await self._disconnect()

    async def _disconnect(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, asyncio.TimeoutError):
                pass  # already gone; nothing to flush

    # -- the wire ----------------------------------------------------------
    async def _connect(self) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port), self._timeout)
        except (OSError, asyncio.TimeoutError) as exc:
            raise RouterOSUnreachableError(
                f"{self._host}:{self._port}: {exc}") from exc

    async def _send(self, words: list[str]) -> None:
        assert self._writer is not None
        buf = bytearray()
        for word in words:
            wb = word.encode()
            buf += _encode_length(len(wb)) + wb
        buf += b"\x00"
        self._writer.write(bytes(buf))
        try:
            await asyncio.wait_for(self._writer.drain(), self._timeout)
        except (OSError, asyncio.TimeoutError) as exc:
            raise RouterOSUnreachableError(
                f"{self._host}:{self._port}: {exc}") from exc

    async def _read_length(self) -> int:
        b = (await self._readexactly(1))[0]
        if b & 0x80 == 0:
            return b
        if b & 0xC0 == 0x80:
            return ((b & 0x3F) << 8) | (await self._readexactly(1))[0]
        if b & 0xE0 == 0xC0:
            x = await self._readexactly(2)
            return ((b & 0x1F) << 16) | (x[0] << 8) | x[1]
        if b & 0xF0 == 0xE0:
            x = await self._readexactly(3)
            return ((b & 0x0F) << 24) | (x[0] << 16) | (x[1] << 8) | x[2]
        x = await self._readexactly(4)
        return int.from_bytes(x, "big")

    async def _readexactly(self, n: int) -> bytes:
        assert self._reader is not None
        try:
            return await asyncio.wait_for(
                self._reader.readexactly(n), self._timeout)
        except (asyncio.IncompleteReadError, OSError, asyncio.TimeoutError) as exc:
            raise RouterOSUnreachableError(
                f"{self._host}:{self._port}: {exc}") from exc

    async def _read_sentence(self) -> list[str]:
        words: list[str] = []
        while True:
            n = await self._read_length()
            if n == 0:
                return words
            words.append((await self._readexactly(n)).decode(errors="replace"))

    async def _read_reply(self) -> list[dict[str, str]]:
        """Collect ``!re`` rows until ``!done``; raise on ``!trap``/``!fatal``."""
        rows: list[dict[str, str]] = []
        trap: str | None = None
        while True:
            sentence = await self._read_sentence()
            if not sentence:
                continue
            tag, attrs = sentence[0], _attrs(sentence[1:])
            if tag == "!re":
                rows.append(attrs)
            elif tag == "!done":
                if attrs:
                    rows.append(attrs)  # e.g. login's =ret= challenge
                break
            elif tag == "!trap":
                trap = attrs.get("message", "command failed")
            elif tag == "!fatal":
                await self._disconnect()
                raise RouterOSUnreachableError(
                    f"{self._host}:{self._port}: {attrs.get('message', 'fatal')}")
        if trap is not None:
            raise RouterOSError(trap)
        return rows

    async def _talk(self, words: list[str]) -> list[dict[str, str]]:
        await self._send(words)
        return await self._read_reply()

    # -- login -------------------------------------------------------------
    async def login(self) -> None:
        """Establish a connection and authenticate.

        Tries the 6.43+ plain login first; if the box answers with a ``=ret=``
        challenge (pre-6.43) the MD5 response is computed and sent.
        """
        await self._connect()
        try:
            rows = await self._talk(
                ["/login", "=name=" + self._username,
                 "=password=" + self._password])
        except RouterOSError as exc:
            await self._disconnect()
            raise RouterOSAuthError(
                "login refused (check LORA_USERNAME / LORA_PASSWORD): "
                f"{exc}") from exc
        challenge = next(
            (r["ret"] for r in rows if "ret" in r), None)
        if challenge is not None:
            await self._login_challenge(challenge)

    async def _login_challenge(self, challenge_hex: str) -> None:
        digest = hashlib.md5(
            b"\x00" + self._password.encode()
            + bytes.fromhex(challenge_hex)).hexdigest()
        try:
            await self._talk(
                ["/login", "=name=" + self._username,
                 "=response=00" + digest])
        except RouterOSError as exc:
            await self._disconnect()
            raise RouterOSAuthError(
                "login refused (check LORA_USERNAME / LORA_PASSWORD): "
                f"{exc}") from exc

    # -- commands ----------------------------------------------------------
    async def command(self, path: str, **query: str) -> list[dict[str, str]]:
        """Run a ``print``-style command, returning one dict per ``!re`` row.

        Ensures a logged-in connection, reconnecting once if the box dropped an
        idle session between polls — a lapsed connection is a transport quirk,
        not a failure worth surfacing, the same way the HaLow client renews a
        lapsed ``ubus`` session and retries.
        """
        words = [path] + [f"={k.replace('_', '-')}={v}" for k, v in query.items()]
        if self._writer is None:
            await self.login()
        try:
            return await self._talk(words)
        except RouterOSUnreachableError:
            await self._disconnect()
            await self.login()
            return await self._talk(words)


def _attrs(words: list[str]) -> dict[str, str]:
    """``=key=value`` words -> a dict. A value may itself contain ``=``."""
    out: dict[str, str] = {}
    for word in words:
        if word.startswith("="):
            key, _, value = word[1:].partition("=")
            out[key] = value
    return out
