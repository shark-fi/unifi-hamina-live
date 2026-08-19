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


# --- PORT vs HOST_PORT -------------------------------------------------------

from unifi_hamina_live.__main__ import CONTAINER_PORT, port_mapping_warning


def test_changing_port_inside_a_container_warns():
    """The failure nothing else catches.

    Moving a clashing host port by setting PORT leaves compose mapping
    HOST_PORT to 8080 with nothing on it. The bind succeeds — the port really
    is free in there — the container starts cleanly, serves nobody, and the
    healthcheck fails it into a restart loop.
    """
    why = port_mapping_warning(8090, inside=True)
    assert why is not None
    assert "HOST_PORT" in why, "must name the setting that actually moves it"
    assert str(CONTAINER_PORT) in why
    assert "healthcheck" in why.lower()


def test_the_image_default_is_silent():
    assert port_mapping_warning(CONTAINER_PORT, inside=True) is None


def test_outside_a_container_any_port_is_fine():
    """Bare-metal has no mapping to disagree with."""
    assert port_mapping_warning(8090, inside=False) is None
    assert port_mapping_warning(9999, inside=False) is None


def test_it_warns_rather_than_refusing(monkeypatch, capsys):
    """A hand-written compose may publish to a different container port.

    That is legitimate, so this must not block it — the message says as much.
    """
    import unifi_hamina_live.__main__ as entry

    ran = []
    monkeypatch.setattr(entry.uvicorn, "run", lambda *a, **k: ran.append(True))
    monkeypatch.setattr(entry, "port_mapping_warning", lambda p: "mapping is odd")
    monkeypatch.setattr(entry, "check_port_free", lambda h, p: None)
    entry.main()
    assert ran, "a mapping warning must not stop the service"
    assert "mapping is odd" in capsys.readouterr().err
