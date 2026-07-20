"""Shared fixtures: realistic UniFi payloads and a fake collector."""

from __future__ import annotations

import time

import pytest

from unifi_hamina_live.config import Settings
from unifi_hamina_live.unifi import normalize
from unifi_hamina_live.models import Snapshot


# --- raw UniFi payload samples (trimmed to fields we consume) --------------
SITES_RAW = [
    {"name": "default", "desc": "HQ"},
    {"name": "site2", "desc": "Warehouse"},
]

DEVICES_RAW = {
    "default": [
        {
            "type": "uap", "name": "AP-Lobby", "mac": "aa:bb:cc:00:11:22",
            "model": "U7PRO", "ip": "192.168.1.20", "state": 1,
            "uptime": 123456, "version": "6.6.77", "user-num_sta": 3,
            "radio_table": [
                {"radio": "ng", "channel": 6, "ht": 20},
                {"radio": "na", "channel": 36, "ht": 80},
                {"radio": "6e", "channel": 37, "ht": 160},
            ],
            "radio_table_stats": [
                {"radio": "ng", "channel": 6, "tx_power": 15, "num_sta": 1, "cu_total": 22},
                {"radio": "na", "channel": 36, "tx_power": 20, "num_sta": 2, "cu_total": 41},
                {"radio": "6e", "channel": 37, "tx_power": 18, "num_sta": 0},
            ],
        },
        {
            # a switch — must be ignored (type != uap)
            "type": "usw", "name": "SW-Core", "mac": "aa:bb:cc:00:33:44",
            "model": "US48", "state": 1,
        },
        {
            "type": "uap", "name": "AP-Warehouse", "mac": "aa:bb:cc:00:55:66",
            "model": "U6PRO", "ip": "192.168.1.21", "state": 0,
            "radio_table_stats": [
                {"radio": "ng", "channel": 1, "tx_power": 14, "num_sta": 0},
            ],
        },
    ],
    "site2": [],
}

CLIENTS_RAW = {
    "default": [
        {
            "mac": "de:ad:be:ef:00:01", "hostname": "laptop", "ip": "192.168.1.50",
            "ap_mac": "aa:bb:cc:00:11:22", "essid": "Corp", "radio": "na",
            "channel": 36, "rssi": 45, "signal": -62, "noise": -95,
            "tx_rate": 866000, "rx_rate": 780000, "tx_bytes": 1000, "rx_bytes": 2000,
            "uptime": 3600,
        },
        {
            "mac": "de:ad:be:ef:00:02", "hostname": "phone",
            "ap_mac": "aa:bb:cc:00:11:22", "essid": "Corp", "radio": "ng",
            "channel": 6, "signal": -70,
        },
        {
            # wired client — must be filtered out
            "mac": "de:ad:be:ef:00:03", "is_wired": True, "ip": "192.168.1.9",
        },
    ],
    "site2": [],
}


def build_snapshot() -> Snapshot:
    aps, clients, sites = [], [], []
    from unifi_hamina_live.models import Site

    for site in SITES_RAW:
        sid = site["name"]
        site_aps = [
            normalize.access_point(d, sid)
            for d in DEVICES_RAW.get(sid, [])
            if normalize.is_access_point(d)
        ]
        serial_by_mac = {a.mac: a.serial for a in site_aps}
        stas = normalize.wireless_clients_only(CLIENTS_RAW.get(sid, []))
        site_clients = [normalize.client(s, sid, serial_by_mac) for s in stas]
        aps.extend(site_aps)
        clients.extend(site_clients)
        sites.append(
            Site(id=sid, name=site["desc"], num_aps=len(site_aps),
                 num_clients=len(site_clients))
        )
    return Snapshot(
        generated_at=time.time(), ok=True, sites=sites,
        access_points=aps, clients=clients,
    )


class FakeCollector:
    """Stands in for the real Collector — no network, fixed snapshot."""

    def __init__(self, snapshot: Snapshot) -> None:
        self._snapshot = snapshot

    @property
    def snapshot(self) -> Snapshot:
        return self._snapshot

    def start(self) -> None:  # no-op
        pass

    async def stop(self) -> None:  # no-op
        pass

    async def poll_once(self) -> Snapshot:
        return self._snapshot


@pytest.fixture
def settings() -> Settings:
    return Settings(
        meraki_compat_api_key="test-key",
        meraki_org_name="UniFi",
        openintent_refresh_enabled=False,
    )


@pytest.fixture
def snapshot() -> Snapshot:
    return build_snapshot()


@pytest.fixture
def client(settings, snapshot):
    """A TestClient wired to a FakeCollector holding the sample snapshot."""
    from fastapi.testclient import TestClient

    from unifi_hamina_live.app import create_app

    app = create_app(settings=settings, collector=FakeCollector(snapshot))
    with TestClient(app) as c:
        yield c
