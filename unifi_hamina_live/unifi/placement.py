"""Live AP placement (floor plans + x,y), from classic Maps or InnerSpace.

Positions are produced in the **same pixel coordinate space the OpenIntent
exporter uses**, so a live position lines up with what Hamina imported. This is
what lets AP moves flow through the live API instead of forcing a full
OpenIntent regeneration — the zip is then only needed for the initial import
(floor-plan images + geometry).

The coordinate math mirrors ``unifi_export.py`` (kept deliberately identical):

  * classic Maps store per-device pixel x,y directly on ``stat/device``.
  * InnerSpace stores scene coordinates centred on the image; converting needs
    the plan's map offset/scale and the image dimensions (``scene_to_pixels``).

All transforms here are pure so they can be unit-tested without a console.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ..models import FloorPlan
from .normalize import normalize_mac


@dataclass
class Position:
    floorplan_id: str
    x: float
    y: float


# --- classic Maps ---------------------------------------------------------
def legacy_placement(
    site_id: str, maps: dict, devices: list[dict]
) -> tuple[list[FloorPlan], dict[str, Position]]:
    """Positions come straight off stat/device (map_id, x, y already pixels)."""
    floorplans: dict[str, FloorPlan] = {}
    for map_id, m in maps.items():
        floorplans[map_id] = FloorPlan(
            id=str(map_id),
            site_id=site_id,
            name=m.get("name") or str(map_id),
            source="legacy",
            width_px=_num(m.get("width")),
            height_px=_num(m.get("height")),
            meters_per_px=_num(m.get("upp")),
        )
    positions: dict[str, Position] = {}
    for dev in devices:
        map_id = dev.get("map_id")
        x, y = dev.get("x"), dev.get("y")
        mac = normalize_mac(dev.get("mac"))
        if map_id and mac and x is not None and y is not None and str(map_id) in floorplans:
            positions[mac] = Position(str(map_id), float(x), float(y))
    return list(floorplans.values()), positions


# --- InnerSpace -----------------------------------------------------------
def scene_to_pixels(pt: dict, map_shape: dict, img_w: float, img_h: float) -> tuple[float, float]:
    """InnerSpace scene units -> OpenIntent pixel coordinates (identical to the
    exporter; do NOT flip y)."""
    off = (map_shape.get("position") or [{}])[0]
    sc = map_shape.get("scale") or {}
    sx = float(sc.get("x") or 1) or 1
    sy = float(sc.get("y") or 1) or 1
    x = (float(pt["x"]) - float(off.get("x") or 0)) / sx + img_w / 2.0
    y = (float(pt["y"]) - float(off.get("y") or 0)) / sy + img_h / 2.0
    return x, y


def innerspace_image_urls(project: dict) -> dict[str, str]:
    """{planId: image_url} for plans that have a map image — the collector uses
    these to fetch dimensions once (and cache them)."""
    urls: dict[str, str] = {}
    for s in project.get("shapes") or []:
        if s.get("type") == "map" and s.get("planId") and s.get("urlImage"):
            urls[s["planId"]] = s["urlImage"]
    return urls


def innerspace_placement(
    site_id: str, project: dict, image_dims: dict[str, tuple[float, float]]
) -> tuple[list[FloorPlan], dict[str, Position]]:
    """Build floor plans + positions from an InnerSpace project.

    ``image_dims`` maps planId -> (width_px, height_px); plans without known
    dimensions are still emitted but their device positions are skipped (we
    can't place without the image size).
    """
    plans = {p["id"]: p for p in project.get("plans") or []}
    products = {p["id"]: p for p in project.get("products") or []}
    by_plan: dict[str, list[dict]] = {}
    for s in project.get("shapes") or []:
        pid = s.get("planId")
        if pid:
            by_plan.setdefault(pid, []).append(s)

    floorplans: list[FloorPlan] = []
    positions: dict[str, Position] = {}
    for pid, shapes in by_plan.items():
        map_shape = next((s for s in shapes if s.get("type") == "map"), None)
        if not map_shape:
            continue
        plan = plans.get(pid) or {}
        dims = image_dims.get(pid)
        img_w, img_h = dims if dims else (None, None)
        floorplans.append(
            FloorPlan(
                id=str(pid),
                site_id=site_id,
                name=plan.get("title") or str(pid),
                source="innerspace",
                width_px=img_w,
                height_px=img_h,
                meters_per_px=_meters_per_px(shapes, map_shape),
            )
        )
        if not dims:
            continue
        for s in shapes:
            if s.get("type") != "device":
                continue
            if (products.get(s.get("productId")) or {}).get("category") != "wifi":
                continue
            pts = s.get("position") or []
            mac = normalize_mac((s.get("meta") or {}).get("mac"))
            if pts and mac:
                x, y = scene_to_pixels(pts[0], map_shape, img_w, img_h)
                positions[mac] = Position(str(pid), round(x, 2), round(y, 2))
    return floorplans, positions


def _meters_per_px(shapes: list[dict], map_shape: dict) -> float | None:
    scale_shape = next((s for s in shapes if s.get("type") == "scale"), None)
    if not scale_shape or not scale_shape.get("scale"):
        return None
    p = scale_shape.get("position") or []
    if len(p) < 2:
        return None
    dist = ((p[1]["x"] - p[0]["x"]) ** 2 + (p[1]["y"] - p[0]["y"]) ** 2) ** 0.5
    if not dist:
        return None
    m_per_unit = float(scale_shape["scale"]) / dist
    sx = float((map_shape.get("scale") or {}).get("x") or 1) or 1
    return round(m_per_unit * sx, 6)


# --- shared ---------------------------------------------------------------
def image_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) from PNG/JPEG bytes, or None (mirrors the exporter)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) > 24:
        w, h = struct.unpack(">II", data[16:24])
        return w, h
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seglen = struct.unpack(">H", data[i + 2:i + 4])[0]
            i += 2 + seglen
    return None


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) else None
