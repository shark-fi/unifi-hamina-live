"""Catalyst Center Maps export: the CiscoUnifiedInterchange map archive.

Hamina's importer, after resolving the site hierarchy, exports a floor's map by
POSTing to ``/dna/intent/api/v1/maps/export/{floorId}`` and expects the map
archive **synchronously in the response body** (it makes no follow-up poll — a
real appliance streams the archive back on this call).

The archive is Cisco's map-interchange format, verified byte-for-byte against a
Hamina Catalyst export:

    images/<name>.png                 one image per floor
    xmlDir/MapsImportExport.xml        <ns0:CiscoUnifiedInterchange> hierarchy

    <?xml version="1.0" encoding="UTF-8"?>
    <ns0:CiscoUnifiedInterchange xmlns:ns0="http://importexport.cisco.com/1.0"
        ver="1.0" source="..." angleUnits="DEGREE" distUnits="FEET"
        ssUnits="dBm" createdOn="<ms>" lastUpdated="<ms>">
      <ns0:Maps>
        <ns0:Site name="...">
          <ns0:Building name="...">
            <ns0:Floor name="..." level="1">
              <ns0:Dimension width="..." length="..." height="..."/>  (FEET)
              <ns0:ImageInfo imageName="....png" imageType="PNG"/>
            </ns0:Floor>
          </ns0:Building>
        </ns0:Site>
      </ns0:Maps>
    </ns0:CiscoUnifiedInterchange>

Dimensions are in feet (distUnits="FEET"); our geometry is metric, so we
convert. APs are not part of this archive — Hamina reads live AP data from the
device endpoints after the floor image is in.
"""

from __future__ import annotations

import io
import json
import logging
import tarfile
import time
import uuid
from xml.sax.saxutils import quoteattr

from ..models import FloorPlan, Snapshot
from . import mapping

log = logging.getLogger("unifi_hamina_live.catalyst.maps")

_M_TO_FT = 3.280839895
_CEILING_M = 2.5  # matches Hamina's default 8.2021 ft ceiling
_JOB_NS = uuid.UUID("6f5c9e2a-3333-4000-8000-000000000000")


class MapExportJobs:
    """Registry of maps/export async tasks (taskId/fileId -> floor).

    Catalyst Center's maps export is the task-based async BAPI: POST returns a
    {taskId, url}; the client polls GET /task/{taskId} which reports completion
    and a file id; the client then downloads GET /file/{fileId}. Ids are
    derived deterministically from the floor so repeated exports are stable.
    """

    def __init__(self) -> None:
        self._by_task: dict[str, dict] = {}
        self._by_file: dict[str, dict] = {}

    def create(self, floor_id: str) -> dict:
        # Each export submission gets a FRESH completion stamp — the task must
        # appear to have finished *after* this submit, or the client decides it
        # hasn't completed yet and polls to timeout. (ids stay deterministic;
        # only the timestamp refreshes.) Stability across polls is guaranteed by
        # task_response reading this stored ts, not the wall clock.
        task_id = str(uuid.uuid5(_JOB_NS, "task:" + floor_id))
        file_id = str(uuid.uuid5(_JOB_NS, "file:" + floor_id))
        job = {"floor_id": floor_id, "task_id": task_id, "file_id": file_id,
               "ts_ms": int(time.time() * 1000)}
        self._by_task[task_id] = job
        self._by_file[file_id] = job
        return job

    def by_task(self, task_id: str) -> dict | None:
        return self._by_task.get(task_id)

    def by_file(self, file_id: str) -> dict | None:
        return self._by_file.get(file_id)


def submit_response(job: dict) -> dict:
    """The POST maps/export body: an async task handle (response.taskId + url)."""
    return {
        "response": {
            "taskId": job["task_id"],
            "url": f"/dna/intent/api/v1/task/{job['task_id']}",
        },
        "version": "1.0",
    }


def task_response(job: dict, delay_ms: int = 0) -> tuple[dict, bool]:
    """A DNAC file task, matched field-for-field to a real appliance (verified
    against a Command Runner task on the sandbox). Returns (body, done).

    For the first ``delay_ms`` after submit the task reports RUNNING (no endTime,
    no fileId) — a real maps/export takes seconds, and an instant-done task can
    trip a client that waits for the running->done transition. After that it
    reports DONE, with the fileId carried ONLY in ``progress`` as COMPACT JSON
    (``{"fileId":"..."}``); the client regex-extracts it and builds the
    /file/{fileId} URL itself. endTime/version/lastUpdate are the fixed
    completion stamp and equal each other, so every poll once done is identical.
    """
    start = job["ts_ms"]
    end = start + max(delay_ms, 250)
    done = int(time.time() * 1000) - start >= delay_ms
    resp = {
        "startTime": start,
        "serviceType": "Maps Service",
        "username": "admin",
        "isError": False,
        "instanceTenantId": mapping._TENANT,
        "id": job["task_id"],
    }
    if done:
        resp.update({
            "version": end,
            "endTime": end,
            "lastUpdate": end,
            "progress": json.dumps({"fileId": job["file_id"]}, separators=(",", ":")),
        })
    else:
        resp.update({
            "version": start,
            "lastUpdate": start,
            "progress": "CREATING MAP ARCHIVE",
        })
    return {"response": resp, "version": "1.0"}, done


def _ft(metres: float | None) -> str:
    return "0.0" if not metres else f"{metres * _M_TO_FT:.6f}"


def _floor(snap: Snapshot, floor_id: str) -> FloorPlan | None:
    return next((f for f in snap.floorplans if mapping.floor_id_for(f) == floor_id), None)


def _building_name(snap: Snapshot, fp: FloorPlan) -> str:
    s = next((s for s in snap.sites if s.id == fp.site_id), None)
    return s.name if s else fp.site_id


def _image_ext(blob: bytes | None) -> tuple[str, str]:
    """(extension, imageType) sniffed from the blob; defaults to PNG."""
    if blob and blob[:3] == b"\xff\xd8\xff":
        return ".jpg", "JPEG"
    return ".png", "PNG"


def build_maps_xml(snap: Snapshot, fp: FloorPlan, image_name: str,
                   image_type: str, created_ms: int) -> str:
    building = _building_name(snap, fp)
    w_m, l_m = mapping._metres_dims(fp)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<ns0:CiscoUnifiedInterchange xmlns:ns0="http://importexport.cisco.com/1.0"'
        ' ver="1.0" source="UniFi" angleUnits="DEGREE" distUnits="FEET"'
        f' ssUnits="dBm" createdOn="{created_ms}" lastUpdated="{created_ms}">\n'
        '  <ns0:Maps>\n'
        f'    <ns0:Site name={quoteattr(mapping._AREA_NAME)}>\n'
        f'      <ns0:Building name={quoteattr(building)}>\n'
        f'        <ns0:Floor name={quoteattr(fp.name)} level="1">\n'
        f'          <ns0:Dimension width="{_ft(w_m)}" length="{_ft(l_m)}"'
        f' height="{_ft(_CEILING_M)}"/>\n'
        f'          <ns0:ImageInfo imageName={quoteattr(image_name)}'
        f' imageType="{image_type}"/>\n'
        '        </ns0:Floor>\n'
        '      </ns0:Building>\n'
        '    </ns0:Site>\n'
        '  </ns0:Maps>\n'
        '</ns0:CiscoUnifiedInterchange>\n'
    )


def build_archive(snap: Snapshot, floor_id: str, image_bytes: bytes | None,
                  created_ms: int | None = None) -> bytes:
    """Build the CiscoUnifiedInterchange map archive (gzipped tar) for a floor."""
    fp = _floor(snap, floor_id)
    if fp is None:
        raise KeyError(floor_id)
    if created_ms is None:
        created_ms = int(time.time() * 1000)
    ext, image_type = _image_ext(image_bytes)
    image_name = f"{floor_id}{ext}"
    xml = build_maps_xml(snap, fp, image_name, image_type, created_ms).encode("utf-8")

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        _adddir(tar, "images")
        if image_bytes:
            _addfile(tar, f"images/{image_name}", image_bytes)
        _adddir(tar, "xmlDir")
        _addfile(tar, "xmlDir/MapsImportExport.xml", xml)
    return buf.getvalue()


def _addfile(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def _adddir(tar: tarfile.TarFile, name: str) -> None:
    info = tarfile.TarInfo(name=name)
    info.type = tarfile.DIRTYPE
    info.mode = 0o755
    tar.addfile(info)
