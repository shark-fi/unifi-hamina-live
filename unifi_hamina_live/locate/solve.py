"""Distance from signal strength, and position from distances."""

from __future__ import annotations

import math

# A sensor cannot be usefully closer than this, and a reading implying
# kilometres is noise rather than a measurement. Clamping keeps one absurd
# sample from dominating a least-squares fit that has no other defence.
MIN_DISTANCE_M = 0.3
MAX_DISTANCE_M = 500.0


def pathloss_distance(rssi: float, rssi_at_1m: float, exponent: float) -> float:
    """Invert the log-distance path-loss model: ``RSSI(d) = A - 10*n*log10(d)``.

    ``rssi_at_1m`` and ``exponent`` are site constants, and they are guesses
    until someone calibrates them -- the sensor tool has a ``--calibrate`` mode
    for exactly this. An uncalibrated exponent does not fail; it biases every
    distance in the same direction, which a least-squares fit happily absorbs
    into a confident, wrong position. The residual reported alongside the fix is
    what exposes that, so it must never be dropped on the way out.
    """
    d = 10.0 ** ((rssi_at_1m - rssi) / (10.0 * exponent))
    return min(max(d, MIN_DISTANCE_M), MAX_DISTANCE_M)


def multilaterate(points, area, coarse: int = 41):
    """Least-squares position from ``(x, y, distance, weight)`` anchors.

    Coarse grid search first, then a shrinking coordinate descent. The grid is
    not an optimisation -- with bad geometry (sensors nearly collinear, or a
    target outside their hull) the cost surface has local minima that a descent
    started anywhere else settles into happily.

    Returns ``(x, y, rms_residual_m)``, all in the same units as the anchors.

    The result is NOT clamped to ``area``. A target outside the sensor hull
    genuinely estimates outside it, and clamping would move the point onto the
    boundary where it looks like a real measurement instead of the extrapolation
    it is. Callers that need it inside a plan should reject on residual, not
    squeeze the coordinates.
    """
    xmin, ymin, xmax, ymax = area

    def cost(px: float, py: float) -> float:
        s = 0.0
        for x, y, d, w in points:
            r = math.hypot(px - x, py - y) - d
            s += w * r * r
        return s

    bx, by, bc = (xmin + xmax) / 2.0, (ymin + ymax) / 2.0, float("inf")
    for i in range(coarse):
        px = xmin + (xmax - xmin) * i / (coarse - 1)
        for j in range(coarse):
            py = ymin + (ymax - ymin) * j / (coarse - 1)
            c = cost(px, py)
            if c < bc:
                bc, bx, by = c, px, py

    step = max(xmax - xmin, ymax - ymin) / (coarse - 1)
    for _ in range(200):
        improved = False
        for dx, dy in ((step, 0), (-step, 0), (0, step), (0, -step)):
            c = cost(bx + dx, by + dy)
            if c < bc:
                bc, bx, by = c, bx + dx, by + dy
                improved = True
        if not improved:
            step *= 0.5
            if step < 0.01:
                break

    wsum = sum(w for _, _, _, w in points) or 1.0
    return bx, by, math.sqrt(bc / wsum)


# --- floor-plan pixels <-> metres -------------------------------------------
#
# The whole reason this runs inside the bridge. A plan's meters_per_px comes
# from InnerSpace, so a sensor position clicked on the plan and an AP placement
# read from the console are already in the same frame -- there is no transform
# to derive and nothing to calibrate.
#
# Floor-plan y grows DOWNWARD (image convention) and so does the metre value
# derived from it. That is deliberate: it matches how placements are already
# reported, so a located target and a placed AP can be drawn by the same code.
# Flipping here would make this module disagree with every other coordinate in
# the service.


def px_to_m(x_px: float, y_px: float, meters_per_px: float):
    return x_px * meters_per_px, y_px * meters_per_px


def m_to_px(x_m: float, y_m: float, meters_per_px: float):
    if not meters_per_px:
        raise ValueError("meters_per_px is zero or unknown for this plan")
    return x_m / meters_per_px, y_m / meters_per_px
