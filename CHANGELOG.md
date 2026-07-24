# Changelog

All notable changes to this project are documented here. The format is loosely
based on [Keep a Changelog](https://keepachangelog.com/), and this project
follows semantic versioning.

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
