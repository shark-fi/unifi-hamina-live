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

log = logging.getLogger("unifi_hamina_live.refresh")


class OpenIntentRefresher:
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.last_run: dict = {"ran_at": None, "ok": None, "output": None, "error": None}

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
            else:
                log.warning("openintent refresh failed (%s)", proc.returncode)
        except Exception as exc:  # pragma: no cover - defensive
            self.last_run = {
                "ran_at": time.time(), "ok": False, "output": None,
                "error": repr(exc),
            }
            log.exception("openintent refresh crashed")
        return self.last_run

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

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task
            self._task = None
