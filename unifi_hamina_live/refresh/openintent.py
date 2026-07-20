"""Background job that regenerates an OpenIntent zip on an interval.

Hamina Live can only *pull* from vendor clouds it already supports, and there is
no inbound/push API (see docs/HAMINA.md). Until Hamina adds UniFi natively or a
custom-endpoint option, the pragmatic "near-live" path is to re-run the
companion exporter on a schedule and re-import the fresh zip into a Hamina
Planner project.

This job shells out to ``unifi_export.py`` (from shark-fi/unifi-hamina-export)
so all the floor-plan / placement / OpenIntent logic stays in one place. It
never blocks the event loop — the subprocess runs via asyncio.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

from ..config import Settings
from ..unifi import placement

log = logging.getLogger("unifi_hamina_live.refresh")


class OpenIntentRefresher:
    def __init__(self, settings: Settings, collector=None) -> None:
        self._s = settings
        self._collector = collector  # source of live floor-plan structure
        self._task: asyncio.Task | None = None
        self._monitor_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.last_run: dict = {"ran_at": None, "ok": None, "output": None, "error": None}
        # staleness state: the exported baseline vs. current floor-plan structure
        self._baseline_sigs: dict | None = None
        self._need_baseline = False
        self.stale = False
        self.stale_since: float | None = None
        self.stale_detail: dict | None = None

    @property
    def output_zip(self) -> Path:
        return Path(self._s.openintent_output_dir) / "hamina-live.zip"

    def _command(self) -> list[str]:
        exporter = self._s.openintent_exporter_path
        # Use the interpreter running this server (the venv's Python) rather
        # than a bare "python3" from PATH, which may be missing under systemd.
        return [
            sys.executable, exporter, self._s.openintent_mode,
            "--host", self._s.unifi_host,
            "-u", self._s.unifi_username,
            "--openintent", str(self.output_zip),
            "-o", str(Path(self._s.openintent_output_dir) / "aps.csv"),
        ]

    async def run_once(self) -> dict:
        exporter = Path(self._s.openintent_exporter_path)
        if not exporter.exists():
            self.last_run = {
                "ran_at": time.time(), "ok": False, "output": None,
                "error": f"exporter not found at {exporter} "
                         "(set OPENINTENT_EXPORTER_PATH)",
            }
            log.warning(self.last_run["error"])
            return self.last_run

        Path(self._s.openintent_output_dir).mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        # unifi_export.py reads the password from --password or a prompt; pass it
        # via env so it never lands on the process argv / in `ps` output. The
        # exporter also honours UNIFI_PASSWORD when present.
        env["UNIFI_PASSWORD"] = self._s.unifi_password
        cmd = self._command() + (["--password", self._s.unifi_password]
                                 if self._s.unifi_password else [])
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            out, _ = await proc.communicate()
            ok = proc.returncode == 0
            self.last_run = {
                "ran_at": time.time(),
                "ok": ok,
                "output": (out or b"").decode(errors="replace")[-4000:],
                "error": None if ok else f"exporter exited {proc.returncode}",
            }
            if ok:
                log.info("openintent refresh wrote %s", self.output_zip)
                # re-baseline staleness against whatever the next good poll sees
                self._need_baseline = True
                self.stale = False
                self.stale_since = None
                self.stale_detail = None
            else:
                log.warning("openintent refresh failed (%s)", proc.returncode)
        except Exception as exc:  # pragma: no cover - defensive
            self.last_run = {
                "ran_at": time.time(), "ok": False, "output": None,
                "error": repr(exc),
            }
            log.exception("openintent refresh crashed")
        return self.last_run

    # -- staleness -----------------------------------------------------------
    def evaluate(self, floorplans) -> str | None:
        """Compare current floor-plan structure to the exported baseline.

        Pure state machine (no I/O), so it is unit-testable. Returns
        'became_stale', 'recovered', or None. AP x,y moves never affect this —
        only map-structure changes do (see placement.plan_signatures).
        """
        cur = placement.plan_signatures(floorplans)
        if self._need_baseline:
            self._baseline_sigs = cur
            self._need_baseline = False
            self.stale = False
            self.stale_since = None
            self.stale_detail = None
            return None
        if self._baseline_sigs is None:
            return None
        diff = placement.diff_signatures(self._baseline_sigs, cur)
        if placement.has_changes(diff):
            if not self.stale:
                self.stale = True
                self.stale_since = time.time()
                self.stale_detail = diff
                return "became_stale"
            self.stale_detail = diff  # keep the latest delta
        elif self.stale:
            self.stale = False
            self.stale_since = None
            self.stale_detail = None
            return "recovered"
        return None

    async def _notify_stale(self) -> None:
        summary = self.stale_detail or {}
        log.warning(
            "openintent import is STALE — floor plan structure changed "
            "(added=%s removed=%s changed=%s). Re-import %s into Hamina.",
            summary.get("added"), summary.get("removed"), summary.get("changed"),
            self.output_zip,
        )
        url = self._s.openintent_stale_webhook.strip()
        if not url:
            return
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(url, json={
                    "event": "openintent_stale",
                    "at": self.stale_since,
                    "detail": self.stale_detail,
                    "zip": str(self.output_zip),
                })
        except Exception as exc:  # best-effort
            log.warning("stale webhook failed: %s", exc)

    async def _monitor(self) -> None:
        interval = max(10.0, self._s.poll_interval_seconds)
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set() or self._collector is None:
                continue
            snap = self._collector.snapshot
            if not getattr(snap, "ok", False):
                continue
            action = self.evaluate(snap.floorplans)
            if action == "became_stale":
                await self._notify_stale()
                if self._s.openintent_auto_regenerate:
                    log.info("openintent: auto-regenerating after map change")
                    await self.run_once()

    async def _loop(self) -> None:
        interval = self._s.openintent_refresh_seconds
        # Initial import: always generate once at startup.
        await self.run_once()
        if interval <= 0:
            log.info("openintent: initial-import-only mode (positions flow live)")
            return
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                break
            await self.run_once()

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="openintent-refresh")
            if self._collector is not None:
                self._monitor_task = asyncio.create_task(
                    self._monitor(), name="openintent-stale-monitor"
                )

    async def stop(self) -> None:
        self._stop.set()
        for task in (self._task, self._monitor_task):
            if task:
                await task
        self._task = None
        self._monitor_task = None
