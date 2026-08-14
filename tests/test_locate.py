"""RSSI multilateration: the solver, and the sample store feeding it.

Everything here is synthetic. RSSI is generated from the same path-loss model
the solver inverts, so a fix must land on the position that produced it -- with
noise added, the tests assert a tolerance rather than equality, because a
solver that only works on exact input is not a solver.
"""
import math

from unifi_hamina_live.locate import (
    PathLoss, Sensor, Store, m_to_px, multilaterate, pathloss_distance, px_to_m,
)

PL = PathLoss(rssi_at_1m=-40.0, exponent=3.0)
# a 20 x 15 m room with a sensor in each corner
SENSORS = [Sensor("pi-1", 0.0, 0.0), Sensor("pi-2", 20.0, 0.0),
           Sensor("pi-3", 20.0, 15.0), Sensor("pi-4", 0.0, 15.0)]
AREA = (0.0, 0.0, 20.0, 15.0)


def rssi_at(sensor, x, y, pl=PL, noise=0.0):
    """What `sensor` would hear from a transmitter at (x, y)."""
    d = max(math.hypot(x - sensor.x, y - sensor.y), 0.3)
    return pl.rssi_at_1m - 10.0 * pl.exponent * math.log10(d) + noise


def report(store, x, y, mac="aa:bb:cc:dd:ee:01", now=0.0, noise=None, **kw):
    """Every sensor hears one transmitter at (x, y)."""
    for i, s in enumerate(SENSORS):
        n = 0.0 if noise is None else noise[i]
        store.ingest(s.id, [{"mac": mac, "rssi": rssi_at(s, x, y, noise=n), **kw}],
                     now=now)


# --- path loss --------------------------------------------------------------

def test_one_metre_is_the_reference_reading():
    assert abs(pathloss_distance(-40.0, -40.0, 3.0) - 1.0) < 1e-9


def test_the_model_round_trips():
    for d in (0.5, 1.0, 4.0, 12.5, 60.0):
        rssi = -40.0 - 10.0 * 3.0 * math.log10(d)
        assert abs(pathloss_distance(rssi, -40.0, 3.0) - d) < 1e-6


def test_absurd_readings_are_clamped_rather_than_trusted():
    """One wild sample must not be able to dominate a least-squares fit."""
    assert pathloss_distance(999.0, -40.0, 3.0) == 0.3
    assert pathloss_distance(-999.0, -40.0, 3.0) == 500.0


# --- the solver -------------------------------------------------------------

def test_exact_ranges_recover_the_position():
    for tx, ty in ((10.0, 7.5), (2.0, 2.0), (18.5, 13.0)):
        pts = [(s.x, s.y, math.hypot(tx - s.x, ty - s.y), 1.0) for s in SENSORS]
        x, y, rms = multilaterate(pts, AREA)
        assert math.hypot(x - tx, y - ty) < 0.1, (tx, ty, x, y)
        assert rms < 0.05


def test_a_target_outside_the_hull_is_not_squeezed_into_the_area():
    """Clamping would dress an extrapolation up as a measurement.

    A point outside the sensors estimates outside them. Callers reject on
    residual; they are not handed a boundary coordinate that looks surveyed.
    """
    tx, ty = 26.0, 7.5
    pts = [(s.x, s.y, math.hypot(tx - s.x, ty - s.y), 1.0) for s in SENSORS]
    x, _y, _rms = multilaterate(pts, AREA)
    assert x > AREA[2] - 1.0, f"collapsed back inside the area: x={x}"


def test_collinear_sensors_are_reported_by_the_residual_not_hidden():
    """Degenerate geometry cannot be solved, and must not look solved."""
    line = [Sensor("a", 0.0, 0.0), Sensor("b", 10.0, 0.0), Sensor("c", 20.0, 0.0)]
    tx, ty = 10.0, 8.0
    pts = [(s.x, s.y, math.hypot(tx - s.x, ty - s.y), 1.0) for s in line]
    x, y, rms = multilaterate(pts, AREA)
    mirrored = math.hypot(x - tx, y - (-ty))
    assert min(math.hypot(x - tx, y - ty), mirrored) < 1.0, "should fit one side"
    assert rms < 1.0  # fits well -- which is exactly why residual alone is not proof


# --- the store --------------------------------------------------------------

def test_a_fix_lands_where_the_signal_came_from():
    st = Store(SENSORS, PL, window_sec=6.0)
    report(st, 12.0, 4.0, now=100.0)
    (t,) = st.solve(AREA, now=100.5)
    assert math.hypot(t.x_m - 12.0, t.y_m - 4.0) < 0.5, (t.x_m, t.y_m)
    assert t.sensors_used == 4
    assert t.residual_m < 0.5


def test_noise_degrades_the_fix_without_breaking_it():
    st = Store(SENSORS, PL, window_sec=6.0)
    report(st, 8.0, 6.0, now=0.0, noise=[2.0, -3.0, 1.5, -1.0])
    (t,) = st.solve(AREA, now=0.5)
    assert math.hypot(t.x_m - 8.0, t.y_m - 6.0) < 3.0
    assert t.residual_m > 0.0, "noise must show up in the residual"


def test_too_few_sensors_yields_nothing_rather_than_a_guess():
    st = Store(SENSORS, PL, min_sensors=3)
    for s in SENSORS[:2]:
        st.ingest(s.id, [{"mac": "aa:bb:cc:dd:ee:01", "rssi": -55.0}], now=0.0)
    assert st.solve(AREA, now=0.1) == []


def test_a_report_from_an_unconfigured_sensor_is_dropped():
    """Its position is unknown, so its range is an anchor to nowhere."""
    st = Store(SENSORS, PL)
    assert st.ingest("pi-rogue", [{"mac": "aa:bb:cc:dd:ee:01", "rssi": -50.0}]) == 0
    assert st.tracked == 0


def test_samples_outside_the_window_stop_counting():
    st = Store(SENSORS, PL, window_sec=6.0)
    report(st, 5.0, 5.0, now=0.0)
    assert len(st.solve(AREA, now=1.0)) == 1
    assert st.solve(AREA, now=100.0) == [], "stale samples must not produce a fix"


def test_transmitters_heard_once_are_eventually_forgotten():
    """The leak this port fixes.

    The standalone tool kept every MAC it ever heard for the life of the
    process. Fine for an ad-hoc run; in a service that runs for months in a busy
    RF environment it grows without bound, one passer-by at a time.
    """
    st = Store(SENSORS, PL, forget_sec=300.0)
    for i in range(50):
        st.ingest("pi-1", [{"mac": f"aa:bb:cc:00:00:{i:02x}", "rssi": -70.0}],
                  now=0.0)
    assert st.tracked == 50
    report(st, 10.0, 7.5, mac="aa:bb:cc:dd:ee:01", now=600.0)   # still around
    st.solve(AREA, now=600.0)
    assert st.tracked == 1, "one-off transmitters should be gone"


def test_a_configured_name_beats_a_beacon_ssid():
    st = Store(SENSORS, PL, names={"AA:BB:CC:DD:EE:01": "AP-Lobby"})
    report(st, 10.0, 7.5, now=0.0, name="SomeSSID", kind="ap", channel=36)
    (t,) = st.solve(AREA, now=0.1)
    assert t.name == "AP-Lobby"
    assert t.channel == 36 and t.kind == "ap"


def test_an_unnamed_transmitter_falls_back_to_its_mac():
    st = Store(SENSORS, PL)
    report(st, 10.0, 7.5, now=0.0)
    (t,) = st.solve(AREA, now=0.1)
    assert t.name == "aa:bb:cc:dd:ee:01"


def test_every_contributing_sensor_is_shown():
    """Three sensors agreeing and one wildly disagreeing still make a point."""
    st = Store(SENSORS, PL)
    report(st, 10.0, 7.5, now=0.0)
    (t,) = st.solve(AREA, now=0.1)
    assert [a.sensor for a in t.anchors] == ["pi-1", "pi-2", "pi-3", "pi-4"]
    assert all(a.samples == 1 and a.dist_m > 0 for a in t.anchors)


# --- floor-plan coordinates -------------------------------------------------

def test_pixels_and_metres_round_trip():
    x_m, y_m = px_to_m(400.0, 250.0, 0.05)
    assert (x_m, y_m) == (20.0, 12.5)
    assert m_to_px(x_m, y_m, 0.05) == (400.0, 250.0)


def test_an_unscaled_plan_refuses_rather_than_dividing_by_zero():
    """A plan with no meters_per_px cannot place anything; say so."""
    try:
        m_to_px(1.0, 1.0, 0.0)
    except ValueError as e:
        assert "meters_per_px" in str(e)
    else:
        raise AssertionError("expected a ValueError")


# --- path loss per technology ------------------------------------------------

BLE = PathLoss(rssi_at_1m=-50.0, exponent=3.0)   # ~10 dB weaker TX than an AP


def test_the_same_reading_from_ble_is_a_shorter_distance():
    """The bias this exists to remove.

    BLE transmits about 10 dB weaker, so an identical RSSI means the
    transmitter is CLOSER, not further. One shared intercept pushes every BLE
    fix outward — consistently, which is a bias the residual cannot show.
    """
    wifi_d = pathloss_distance(-70.0, -40.0, 3.0)
    ble_d = pathloss_distance(-70.0, -50.0, 3.0)
    assert ble_d < wifi_d
    assert 2.0 < wifi_d / ble_d < 2.5, "10 dB at n=3 is about a 2.15x factor"


def test_a_ble_transmitter_uses_the_ble_constants():
    st = Store(SENSORS, PL, window_sec=6.0, path_loss_by_kind={"ble": BLE})
    report(st, 10.0, 7.5, mac="aa:bb:cc:dd:ee:02", now=0.0, kind="ble")
    assert st.path_loss_for("ble").rssi_at_1m == -50.0
    (t,) = st.solve(AREA, now=0.1)
    assert t.kind == "ble"


def test_wifi_is_untouched_by_a_ble_entry_existing():
    st = Store(SENSORS, PL, path_loss_by_kind={"ble": BLE})
    assert st.path_loss_for("ap").rssi_at_1m == PL.rssi_at_1m
    assert st.path_loss_for(None).rssi_at_1m == PL.rssi_at_1m
    assert st.path_loss_for("something-new").rssi_at_1m == PL.rssi_at_1m


def test_a_ble_fix_lands_where_the_signal_came_from():
    """End to end with BLE constants: synthesise from them, recover the spot."""
    st = Store(SENSORS, BLE, window_sec=6.0, path_loss_by_kind={"ble": BLE})
    for s in SENSORS:
        d = max(math.hypot(6.0 - s.x, 9.0 - s.y), 0.3)
        st.ingest(s.id, [{"mac": "aa:bb:cc:dd:ee:03", "kind": "ble",
                          "rssi": BLE.rssi_at_1m
                                  - 10.0 * BLE.exponent * math.log10(d)}], now=0.0)
    (t,) = st.solve(AREA, now=0.1)
    assert math.hypot(t.x_m - 6.0, t.y_m - 9.0) < 0.5, (t.x_m, t.y_m)


def test_mixing_kinds_without_splitting_would_move_the_fix():
    """Why this is a bias and not a rounding detail.

    The same BLE transmitter, solved with Wi-Fi constants, lands somewhere else
    entirely — so a mixed deployment on one PathLoss is not slightly wrong.
    """
    true_xy = (6.0, 9.0)

    def fix(store_pl, by_kind):
        st = Store(SENSORS, store_pl, path_loss_by_kind=by_kind)
        for s in SENSORS:
            d = max(math.hypot(true_xy[0] - s.x, true_xy[1] - s.y), 0.3)
            st.ingest(s.id, [{"mac": "aa:bb:cc:dd:ee:04", "kind": "ble",
                              "rssi": BLE.rssi_at_1m
                                      - 10.0 * BLE.exponent * math.log10(d)}],
                      now=0.0)
        (t,) = st.solve(AREA, now=0.1)
        return t

    right = fix(PL, {"ble": BLE})
    wrong = fix(PL, {})                      # BLE solved as if it were Wi-Fi
    off_by = math.hypot(wrong.x_m - right.x_m, wrong.y_m - right.y_m)
    assert off_by > 2.0, f"only {off_by:.1f} m apart — check the constants"
    assert math.hypot(right.x_m - true_xy[0], right.y_m - true_xy[1]) < 0.5
