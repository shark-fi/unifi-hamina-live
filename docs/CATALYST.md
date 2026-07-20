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

## Status

This is a **skeleton for the observe-and-match loop**, not a certified DNA
Center emulation. The auth flow and the endpoints above are real and tested;
the remaining floor-map/placement endpoints are finalised from the captured
request log once Hamina is pointed at it. Model strings map UniFi → a plausible
`Unified AP`; the true UniFi model is preserved in the fields.
