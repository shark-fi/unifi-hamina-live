# Changelog

All notable changes to this project are documented here. The format is loosely
based on [Keep a Changelog](https://keepachangelog.com/), and this project
follows semantic versioning.

## [Unreleased] — LTE / 5G cells from an Open5GS core

A second live source. Point the bridge at an Open5GS core and every cell it is
talking to joins the same snapshot as the UniFi APs, reaching all four surfaces
unchanged — including the Catalyst facade, which puts a private 5G cell on a
Hamina Live map beside the Wi-Fi.

### Added

- **`cellular/` package** — reads an Open5GS NF metrics server: `/gnb-info`
  (5G cells), `/enb-info` (4G cells), `/ue-info` (UE-to-cell association) and
  `/pdu-info` (UE IP + DNN, from the SMF). Those dumpers arrived in **Open5GS
  2.7.7**; `/metrics` alone publishes core-wide totals only and can never say
  which UE is on which cell, so an older core degrades to a per-cell client
  count and no UE list, announced once in the log rather than silently.
- **Cells as access points, UEs as clients.** Marked `source="cellular"` on
  `AccessPoint`, so nothing downstream had to change and anything that reports
  hardware can still tell a real AP from a costumed cell.
- **`cells.json`** ([`cells.example.json`](cells.example.json)) — band, ARFCN,
  bandwidth, TX power and placement, because the core has never seen the radio.
  A spec with no `match` is a fallback for whatever gNB id turns up; a declared
  cell the core stops reporting goes **offline** rather than vanishing.
- **Placement by anchor.** Name a UniFi device already placed on the console's
  own floor plan and the cell rides on its live position — drag the anchor in
  UniFi and the cell moves, with nothing to edit and nothing to re-import.
  Explicit plan + pixels also supported.
- **`GET /api/cellular`** — the one endpoint that separates the real (carrier,
  bandwidth, TX power, identity, attached UEs) from the costume (Wi-Fi band,
  channel, hardware model), because every other surface deliberately presents a
  cell as an access point.
- **`Radio.technology` / `carrier_mhz` / `carrier_label`** — the true carrier
  kept beside the Wi-Fi channel it wears, not replaced by it. Additive; Wi-Fi
  radios keep their defaults.
- **Open5G2GO support** (`OPEN5G2GO_URL`) — reads
  [that project's](https://github.com/Waveriders-Collective/open5G2GO) own
  backend instead of the core, and gets strictly more: its SNMP layer against
  the eNodeB supplies **band, EARFCN, bandwidth, TX power, PRB utilisation** and
  the radio's real MAC, serial, model and firmware, and its connection list
  supplies device names from the subscriber database. `cells.json` then shrinks
  to placement plus the model costume. PRB utilisation lands on the radio as
  `channel_utilization_pct` — a real load measurement, not a costume. It cannot
  say which cell a UE is on (it tracks one radio), so with several live cells it
  refuses to guess and says so; use the core's own endpoints there.
- Live radio values override declared ones **field by field**, so a deployment
  that reads band and EARFCN but not TX power keeps the declared power.
- A **fallback spec no longer renames** the cells it catches — it describes the
  radio, not its identity, and relabelling an estate to one string was wrong.
- [`docs/OPEN5GS.md`](docs/OPEN5GS.md) — setup, the map-placement walkthrough,
  the Cisco-model requirement for the Hamina import, and what this demo is
  actually arguing to Hamina.

### Notes

- Client **signal strength is deliberately empty** for UEs. A core never sees
  the air; an invented RSSI is the one thing that would make a heatmap actively
  wrong rather than merely costumed.
- SUPIs are masked to PLMN + last four by default (`OPEN5GS_MASK_SUPI`).
- Hamina's Catalyst connector resolves models against Cisco hardware only, so a
  cell must declare a Cisco AP model to import — per-cell in `cells.json`,
  which leaves real UniFi APs reporting their real models.

## [0.3.0] — Catalyst maps/export + Assurance layer; Live vendor sync ruled out

Pushed the Catalyst facade all the way through the vendor-sync gate, matching a
real appliance field-for-field — and established, honestly, that the final
Live-sync step is not reproducible with a UniFi-backed facade.

### Added / fixed

- **`maps/export` archive flow** — real task-based BAPI: `POST` with
  `Content-Type: text/plain` (filename body) → **202** `{response:{taskId,url}}`;
  the done task carries the download path in **`data:"/file/{fileId}"`**; archive
  served under `/file`, `/api/v1/file`, `/dna/intent/api/v1/file`. Captured from a
  real appliance. Hamina downloads the archive.
- **v2 floors + `accessPointPositions`** — matched field-for-field (`nameHierarchy`,
  UUID ids, `radios` = `id/bands/antenna`).
- **Assurance `networkDevices`** — the complete **94-field `values`** object and
  **28-field `radios`** object matched exactly to a live capture; sub-objects
  (`radios`/`neighbors`/`connectedNetworkDevice`) **field-gated per query** as a
  real box does; response **scoped to the queried floor** via the `sites` filter so
  assurance and `accessPointPositions` agree on device counts.

### Known limitation

- **Catalyst *Live* vendor sync does not complete** even with every response
  matched field-for-field, floor-scoped, and returning HTTP 200. It depends on a
  fuller Catalyst topology (WLC, uplink switches, cross-family device graph) a
  facade can't reproduce. See issue #1 (closed, not planned).
- **Use the OpenIntent path** (`unifi-hamina-export` → Hamina Simulation) for the
  working UniFi → Hamina pipeline. Recommended: `CATALYST_ADVERTISE_FLOOR_MAPS=false`.

## [0.2.0] — Cisco Catalyst (DNA) Center facade

This release adds a **Cisco Catalyst Center (DNA Center) Intent-API facade** — a
second way to bridge live UniFi telemetry toward Hamina, alongside the existing
Meraki-compatible API and OpenIntent refresh. It's a from-scratch DNA Center
emulation, backed by the live UniFi snapshot, that a real Hamina "Cisco Catalyst
(DNA) Center API" integration connects to and walks through hierarchy discovery.

### Added

- **Catalyst Center facade** (`/dna/*`), enabled with `catalyst_enabled` +
  `catalyst_username` / `catalyst_password`:
  - **Auth** — `POST /dna/system/api/v1/auth/token` (Basic → Token, `X-Auth-Token`
    on subsequent calls).
  - **Site hierarchy** — `GET /dna/intent/api/v2/site` (and v1), projecting
    `Global → UniFi (area) → <site> (building) → <floor>`, matched field-for-field
    to a real Catalyst 2.3.7.x appliance (`groupNameHierarchy` / `groupHierarchy`,
    bare root, correct `additionalInfo` namespaces). Hamina's Area/Building/Floor
    pickers populate from live UniFi data.
  - **Devices** — `network-device`, `network-device/count`, `device-detail`, and
    `wireless/accesspoint-configuration/summary` (radios: channel, width, TX
    power; x/y placement in metres).
  - **Maps export** — `maps/export` task-based async BAPI + a
    `CiscoUnifiedInterchange` map-archive builder (floor image + geometry),
    byte-matched to a real Hamina Catalyst export. (See Known limitations.)
  - **Request capture** — `GET /catalyst/_captured` records every request
    (matched or not) for the observe-and-match workflow.
- **Model mapping** — `UAPA6A6` → U7 Pro Outdoor.
- **CI** — published images now also carry the verbatim git tag (e.g. `v0.2.0`)
  in addition to the semver / `sha-` tags.
- **Docs** — `docs/CATALYST.md`: full write-up of the facade, the verified flow,
  and the maps/export blocker.

### Known limitations

- **Catalyst *live* sync is blocked on Hamina's side.** Hamina's connector
  requires a successful `maps/export` **image download** before it will sync AP
  data, and that download step can't be reproduced against a facade without a
  real Catalyst appliance to observe. Auth, hierarchy, and device shapes all
  work; the sync stalls on the mandatory map export. Full detail and repro in
  [#1](https://github.com/shark-fi/unifi-hamina-live/issues/1).
- **Recommended pipeline:** use the **OpenIntent export** (companion
  `unifi-hamina-export`) for the floor plan + AP placement, kept current by the
  scheduled refresher (`openintent_refresh_enabled` / `openintent_refresh_seconds`
  + stale-map detection). This is a working near-live UniFi → Hamina path today.
  See [#2](https://github.com/shark-fi/unifi-hamina-live/issues/2).

### Notes

The facade is a faithful, tested DNA Center skeleton — if Hamina relaxes the
mandatory map export, or a real `maps/export → task → file` capture becomes
available, completing the live path is a one-field change in
`catalyst/maps.py`.

## [0.1.0] — Initial release

- **Live UniFi collector** — background poll loop producing an immutable,
  normalized snapshot (APs, radios, clients, sites, floor plans).
- **Meraki-compatible facade** — Meraki Dashboard API v1-shaped endpoints so
  Hamina's Meraki connector can read live UniFi telemetry.
- **Vendor-neutral REST API** (`/api`) + a live dashboard (`/`).
- **Live AP placement** — legacy Maps + InnerSpace floor-plan x,y collected
  live, so AP moves flow through the API without an OpenIntent rebuild.
- **Scheduled OpenIntent refresh** — regenerate the import artifact on an
  interval, with stale-map detection (flag + notify, optional auto-regenerate).
- **One-command install script + systemd**, Docker multi-arch (amd64/arm64)
  GHCR publish, optional Cloudflare-tunnel exposure, tests, docs, and CI.

[0.2.0]: https://github.com/shark-fi/unifi-hamina-live/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/shark-fi/unifi-hamina-live/releases/tag/v0.1.0
