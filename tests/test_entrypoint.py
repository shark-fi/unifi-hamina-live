"""Refusing to start, rather than starting and then dying.

Uvicorn runs lifespan startup BEFORE it binds, so a port clash used to start
the collector, log "Application startup complete", poll the console, and only
then fail on the bind. Under restart:unless-stopped that repeated forever — one
host did it 24 times, with the single line that mattered buried under 24
successful-looking startups.
"""
import errno
import socket

from unifi_hamina_live.__main__ import check_port_free


def _taken_port():
    """A bound port, and the socket keeping it that way."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s, s.getsockname()[1]


def test_a_free_port_is_reported_free():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert check_port_free("127.0.0.1", port) is None


def test_a_taken_port_names_the_clash_and_the_setting_that_moves_it():
    sock, port = _taken_port()
    try:
        why = check_port_free("127.0.0.1", port)
        assert why is not None, "a bound port must not report as free"
        assert "already in use" in why
        assert "HOST_PORT" in why, "say which knob moves it, not just that it failed"
    finally:
        sock.close()


def test_an_address_this_machine_does_not_have_is_named_as_such():
    """A wrong HOST reads as a port problem otherwise."""
    why = check_port_free("192.0.2.123", 8080)     # TEST-NET-1, never local
    assert why is not None
    assert "not an address on this machine" in why or "cannot bind" in why


def test_the_check_releases_the_port_it_tested():
    """It must not become the thing occupying the port it just cleared."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert check_port_free("127.0.0.1", port) is None
    again = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        again.bind(("127.0.0.1", port))          # would raise if still held
    finally:
        again.close()


def test_main_exits_before_starting_anything_when_the_port_is_taken(monkeypatch):
    """The whole point: no collector, no uvicorn, one message."""
    import unifi_hamina_live.__main__ as entry

    started = []
    monkeypatch.setattr(entry.uvicorn, "run",
                        lambda *a, **k: started.append(True))
    monkeypatch.setattr(entry, "check_port_free", lambda h, p: "port 8080 is busy")
    try:
        entry.main()
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("should have exited")
    assert not started, "uvicorn must never be reached"
