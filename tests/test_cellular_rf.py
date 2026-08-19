"""Carrier arithmetic, checked against the published 3GPP tables, and the
costume, checked for the only property it can have: stability."""

from __future__ import annotations

import pytest

from unifi_hamina_live.cellular import rf


@pytest.mark.parametrize("arfcn,mhz", [
    (0, 0.0),
    (100000, 500.0),          # FR1 lower range: 5 kHz raster
    (599999, 2999.995),       # last channel before the raster changes
    (600000, 3000.0),         # first of the 15 kHz raster
    (636667, 3550.005),       # n48 / CBRS lower edge
    (632628, 3489.42),        # a common n78 SSB point
    (2016667, 24250.08),      # first of the 60 kHz raster
])
def test_nr_arfcn_to_mhz(arfcn, mhz):
    assert rf.nr_arfcn_to_mhz(arfcn) == pytest.approx(mhz, abs=1e-3)


@pytest.mark.parametrize("earfcn,band,mhz", [
    (55240, 48, 3550.0),      # bottom of CBRS
    (56739, 48, 3699.9),      # top of CBRS
    (39650, 41, 2496.0),
    (600, 2, 1930.0),
    (5180, 13, 746.0),
    (68586, 71, 617.0),
])
def test_earfcn_to_mhz(earfcn, band, mhz):
    assert rf.earfcn_to_mhz(earfcn, band) == pytest.approx(mhz, abs=1e-3)


def test_earfcn_unknown_band_is_none_not_a_guess():
    # A band we have no row for must not silently produce a plausible number:
    # the caller falls back to asking for frequency_mhz.
    assert rf.earfcn_to_mhz(1000, 99) is None


@pytest.mark.parametrize("mhz,band", [
    (700.0, "2.4"), (1800.0, "2.4"), (2600.0, "2.4"),
    (3550.0, "5"), (3700.0, "5"), (5180.0, "5"),
    (6000.0, "6"), (26000.0, "6"),
])
def test_wifi_band_for(mhz, band):
    assert rf.wifi_band_for(mhz) == band


def test_costume_channel_is_stable_and_legal():
    for band, pool in rf.CHANNEL_POOL.items():
        first = rf.costume_channel(band, "02:5c:aa:bb:cc:dd")
        assert first in pool
        # Same cell, same channel — a channel that moved between polls would
        # read downstream as a radio retuning itself.
        assert rf.costume_channel(band, "02:5c:aa:bb:cc:dd") == first


def test_costume_channel_defaults_to_five_for_an_unknown_band():
    assert rf.costume_channel("60", "key") in rf.CHANNEL_POOL["5"]


@pytest.mark.parametrize("bandwidth,width", [
    (None, None), (0, None),
    (5, 20), (10, 20), (15, 20),      # narrower than any Wi-Fi width
    (20, 20), (40, 40), (50, 40),
    (100, 80), (160, 160), (400, 320),
])
def test_costume_width_never_overstates_the_carrier(bandwidth, width):
    assert rf.costume_width(bandwidth) == width
