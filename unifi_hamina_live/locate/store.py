"""Recent RSSI samples per (transmitter, sensor), and the fix they produce."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

from .solve import multilaterate, pathloss_distance

# Samples per (mac, sensor). At the sensor's default one report a second this
# is a minute of history, far more than any window needs -- it bounds memory
# per pair, it is not a tuning knob.
MAX_SAMPLES = 64


@dataclass(frozen=True)
class Sensor:
    """A WLAN Pi at a known position, in metres, in the floor plan's frame."""

    id: str
    x: float
    y: float
    rssi_offset: float = 0.0


@dataclass(frozen=True)
class PathLoss:
    """One technology's RSSI-to-distance constants.

    Two things live here and they behave differently, which is why path loss is
    per-kind at all:

    `exponent` describes the BUILDING — walls, clutter, how fast signal decays —
    and applies to any link in the same band. A 2.4 GHz exponent measured from
    BLE sensors is usable for 2.4 GHz Wi-Fi in that building.

    `rssi_at_1m` describes the TRANSMITTER. BLE runs about 10 dBm where an AP
    runs about 20, so the same distance reads roughly 10 dB weaker. Sharing one
    intercept across both makes every BLE fix read as further away than it is —
    consistently, which a least-squares fit absorbs into a confident wrong
    position with nothing in the residual to show it.
    """

    rssi_at_1m: float = -40.0
    exponent: float = 3.0


@dataclass(frozen=True)
class Anchor:
    """One sensor's contribution to a fix, kept for display.

    A fix with no visible working is untestable in the field: three sensors
    agreeing at 4 m and one insisting on 40 m produce a plausible-looking point,
    and the only way to see that is per-sensor detail.
    """

    sensor: str
    rssi: float
    dist_m: float
    samples: int


@dataclass
class Target:
    mac: str
    name: str
    x_m: float
    y_m: float
    residual_m: float
    anchors: list[Anchor] = field(default_factory=list)
    kind: str | None = None
    channel: int | None = None

    @property
    def sensors_used(self) -> int:
        return len(self.anchors)


class Store:
    """Sample buffer and solver, driven by the caller's clock.

    Not thread-safe, deliberately. The original guarded every access with a
    lock because it served requests from a thread pool; here ingest and solve
    both run on the event loop, so a lock would add contention to protect
    against a caller that cannot exist. If this is ever driven from a thread,
    that assumption has to be revisited rather than quietly relied on.

    Time is injected rather than read from the clock, so window expiry and
    eviction are testable without sleeping.
    """

    def __init__(self, sensors, path_loss=None, *, window_sec: float = 6.0,
                 min_sensors: int = 3, forget_sec: float = 300.0,
                 track_all: bool = True, names=None, path_loss_by_kind=None):
        self.sensors = {s.id: s for s in sensors}
        self.path_loss = path_loss or PathLoss()
        # kind -> PathLoss, for transmitters that are not Wi-Fi APs. Anything
        # absent falls back to the default above, so adding a kind is additive.
        self.path_loss_by_kind = dict(path_loss_by_kind or {})
        self.window_sec = window_sec
        self.min_sensors = min_sensors
        # A busy RF environment produces a steady stream of MACs heard once and
        # never again. The standalone tool kept every one of them for the life
        # of the process, which was fine for an ad-hoc run and an unbounded leak
        # in a service that runs for months. Anything untouched for this long is
        # dropped: it can no longer contribute to a fix, since every sample is
        # already outside the window.
        self.forget_sec = forget_sec
        self.track_all = track_all
        self.names = {k.lower(): v for k, v in (names or {}).items()}

        self._samples: dict = defaultdict(lambda: defaultdict(
            lambda: deque(maxlen=MAX_SAMPLES)))
        self._meta: dict = {}
        self._last_seen: dict = {}
        self._seq = 0

    def set_sensors(self, sensors) -> None:
        """Replace sensor positions, keeping every sample already collected.

        Positions are configured in floor-plan pixels and only become metres
        once the plan's ``meters_per_px`` is known, which arrives with a poll.
        Samples are keyed by sensor id, not position, so a rescale re-aims the
        anchors without discarding history — and ingest can run before the
        scale is known at all.
        """
        self.sensors = {s.id: s for s in sensors}

    # -- ingest -----------------------------------------------------------
    def ingest(self, sensor_id: str, detections, now: float | None = None) -> int:
        """Record one sensor's batch. Returns how many samples were kept.

        A report from an unconfigured sensor is dropped rather than accepted
        with an implied position: without knowing where it stood, its distance
        is an anchor to nowhere, and one of those is enough to drag every fix in
        the batch off target.
        """
        if sensor_id not in self.sensors:
            return 0
        now = time.monotonic() if now is None else now
        kept = 0
        for d in detections or []:
            mac = str(d.get("mac", "")).lower()
            if not mac:
                continue
            if not self.track_all and mac not in self.names:
                continue
            try:
                rssi = float(d["rssi"])
            except (KeyError, TypeError, ValueError):
                continue
            self._samples[mac][sensor_id].append((now, rssi))
            self._last_seen[mac] = now
            meta = self._meta.setdefault(mac, {"kind": None, "channel": None,
                                               "name": None})
            if d.get("kind"):
                meta["kind"] = d["kind"]
            if d.get("channel") is not None:
                meta["channel"] = d["channel"]
            # A configured name always wins; a beacon SSID fills a blank but
            # never overwrites, so a renamed AP does not flip back and forth.
            if mac in self.names:
                meta["name"] = self.names[mac]
            elif d.get("name") and not meta["name"]:
                meta["name"] = d["name"]
            kept += 1
        return kept

    def path_loss_for(self, kind) -> PathLoss:
        """The constants for a transmitter of this kind, else the default."""
        return self.path_loss_by_kind.get(kind or "", self.path_loss)

    def evict(self, now: float | None = None) -> int:
        """Forget transmitters not heard for ``forget_sec``. Returns how many."""
        now = time.monotonic() if now is None else now
        stale = [m for m, ts in self._last_seen.items()
                 if now - ts > self.forget_sec]
        for mac in stale:
            self._samples.pop(mac, None)
            self._meta.pop(mac, None)
            self._last_seen.pop(mac, None)
        return len(stale)

    # -- solve ------------------------------------------------------------
    def solve(self, area, now: float | None = None) -> list[Target]:
        """A fix for every transmitter heard by at least ``min_sensors``.

        ``area`` is ``(xmin, ymin, xmax, ymax)`` in metres -- for a floor plan,
        its pixel extent scaled by ``meters_per_px``. It bounds the initial
        search, not the answer.
        """
        now = time.monotonic() if now is None else now
        self._seq += 1
        self.evict(now)
        out: list[Target] = []
        for mac, per_sensor in self._samples.items():
            # Resolved per transmitter, before any distance is computed: a BLE
            # tag and an AP heard by the same sensors need different constants,
            # and applying one set to both is a bias, not noise.
            kind = (self._meta.get(mac) or {}).get("kind")
            pl = self.path_loss_for(kind)
            anchors, points = [], []
            for sid, dq in per_sensor.items():
                fresh = [r for ts, r in dq if now - ts <= self.window_sec]
                if not fresh:
                    continue
                s = self.sensors[sid]
                rssi = _median(fresh) + s.rssi_offset
                dist = pathloss_distance(rssi, pl.rssi_at_1m, pl.exponent)
                # A stronger signal is a shorter, and therefore more
                # trustworthy, range estimate: path-loss error grows with
                # distance, so a far anchor should not outvote a near one.
                points.append((s.x, s.y, dist, 1.0 / max(dist, 1.0)))
                anchors.append(Anchor(sensor=sid, rssi=round(rssi, 1),
                                      dist_m=round(dist, 2), samples=len(fresh)))
            if len(points) < self.min_sensors:
                continue
            x, y, rms = multilaterate(points, area)
            meta = self._meta.get(mac, {})
            out.append(Target(
                mac=mac, name=meta.get("name") or mac,
                x_m=round(x, 2), y_m=round(y, 2), residual_m=round(rms, 2),
                anchors=sorted(anchors, key=lambda a: a.sensor),
                kind=meta.get("kind"), channel=meta.get("channel")))
        out.sort(key=lambda t: t.name)
        return out

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def tracked(self) -> int:
        """Transmitters currently held. Watch it: it is the leak indicator."""
        return len(self._samples)


def _median(values):
    v = sorted(values)
    n = len(v)
    m = n // 2
    return v[m] if n % 2 else (v[m - 1] + v[m]) / 2.0
