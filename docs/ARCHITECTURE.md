# Architecture

One background poller, one immutable snapshot, three read-only projections.

```
                    ┌────────────────────────────────────────────┐
                    │              FastAPI app                    │
   UniFi console    │                                             │
  (UniFi OS /       │   ┌──────────────┐   Snapshot (in memory)   │
   classic ctrl)    │   │  Collector   │──────────┐               │
        ▲           │   │  poll loop   │          ▼               │
        │  GET      │   └──────────────┘   ┌──────────────┐       │
        │ stat/device   │ every N s        │  /api/v1     │  Meraki-compatible
        │ stat/sta  │   ▲                  │  (facade)    │  ── X-Cisco-Meraki-API-Key
        │ self/sites│   │ UniFiClient      ├──────────────┤       │
        └───────────┼───┘ (httpx, async)  │  /api        │  neutral REST + dashboard
                    │                      ├──────────────┤       │
                    │   OpenIntentRefresher│  /openintent │  scheduled zip
                    │   (subprocess) ──────┤  (optional)  │       │
                    │                      └──────────────┘       │
                    └────────────────────────────────────────────┘
```

## Data flow

1. **`Collector`** (`unifi/collector.py`) runs an asyncio loop. Each tick it
   builds a fresh `UniFiClient`, logs in, and reads `self/sites`, `stat/device`
   and `stat/sta` per site.
2. Raw payloads pass through **`unifi/normalize.py`** into neutral
   `AccessPoint` / `Radio` / `Client` / `Site` models (`models.py`). The model
   map and radio parsing mirror the companion `unifi_export.py`.
3. The result is one immutable **`Snapshot`**, swapped in under a lock. If a poll
   fails, the last good snapshot is retained and `ok=False` + `error` are set.
4. Every endpoint is a **pure projection** of the current snapshot:
   - `meraki/router.py` + `meraki/mapping.py` → Meraki v1 shapes.
   - `api/router.py` → neutral JSON + the dashboard's data.
   Because all three read the same snapshot, they can never disagree.

## Why this shape

- **Poll-and-cache, not per-request fetch.** The console is hit once per
  interval regardless of API traffic, so a busy dashboard or a chatty Meraki
  client never hammers UniFi, and every reader sees a consistent instant.
- **Immutable snapshot.** No partial state is ever visible; a swap is atomic
  from a reader's perspective.
- **Failure isolation.** Poll errors are contained in the collector; the HTTP
  surface stays up and serves the last good data with a clear health signal.
- **Injectable collector.** `create_app(collector=...)` lets tests supply a
  `FakeCollector` with canned snapshots — the whole API is testable without a
  network.

## Three independent data layers

It helps to see the data as three layers with different change rates and
transports — they are deliberately decoupled:

| Layer | Source | Transport | Consumed as |
|---|---|---|---|
| **Telemetry** (clients, radio channel/power, AP up/down) | `stat/device`, `stat/sta` | poll + optional WebSocket push | live API / dashboard |
| **Placement** (floor plans, AP x,y) | classic Maps (`stat/device`) or InnerSpace | poll (`unifi/placement.py`) | `/api/floorplans`, AP `x`/`y`, Meraki `floorPlans` |
| **Import bundle** (floor-plan images + geometry) | the OpenIntent exporter | subprocess, once or scheduled | OpenIntent zip |

Why this matters: **telemetry never touches OpenIntent**, and the WebSocket only
accelerates the telemetry layer — so live client/radio churn never triggers a
zip rebuild.

**Placement is now its own live layer.** Each poll the collector reads AP
positions — for free from `stat/device` (`map_id`, `x`, `y`) on classic Maps, or
from the InnerSpace project (converted with the exporter's exact
`scene_to_pixels` math; image dimensions are fetched once and cached). Positions
land on `AccessPoint.floorplan_id`/`x`/`y` and floor plans in
`Snapshot.floorplans`, exposed via the neutral API and the Meraki `floorPlans`
endpoint. So **an AP move is a snapshot update a live consumer sees on the next
poll** — no OpenIntent regeneration.

The **OpenIntent zip is now only needed for the initial import** (the
floor-plan *images* + geometry, which Hamina can't get from the placement feed).
Set `OPENINTENT_REFRESH_SECONDS=0` to generate it once at startup and then rely
on live positions. Floor-plan *images* still come from the exporter, so its
InnerSpace/image parsing stays the single source of truth there; the bridge
duplicates only the lightweight coordinate math it needs for live positions.

## The OpenIntent refresher

`refresh/openintent.py` is independent of the live poll. It shells out to the
companion `unifi_export.py` on its own interval so all floor-plan / placement /
OpenIntent-zip logic stays in one place rather than being duplicated here. The
generated zip is served at `/openintent/latest.zip`. See
[HAMINA.md](HAMINA.md) for why this is the path that works with Hamina today.

## Module map

| Module | Responsibility |
|---|---|
| `config.py` | env/`.env` settings |
| `models.py` | neutral data models + snapshot helpers |
| `unifi/client.py` | async UniFi HTTP client (login/CSRF/TLS, GET helpers, WS URL/auth) |
| `unifi/normalize.py` | raw UniFi payload → neutral models |
| `unifi/collector.py` | poll loop + snapshot ownership + WS event application |
| `unifi/placement.py` | pure AP-position transforms (classic Maps + InnerSpace, tested) |
| `unifi/events.py` | pure WebSocket-event → snapshot mutations (tested) |
| `unifi/websocket.py` | experimental WS listener (push updates, off by default) |
| `meraki/mapping.py` | neutral → Meraki v1 JSON |
| `meraki/router.py` | Meraki-compatible endpoints + auth |
| `api/router.py` | neutral REST endpoints |
| `refresh/openintent.py` | scheduled exporter subprocess |
| `refresh/router.py` | OpenIntent status/download endpoints |
| `deps.py` | FastAPI dependencies (snapshot, settings, auth) |
| `app.py` | app factory + lifespan wiring |
```
