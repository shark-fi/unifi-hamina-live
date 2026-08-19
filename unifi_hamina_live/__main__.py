"""``python -m unifi_hamina_live`` / ``unifi-hamina-live`` entry point."""

from __future__ import annotations

import errno
import socket
import sys

import uvicorn

from .config import get_settings


def check_port_free(host: str, port: int) -> str | None:
    """None if the port can be bound, else why it cannot.

    Uvicorn runs lifespan startup BEFORE it creates the listening socket, so a
    port clash starts the collector, logs "Application startup complete", polls
    the console, and only then dies on the bind. Under restart:unless-stopped
    that repeats forever: the journal fills with poll warnings and successful
    startups, and the one line that matters — address already in use — is
    buried among them. Observed 24 times in a row on one host.

    Checking first costs one socket and turns that into a single sentence.
    """
    fam = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(fam, socket.SOCK_STREAM) as sock:
        # Deliberately NOT SO_REUSEADDR: the question is whether uvicorn will
        # be able to bind, and it does not set it either.
        try:
            sock.bind((host, port))
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                return (f"port {port} on {host} is already in use — something "
                        f"else is listening. Set HOST_PORT (Docker) or PORT to "
                        f"move this service.")
            if exc.errno in (errno.EACCES, errno.EPERM):
                return (f"not permitted to bind port {port} — ports below 1024 "
                        f"need root or CAP_NET_BIND_SERVICE.")
            if exc.errno in (errno.EADDRNOTAVAIL, errno.ENOENT):
                return (f"{host} is not an address on this machine — check HOST.")
            return f"cannot bind {host}:{port}: {exc}"
    return None


def main() -> None:
    settings = get_settings()
    problem = check_port_free(settings.host, settings.port)
    if problem:
        # Before uvicorn.run, so the collector never starts and the log says
        # this once instead of burying it under a successful-looking startup.
        print(f"ERROR: {problem}", file=sys.stderr)
        raise SystemExit(1)
    uvicorn.run(
        "unifi_hamina_live.app:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
