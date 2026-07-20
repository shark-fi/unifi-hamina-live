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
