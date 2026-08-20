# Catalyst Center (DNA Center) facade

> ## ⚠️ This works — but every AP has to claim to be Cisco
>
> The vendor sync **does** complete. APs land on a Hamina Live map with real
> MACs, per-radio channel, TX power, channel width, per-radio client counts,
> firmware, and Hamina's capacity analysis running on those numbers.
>
> The catch: the connector resolves an AP against **Cisco hardware only** — a
> bare Cisco model token, make implied by the connector. Every UniFi spelling was
> refused: UniFi's internal code (`U7PROMAX`), our slug (`u7-pro-max`, which is
> also Hamina's own bare `modelId`), Hamina's catalog display name
> (`U7 Pro Max`), and Hamina's fully-qualified catalog id
> (`ubiquiti:u7-pro-max`). Only `CW9166` — a Cisco token — works.
>
> ```bash
> CATALYST_MODEL_OVERRIDE=CW9166      # every AP reports as a Cisco 9166i
> ```
>
> So the telemetry is genuine and the **hardware identity is not**. Hamina's
> coverage simulation then runs on a CW9166's antenna pattern rather than your
> actual AP's: fine for live monitoring, **misleading for planning**, and it does
> not announce itself on the map.
>
> Ubiquiti *is* in Hamina's catalog (one of ~92 makes, whole U6/U7 line) — but
> that is the **planner** catalog, which is why OpenIntent imports place real U7
> hardware correctly. Vendor sync is a different resolver, keyed by the
> connector's make. A working Juniper Mist project in the same account showed the
> shape: `mapById.accessPoints` returns `"make": "juniperMist", "model": "ap41"`
> — stored separately, model a bare token.
>
> **Still failing:** client sync — `GET /dna/data/api/v1/clients` returns 200
> with well-formed clients and Hamina refuses it. Per-radio client *counts* work
> (they come from the assurance layer); the individual client list does not.
>
> For live UniFi data in Hamina *without* misrepresenting your hardware, use the
> browser extension instead — same telemetry, over Hamina's own map, APs
> correctly identified, no vendor integration required.

Unlike the Meraki connector (fixed Region dropdown, cloud-only, cert-pinned),
Hamina's **Cisco Catalyst (DNA) Center API** connector accepts:

- a free-text **Instance URL**,
- a **username / password**, and
- **Use self-signed certificate** / **Disable TLS verification** checkboxes.

That means it can be pointed at *this bridge* — the connector accepts it and the
facade answers correctly. This facade speaks the DNA Center Intent API (auth
token + Intent endpoints) backed by live UniFi data, so Hamina can pull UniFi
APs, floor plans, and placement as if talking to a Catalyst Center appliance —
**no change needed from Hamina**. The vendor sync completes; what it cannot do
is let those APs be UniFi — see the banner above.

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
# -> { "count": N, "requests": [ {at, method, path, query, status, implemented, authenticated}, ... ] }
# `at` is a unix timestamp — line it up against the timestamps in Hamina's
# own GraphQL error responses to see whether a failing mutation ever
# reached the facade at all.
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

### Resolved: the archive download works, and was never the blocker

An earlier version of this document stated that Hamina "never downloads the
archive" and gated the whole sync on a map export the facade could not satisfy.
That is **wrong**, and the request log disproves it: `maps/export` → task poll →
`GET /dna/intent/api/v1/file/{id}` all return 200, and both floors' archives are
fetched successfully.

Two things actually blocked the sync, and neither was Catalyst topology:

1. **Cloudflare Access.** Three routes here live outside `/dna/` —
   `POST /api/assurance/v2/networkDevices` (the device sync),
   `GET /api/v1/task/{id}` and `GET /api/v1/file/{id}`. An Access policy with a
   bypass scoped to `/dna` answered the device sync at the edge with an HTML
   login page, which Hamina reported as `VENDOR_API_SERVER_ERROR`. Extend the
   bypass to `/api/assurance` and `/api/v1`; see [EXPOSURE.md](EXPOSURE.md).
2. **A blind spot in this repo's own request log.** Its skip-list filtered
   `/api/` wholesale, so those three routes never appeared — the device sync
   looked, across six consecutive captures, as though Hamina never requested it.
   Fixed; the log now records every path the facade serves.

The lesson is worth keeping: a gap in the instrument reads as evidence about the
system, and it is very convincing.

### What is still open

Client sync. `GET /dna/data/api/v1/clients?siteId=<floorId>&type=Wireless`
returns 200 with well-formed clients and Hamina reports "Failed to synchronize
client information — An unexpected error occurred". Three payload shapes have
been tried, the last adding per-client coordinates after a working Juniper Mist
project was found to return them for 371 of 440 live clients. Per-radio client
**counts** work regardless — they arrive via the assurance layer — so what is
missing is the individual client list.

That is now a question for Hamina rather than a shape to guess at: what does the
connector require in that payload?

### The alternative that needs none of this

The browser extension (`extension/`) draws the same live UniFi telemetry over
Hamina's own map from the browser, joined by AP name, with your hardware
correctly identified and no vendor integration involved. If the Cisco-identity
trade-off above is unacceptable — and for planning work it should be — that is
the path to use.

## Status

Auth, the site hierarchy cascade, device endpoints, `maps/export` and its
archive download, `accessPointPositions` and the assurance device layer are all
working against live Hamina, matched to a real 2.3.7.x appliance. APs appear on a
Hamina Live map with genuine channels, TX power, widths, per-radio client counts
and capacity analysis — provided every AP declares a Cisco model
(`CATALYST_MODEL_OVERRIDE`). The individual client list is the one remaining gap.
