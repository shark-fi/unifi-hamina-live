"""Meraki-compatible facade: auth, shapes, and UniFi->Meraki mapping."""

AUTH = {"X-Cisco-Meraki-API-Key": "test-key"}


def test_requires_api_key(client):
    assert client.get("/api/v1/organizations").status_code == 401
    r = client.get("/api/v1/organizations", headers=AUTH)
    assert r.status_code == 200


def test_bearer_auth_accepted(client):
    r = client.get("/api/v1/organizations", headers={"Authorization": "Bearer test-key"})
    assert r.status_code == 200


def test_organizations_and_networks(client):
    orgs = client.get("/api/v1/organizations", headers=AUTH).json()
    assert len(orgs) == 1
    org_id = orgs[0]["id"]
    assert org_id == "O_UniFi"

    nets = client.get(f"/api/v1/organizations/{org_id}/networks", headers=AUTH).json()
    ids = {n["id"] for n in nets}
    assert ids == {"N_default", "N_site2"}
    assert all(n["productTypes"] == ["wireless"] for n in nets)


def test_org_devices_and_statuses(client):
    devs = client.get("/api/v1/organizations/O_UniFi/devices", headers=AUTH).json()
    # two APs total (switch excluded)
    assert len(devs) == 2
    d = next(x for x in devs if x["name"] == "AP-Lobby")
    assert d["productType"] == "wireless"
    assert d["model"].startswith("MR")  # mapped to a Meraki model
    assert "U7PRO" in d["notes"]
    assert d["serial"] == d["serial"].upper()

    statuses = client.get(
        "/api/v1/organizations/O_UniFi/devices/statuses", headers=AUTH
    ).json()
    by_name = {s["name"]: s for s in statuses}
    assert by_name["AP-Lobby"]["status"] == "online"
    assert by_name["AP-Warehouse"]["status"] == "offline"
    assert by_name["AP-Lobby"]["lastReportedAt"].endswith("Z")


def test_radio_settings_shape(client):
    devs = client.get("/api/v1/networks/N_default/devices", headers=AUTH).json()
    serial = next(d["serial"] for d in devs if d["name"] == "AP-Lobby")
    rs = client.get(
        f"/api/v1/devices/{serial}/wireless/radio/settings", headers=AUTH
    ).json()
    assert rs["fiveGhzSettings"]["channel"] == 36
    assert rs["fiveGhzSettings"]["channelWidth"] == 80
    assert rs["fiveGhzSettings"]["targetPower"] == 20
    assert rs["twoFourGhzSettings"]["channel"] == 6
    assert rs["sixGhzSettings"]["channel"] == 37


def test_network_and_device_clients(client):
    net_clients = client.get("/api/v1/networks/N_default/clients", headers=AUTH).json()
    assert len(net_clients) == 2
    assert all(c["status"] == "Online" for c in net_clients)

    devs = client.get("/api/v1/networks/N_default/devices", headers=AUTH).json()
    serial = next(d["serial"] for d in devs if d["name"] == "AP-Lobby")
    dev_clients = client.get(f"/api/v1/devices/{serial}/clients", headers=AUTH).json()
    assert len(dev_clients) == 2
    assert dev_clients[0]["recentDeviceSerial"] == serial


def test_unknown_org_and_network_404(client):
    assert client.get("/api/v1/organizations/O_Nope", headers=AUTH).status_code == 404
    assert client.get("/api/v1/networks/N_nope/devices", headers=AUTH).status_code == 404


def test_floorplans_empty_but_valid(client):
    r = client.get("/api/v1/networks/N_default/floorPlans", headers=AUTH)
    assert r.status_code == 200 and r.json() == []
