"""Read a Hamina OpenIntent export as the floor-plan source."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import zipfile
from dataclasses import dataclass, field

from ..models import FloorPlan

log = logging.getLogger("unifi_hamina_live.plan")


@dataclass
class PlanAP:
    """An AP as the export places it, before any join to live UniFi data."""

    name: str
    mac: str | None
    plan_id: str
    x: float
    y: float


@dataclass
class OpenIntentPlan:
    floorplans: list[FloorPlan] = field(default_factory=list)
    aps: list[PlanAP] = field(default_factory=list)
    images: dict[str, bytes] = field(default_factory=dict)


def plan_id_for(name: str) -> str:
    """A stable id for a plan that carries no id of the bridge's own.

    Derived from the name because cells.json and sensors.json reference plans by
    id, and those files must not need rewriting every time the zip is
    regenerated. A hash suffix keeps two names that slugify alike from
    collapsing into one plan.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "plan").lower()).strip("-") or "plan"
    return f"oi-{slug}-{hashlib.sha256((name or '').encode()).hexdigest()[:6]}"


def norm_name(name: str) -> str:
    return " ".join((name or "").split()).casefold()


def _pixel_dims(fp: dict) -> tuple[float | None, float | None, float | None]:
    """(width_px, height_px, meters_per_px) for one OpenIntent floorplan.

    OpenIntent calls the second plan axis `length`; `height` on the same object
    is the CEILING. Reading `height` gives a plan 2 m tall.
    """
    dims = fp.get("dimensions") or []
    px = next((d for d in dims if d.get("unit") == "pixels"), None)
    me = next((d for d in dims if d.get("unit") == "meters"), None)
    if not px:
        return None, None, None
    w = float(px.get("width") or 0) or None
    h = float(px.get("length") or 0) or None
    mpp = float(me["width"]) / w if (me and me.get("width") and w) else None
    return w, h, mpp


def _pixel_xy(ap: dict) -> tuple[float, float] | None:
    """The pixel coordinate out of an OpenIntent `coordinates` list.

    Every position is published twice, once in pixels and once in metres, as
    `[{"coordinate_xyz": {...,"unit":"pixels"}}, {...,"unit":"meters"}]`. Taking
    element [0] happens to work on our own exporter's output and would silently
    read metres — a 43 px plan — from a writer that ordered them the other way.
    """
    for entry in ap.get("coordinates") or []:
        c = (entry or {}).get("coordinate_xyz") or {}
        if c.get("unit") == "pixels" and c.get("x") is not None:
            return float(c["x"]), float(c["y"])
    return None


def load_openintent_plan(path: str, site_id: str) -> OpenIntentPlan:
    """Parse an OpenIntent zip into floor plans, AP placements and images.

    Coordinates pass through untouched. OpenIntent pixel space and this
    service's plan space are the same space by construction, not by luck:
    `unifi/placement.py:scene_to_pixels` — the InnerSpace path everything else
    is calibrated against — applies the identical `+ img_h / 2` re-centre with
    no y flip. Adding one here would mirror every AP about the middle of the
    plan, which this project has shipped twice.
    """
    out = OpenIntentPlan()
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        doc_name = next((n for n in names if n.endswith("openintent.json")), None) \
            or next((n for n in names if n.endswith(".json")), None)
        if not doc_name:
            raise ValueError(f"{path} contains no .json — is it an OpenIntent export?")
        doc = json.loads(z.read(doc_name))
        if not isinstance(doc, dict) or "floorplans" not in doc:
            keys = sorted(doc)[:8] if isinstance(doc, dict) else type(doc).__name__
            raise ValueError(f"{doc_name} has no 'floorplans' — found {keys}")

        by_name: dict[str, str] = {}
        for fp in doc.get("floorplans") or []:
            name = fp.get("name") or "Plan"
            pid = by_name.setdefault(name, plan_id_for(name))
            w, h, mpp = _pixel_dims(fp)
            out.floorplans.append(FloorPlan(
                id=pid, site_id=site_id, name=name, source="openintent",
                width_px=w, height_px=h, meters_per_px=mpp,
                # The zip's own path, so a replaced image changes the ref and
                # the staleness check downstream still means something.
                image_ref=fp.get("map_uri") or f"{path}#{name}"))
            uri = fp.get("map_uri") or ""
            if uri:
                leaf = uri.split("/")[-1]
                hit = next((n for n in names if n.split("/")[-1] == leaf), None)
                if hit:
                    out.images[pid] = z.read(hit)
                else:
                    log.warning("plan %r references %s, absent from %s", name, uri, path)

        first = next(iter(by_name.values()), None)
        for ap in doc.get("accesspoints") or []:
            pid = by_name.get(ap.get("floorplan_name")) or first
            xy = _pixel_xy(ap)
            name = ap.get("name") or ""
            if pid is None or xy is None or not name:
                continue
            mac = (ap.get("mac_address") or "").lower() or None
            out.aps.append(PlanAP(name=name, mac=mac, plan_id=pid, x=xy[0], y=xy[1]))
    return out
