"""``python -m unifi_hamina_live`` / ``unifi-hamina-live`` entry point."""

from __future__ import annotations

import errno
import os
import socket
import sys

import uvicorn

from .config import get_settings

# What the image EXPOSEs, what its HEALTHCHECK polls, and what compose maps
# HOST_PORT onto: `${HOST_PORT:-8080}:8080`.
CONTAINER_PORT = 8080


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


def in_container() -> bool:
    """Best-effort: are we running inside a container?

    Only used to decide whether a warning applies, so a wrong answer costs a
    missing hint or a spurious one, never behaviour.
    """
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup", encoding="utf-8") as fh:
            blob = fh.read()
    except OSError:
        return False
    return any(k in blob for k in ("docker", "containerd", "kubepods"))


def port_mapping_warning(port: int, inside=None) -> str | None:
    """Warn when PORT is changed inside a container, which silently breaks it.

    PORT is what the app listens on INSIDE the container; HOST_PORT is what
    Docker publishes, mapped to the container's 8080. Setting PORT to move a
    clashing host port leaves compose mapping to 8080 with nothing on it: the
    container starts, logs a clean startup, serves nobody, and the HEALTHCHECK
    — which polls 8080 — fails it into a restart loop.

    Nothing else catches this. The bind succeeds, because the port really is
    free inside the container; it is the mapping that is wrong, and a mapping
    is not visible from in here. Hence a warning rather than a refusal: a
    hand-written compose CAN publish to a different container port, and that is
    a legitimate setup this must not block.
    """
    inside = in_container() if inside is None else inside
    if not inside or port == CONTAINER_PORT:
        return None
    return (
        f"PORT={port} inside a container. The image publishes "
        f"{CONTAINER_PORT}, and docker-compose maps HOST_PORT to "
        f"{CONTAINER_PORT} — so nothing will be listening where Docker "
        f"forwards, and the healthcheck (which polls {CONTAINER_PORT}) will "
        f"fail. To move the port on the HOST, leave PORT alone and set "
        f"HOST_PORT. Ignore this only if your compose publishes to "
        f"{port} deliberately."
    )


def main() -> None:
    settings = get_settings()
    mapping = port_mapping_warning(settings.port)
    if mapping:
        # Before the bind check, because the bind will SUCCEED and hide this.
        print(f"WARNING: {mapping}", file=sys.stderr)
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
