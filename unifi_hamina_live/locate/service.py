"""Wiring: sensor layout on a floor plan, and fixes in that plan's coordinates."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from ..config import Settings
from .solve import m_to_px, px_to_m
from .store import PathLoss, Sensor, Store

log = logging.getLogger("unifi_hamina_live.locate")


@dataclass(frozen=True)
class SensorSpec:
    """A sensor's position in FLOOR-PLAN PIXELS, as configured."""

    id: str
    x_px: float
    y_px: float
    rssi_offset: float = 0.0


@dataclass(frozen=True)
class Layout:
    plan_id: str
    sensors: tuple[SensorSpec, ...]
    names: dict            # mac -> friendly name

    @classmethod
    def load(cls, path: str) -> "Layout":
        with open(path) as fh:
            raw = json.load(fh)
        plan_id = str(raw.get("plan_id") or "").strip()
        if not plan_id:
            raise ValueError("sensor config needs 'plan_id' (a floor plan id "
                             "from GET /api/floorplans)")
        specs = []
        for s in raw.get("sensors") or []:
            try:
                specs.append(SensorSpec(id=str(s["id"]), x_px=float(s["x_px"]),
                                        y_px=float(s["y_px"]),
                                        rssi_offset=float(s.get("rssi_offset", 0.0))))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("each sensor needs id, x_px, y_px: %s" % exc)
        if len(specs) < 3:
            # Two circles meet at two points; a third picks one. Starting with
            # fewer produces no fixes at all and no explanation, so say it here.
            raise ValueError("at least 3 sensors are needed to fix a position; "
                             "got %d" % len(specs))
        ids = [s.id for s in specs]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate sensor id in %s" % path)
        names = {str(t["mac"]).lower(): t.get("name") or str(t["mac"])
                 for t in raw.get("targets") or [] if t.get("mac")}
        return cls(plan_id=plan_id, sensors=tuple(specs), names=names)


class LocateService:
    """Holds the sample store and turns it into plan coordinates.

    Kept apart from the collector: a sensor report is not a poll, and the two
    fail independently. A console outage must not stop sensors reporting, and a
    misconfigured plan must not stop the console being polled.
    """

    def __init__(self, settings: Settings, layout: Layout):
        self.settings = settings
        self.layout = layout
        self.store = Store(
            # Placeholder positions: the real ones need the plan's
            # meters_per_px, which arrives with a poll. Ingest works meanwhile.
            [Sensor(s.id, 0.0, 0.0, s.rssi_offset) for s in layout.sensors],
            PathLoss(settings.sensor_rssi_at_1m, settings.sensor_pathloss_exponent),
            window_sec=settings.sensor_window_seconds,
            min_sensors=settings.sensor_min_sensors,
            forget_sec=settings.sensor_forget_seconds,
            names=layout.names,
        )
        self._scale: float | None = None

    def _plan(self, snapshot):
        for fp in snapshot.floorplans:
            if fp.id == self.layout.plan_id:
                return fp
        return None

    def rescale(self, meters_per_px: float) -> None:
        if meters_per_px == self._scale:
            return
        self.store.set_sensors([
            Sensor(s.id, *px_to_m(s.x_px, s.y_px, meters_per_px), s.rssi_offset)
            for s in self.layout.sensors
        ])
        self._scale = meters_per_px
        log.info("sensor layout scaled at %.5f m/px", meters_per_px)

    def positions(self, snapshot) -> dict:
        """Current fixes, or a reason there are none.

        Every failure here is a configuration mistake with a specific fix, and
        each one otherwise presents as an empty list — which reads as "nothing
        detected" and sends you to check the radios.
        """
        plan = self._plan(snapshot)
        if plan is None:
            return self._empty("plan %r is not in this console's floor plans "
                               "(see GET /api/floorplans)" % self.layout.plan_id)
        if not plan.meters_per_px:
            return self._empty("floor plan %r has no scale set in InnerSpace, "
                               "so pixels cannot be converted to metres"
                               % (plan.name or plan.id))
        self.rescale(plan.meters_per_px)

        area_m = (0.0, 0.0,
                  (plan.width_px or 0.0) * plan.meters_per_px,
                  (plan.height_px or 0.0) * plan.meters_per_px)
        targets = []
        for t in self.store.solve(area_m):
            x_px, y_px = m_to_px(t.x_m, t.y_m, plan.meters_per_px)
            targets.append({
                "mac": t.mac, "name": t.name, "kind": t.kind,
                "channel": t.channel,
                "x": round(x_px, 1), "y": round(y_px, 1),
                "x_m": t.x_m, "y_m": t.y_m,
                # Fitting error, in metres. NOT a confidence interval: sensors
                # in a near-straight line fit beautifully and are still
                # ambiguous between two mirrored positions. Small residual plus
                # bad geometry is a confident wrong answer.
                "residual_m": t.residual_m,
                "sensors_used": t.sensors_used,
                "anchors": [{"sensor": a.sensor, "rssi": a.rssi,
                             "dist_m": a.dist_m, "samples": a.samples}
                            for a in t.anchors],
            })
        return {
            "ok": True, "error": None,
            "plan_id": plan.id, "plan_name": plan.name,
            "meters_per_px": plan.meters_per_px,
            "estimated": True,   # never confuse these with surveyed placements
            "sensors": [{"id": s.id, "x": s.x_px, "y": s.y_px}
                        for s in self.layout.sensors],
            "tracked": self.store.tracked,
            "targets": targets,
        }

    def _empty(self, error: str) -> dict:
        log.warning("no positions: %s", error)
        return {"ok": False, "error": error, "plan_id": self.layout.plan_id,
                "plan_name": None, "meters_per_px": None, "estimated": True,
                "sensors": [], "tracked": self.store.tracked, "targets": []}
