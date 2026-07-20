# Catalyst Center (DNA Center) facade — get UniFi into Hamina today

Unlike the Meraki connector (fixed Region dropdown, cloud-only, cert-pinned),
Hamina's **Cisco Catalyst (DNA) Center API** connector accepts:

- a free-text **Instance URL**,
- a **username / password**, and
- **Use self-signed certificate** / **Disable TLS verification** checkboxes.

That means it can be pointed at *this bridge*. This facade speaks the DNA Center
Intent API (auth token + Intent endpoints) backed by live UniFi data, so Hamina
can pull UniFi APs, floor plans, and placement as if talking to a Catalyst
Center appliance — **no change needed from Hamina**.

DNA Center's placement model (AP x,y in **metres** on a floor of known
width/length) also maps cleanly from the bridge's placement layer
(`x_px × metres_per_px`), so positions come through natively — no fake geo
coordinates.

## Why there's a request logger

The exact set of endpoints (and fields) Hamina calls depends on the DNA Center
API **version** it targets. Rather than guess, the facade records every `/dna/*`
request — matched or not — so you can see precisely what Hamina needs and
implement the remainder to match. Any endpoint not yet implemented returns a
DNA-Center-shaped 404 and is flagged in the log.

## Setup

1. Enable the facade and set the credentials Hamina will use (`.env`):
   ```ini
   CATALYST_ENABLED=true
   CATALYST_USERNAME=hamina
   CATALYST_PASSWORD=<a strong password>
   CATALYST_LOG_REQUESTS=true
   ```
2. Expose the bridge so Hamina's cloud can reach it (it connects *out* to your
   Instance URL). Cloudflare Tunnel or a port-forward both work — see
   [EXPOSURE.md](EXPOSURE.md). With "Disable TLS verification" you don't even
   need a valid cert. Allowlist Hamina's egress IPs (their docs link on the
   connect screen).
3. In Hamina: **Integration settings → Cisco Catalyst (DNA) Center API**:
   - **Instance URL** = your bridge URL (e.g. `https://unifi-bridge.example.com`)
   - **username / password** = the `CATALYST_*` values above
   - tick **Use self-signed certificate** / **Disable TLS verification** if needed
   - **Continue**.

## Read what Hamina called

After Hamina connects, inspect the capture buffer:

```bash
curl -s localhost:8080/catalyst/_captured | jq
# -> { "count": N, "requests": [ {method, path, query, status, implemented, authenticated}, ... ] }
curl -s "localhost:8080/catalyst/_captured?clear=true"   # reset between attempts
```

Entries with `"implemented": false` are the endpoints to add next. Send those
paths over and they get mapped to the live snapshot + placement layer.

## Implemented so far

- `POST /dna/system/api/v1/auth/token` — Basic-auth → `{ "Token": … }`; all
  Intent calls require the resulting `X-Auth-Token`.
- `GET /dna/intent/api/v1/site` and `/site/count` — Global → Building (UniFi
  site) → Floor (floor plan, with `mapGeometry` width/length in metres).
- `GET /dna/intent/api/v1/membership/{siteId}` — APs on a building/floor.
- `GET /dna/intent/api/v1/network-device` and `/count` — APs as Unified APs.
- `GET /dna/intent/api/v1/device-detail` — incl. floor + x,y placement.
- `GET /dna/intent/api/v1/wireless/accesspoint-configuration/summary?key=<mac>`
  — radios: channel, width, TX power; plus floor placement in metres.

Everything else under `/dna/*` is captured and returns a 404 until implemented.

## Verified against Hamina Live (Catalyst Center connector)

Pointing Hamina's "Cisco Catalyst (DNA) Center API" integration at the bridge,
the following are confirmed working end-to-end against live UniFi data:

- **Connect / auth** — Instance URL + username/password (the `catalyst_*`
  settings), TLS-verify off.
- **Site discovery** — Hamina walks `GET /dna/intent/api/v2/site` by
  `type=area|building|floor`. The bridge exposes the hierarchy
  `Global → UniFi (area) → <site> (building) → <floor>`, matched field-for-field
  to a real 2.3.7.x appliance (`groupNameHierarchy` / `groupHierarchy`, bare
  root, no `systemGroup`). Hamina's Area/Building/Floor pickers populate.
- **Live AP telemetry** — model, TX power, channels, and x/y placement flow via
  the `network-device` / `device-detail` / `accesspoint-configuration`
  endpoints.

### Known limitation: floor-map image auto-import

`POST /dna/intent/api/v1/maps/export/{floorId}` is implemented as the real
task-based async BAPI (submit → poll `GET /task/{id}` → download
`GET /file/{id}` returning a `CiscoUnifiedInterchange` `.tar.gz` with the floor
image + geometry, matched to a real Hamina Catalyst export). The submit and the
task poll work, and the task reports done with the fileId in `progress`, `data`,
and `additionalStatusURL` — but **Hamina polls the task to timeout and never
issues the file download**, so the background *image* does not import
automatically. The exact completion/download signal Catalyst's maps service
uses could not be reproduced without a real appliance to observe (the DevNet
sandbox denies maps permissions).

**Workaround — manual one-time map add.** The hierarchy and live AP data import
fine; only the floor *image* is manual. In Hamina Planner open the floor and
upload the plan image (the bridge already holds it — it is served inside the
maps/export archive, and `GET /catalyst/_captured` / the collector cache expose
it), then calibrate scale to the floor width the bridge reports (metres =
`width_px × meters_per_px`). Live AP positions then overlay on it. If a real
Catalyst maps/export task capture becomes available, finishing the auto-import
is a one-field change in `catalyst/maps.py:task_response`.

## Status

This is a **skeleton for the observe-and-match loop**, not a certified DNA
Center emulation. The auth flow, site hierarchy, and device endpoints are real
and tested; the maps/export archive delivery is the one piece still pending a
real-appliance capture. Model strings map UniFi → a plausible `Unified AP`; the
true UniFi model is preserved in the fields.
