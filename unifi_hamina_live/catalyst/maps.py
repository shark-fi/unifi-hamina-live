"""Catalyst Center Maps import/export: async job flow + map-archive builder.

Hamina's importer, after resolving the site hierarchy, exports a floor's map by
POSTing to ``/dna/intent/api/v1/maps/export/{floorId}``. On a real appliance
that is an asynchronous BAPI:

    POST /dna/intent/api/v1/maps/export/{floorId}
        -> { executionId, executionStatusUrl, message }
    GET  {executionStatusUrl}                       (dnacaap execution-status)
        -> { status: "SUCCESS", additionalStatusURL: "/.../file/{fileId}", ... }
    GET  {additionalStatusURL}
        -> the map archive (a gzipped tar of a maps XML + the floor images)

The archive is Cisco's "Prime/Catalyst map archive" format — the same shape
Hamina produces when you export a project *to* Catalyst Center, round-tripped
back. We build it from live UniFi data: the floor image bytes the collector
already caches, the floor dimensions in metres, and the AP placements.

NOTE: the exact XML schema is being pinned against a real appliance/Hamina
export; ``build_maps_xml`` is intentionally the one place that encodes it.
"""

from __future__ import annotations

import io
import logging
import tarfile
import uuid
from xml.sax.saxutils import escape, quoteattr

from ..models import FloorPlan, Snapshot
from . import mapping

log = logging.getLogger("unifi_hamina_live.catalyst.maps")

_NS = uuid.UUID("6f5c9e2a-2222-4000-8000-000000000000")


class MapExportJobs:
    """In-memory registry of maps/export async jobs (executionId -> floor)."""

    def __init__(self) -> None:
        self._by_exec: dict[str, dict] = {}
        self._by_file: dict[str, dict] = {}

    def create(self, floor_id: str) -> dict:
        # Deterministic ids per floor so repeated exports are idempotent and
        # resumable, and so a stale executionId still resolves.
        exec_id = str(uuid.uuid5(_NS, "exec:" + floor_id))
        file_id = str(uuid.uuid5(_NS, "file:" + floor_id))
        job = {"floor_id": floor_id, "exec_id": exec_id, "file_id": file_id}
        self._by_exec[exec_id] = job
        self._by_file[file_id] = job
        return job

    def by_exec(self, exec_id: str) -> dict | None:
        return self._by_exec.get(exec_id)

    def by_file(self, file_id: str) -> dict | None:
        return self._by_file.get(file_id)


# --- archive --------------------------------------------------------------
def _floor(snap: Snapshot, floor_id: str) -> FloorPlan | None:
    return next((f for f in snap.floorplans if mapping.floor_id_for(f) == floor_id), None)


def _site_name(snap: Snapshot, fp: FloorPlan) -> str:
    s = next((s for s in snap.sites if s.id == fp.site_id), None)
    return s.name if s else fp.site_id


def build_maps_xml(snap: Snapshot, fp: FloorPlan, image_name: str) -> str:
    """Cisco map-archive XML for a single floor.

    Encodes the building/floor hierarchy, the floor dimensions in metres, the
    image reference, and each AP's x,y placement in floor metres. This is the
    one spot pinned to the real appliance's schema.
    """
    building = _site_name(snap, fp)
    w_m, l_m = mapping._metres_dims(fp)
    aps = [a for a in snap.access_points if a.floorplan_id == fp.id]

    def _ap_xml(a) -> str:
        x_m, y_m = mapping._ap_metres(a, fp)
        return (
            f'    <AccessPoint name={quoteattr(a.name)} '
            f'macAddress={quoteattr(a.mac)} model={quoteattr(a.model)}>\n'
            f'      <Position x="{x_m or 0}" y="{y_m or 0}" z="3.0"/>\n'
            f'    </AccessPoint>'
        )

    ap_block = "\n".join(_ap_xml(a) for a in aps)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Maps>\n'
        f'  <Area name="{escape(mapping._AREA_NAME)}">\n'
        f'   <Building name={quoteattr(building)}>\n'
        f'    <Floor name={quoteattr(fp.name)} number="1">\n'
        f'      <Dimension length="{l_m or 0}" width="{w_m or 0}" height="3.0" '
        f'offsetX="0.0" offsetY="0.0" unit="meters"/>\n'
        f'      <Image name={quoteattr(image_name)} '
        f'width="{fp.width_px or 0}" height="{fp.height_px or 0}"/>\n'
        f'      <AccessPoints>\n{ap_block}\n      </AccessPoints>\n'
        f'    </Floor>\n'
        f'   </Building>\n'
        f'  </Area>\n'
        '</Maps>\n'
    )


def build_archive(snap: Snapshot, floor_id: str, image_bytes: bytes | None) -> bytes:
    """Build the gzipped-tar map archive for a floor. Returns the raw bytes."""
    fp = _floor(snap, floor_id)
    if fp is None:
        raise KeyError(floor_id)
    ext = _image_ext(image_bytes)
    image_name = f"{fp.name}{ext}"
    xml = build_maps_xml(snap, fp, image_name).encode("utf-8")

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _add(tar, "maps.xml", xml)
        if image_bytes:
            _add(tar, image_name, image_bytes)
    return buf.getvalue()


def _add(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def _image_ext(blob: bytes | None) -> str:
    if not blob:
        return ".png"
    if blob[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    return ".png"
