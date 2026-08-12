"""Vendor-neutral REST API and the live dashboard."""


def test_health(client):
    h = client.get("/api/health").json()
    assert h["ok"] is True
    assert h["access_points"] == 2
    assert h["clients"] == 2
    assert h["sites"] == 2


def test_sites(client):
    sites = client.get("/api/sites").json()
    ids = {s["id"] for s in sites}
    assert ids == {"default", "site2"}
    hq = next(s for s in sites if s["id"] == "default")
    assert hq["name"] == "HQ" and hq["num_aps"] == 2


def test_access_points_filter_by_site(client):
    aps = client.get("/api/access-points", params={"site": "default"}).json()
    assert len(aps) == 2
    assert {a["name"] for a in aps} == {"AP-Lobby", "AP-Warehouse"}


def test_clients_filter_by_ap(client):
    aps = client.get("/api/access-points").json()
    serial = next(a["serial"] for a in aps if a["name"] == "AP-Lobby")
    clients = client.get("/api/clients", params={"ap_serial": serial}).json()
    assert len(clients) == 2
    assert all(c["ap_serial"] == serial for c in clients)


def test_summary_has_per_ap_client_counts(client):
    s = client.get("/api/summary").json()
    lobby = next(a for a in s["access_points"] if a["name"] == "AP-Lobby")
    assert lobby["num_clients"] == 3
    assert {r["band"] for r in lobby["radios"]} == {"2.4", "5", "6"}


def test_refresh_endpoint(client):
    r = client.post("/api/refresh").json()
    assert r["ok"] is True


def test_dashboard_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "UniFi Live" in r.text


def test_openintent_disabled_returns_404(client):
    assert client.get("/openintent/status").status_code == 404


def test_version_endpoint_reports_the_build():
    """A build that cannot say what it is makes every diagnosis provisional.

    Every "the fix didn't work" report against this project has begun with a
    container that predated the fix — and answering that meant exec'ing in and
    inferring from behaviour. `sha` is stamped by CI at image build time; a
    local build reports "unknown", so `code` carries the answer there.
    """
    import os

    from fastapi.testclient import TestClient
    from unifi_hamina_live.app import create_app
    from unifi_hamina_live.config import Settings
    from tests.conftest import FakeCollector, build_snapshot

    app = create_app(settings=Settings(),
                     collector=FakeCollector(build_snapshot()))
    with TestClient(app) as c:
        body = c.get("/version").json()
    assert body["name"] == "unifi-hamina-live"
    for key in ("version", "sha", "code", "built_at", "python"):
        assert key in body and body[key], key

    # stamped from the environment, so an image can report its own commit
    os.environ["BUILD_SHA"] = "deadbee"
    try:
        app2 = create_app(settings=Settings(),
                          collector=FakeCollector(build_snapshot()))
        with TestClient(app2) as c:
            assert c.get("/version").json()["sha"] == "deadbee"
    finally:
        os.environ.pop("BUILD_SHA", None)


def test_version_is_not_in_the_request_capture():
    """The capture answers "what did the client call". /version is ours."""
    from unifi_hamina_live.config import Settings
    from unifi_hamina_live.app import create_app
    from fastapi.testclient import TestClient
    from tests.conftest import FakeCollector, build_snapshot

    app = create_app(settings=Settings(catalyst_enabled=True,
                                       catalyst_username="hamina",
                                       catalyst_password="secret"),
                     collector=FakeCollector(build_snapshot()))
    with TestClient(app) as c:
        c.get("/version")
        paths = [r["path"] for r in c.get("/catalyst/_captured").json()["requests"]]
    assert "/version" not in paths



def test_version_identifies_a_locally_built_image():
    """`sha` is CI-only, and the runbook tells people to build locally.

    Reporting "unknown" for every local build left /version unable to answer the
    one question it exists for — "am I running the fix?" — in the case that
    needed it most. Hashing the loaded source needs no build arg and no git
    metadata in the image.
    """
    import hashlib
    from pathlib import Path

    from fastapi.testclient import TestClient
    import unifi_hamina_live
    from unifi_hamina_live.app import create_app
    from unifi_hamina_live.config import Settings
    from tests.conftest import FakeCollector, build_snapshot

    app = create_app(settings=Settings(_env_file=None),
                     collector=FakeCollector(build_snapshot()))
    with TestClient(app) as c:
        code = c.get("/version").json()["code"]

    # only useful if you can reproduce it against a checkout and compare
    root = Path(unifi_hamina_live.__file__).resolve().parent
    h = hashlib.sha256()
    for f in sorted(root.rglob("*.py")):
        h.update(f.read_bytes())
    assert code == h.hexdigest()[:12]

    # and it must not be confusable with the git sha beside it
    assert len(code) == 12
