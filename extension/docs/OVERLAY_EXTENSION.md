# Browser-extension overlays — design & scoping

> This repo implements **Q2** below (live clients in InnerSpace). Q1 and the
> plan overlay are scoped here but not built; they use the same technique.

Three overlay targets, all using the **same shadow-DOM card injection** pattern
that Hamina's own extension uses (verified from its bundle: `attachShadow` + a
React/DOM root, anchored by a per-page `selector`/`resolveTarget`, re-anchored by
a `MutationObserver`). Hamina's extension injects a *flat card*, **not** a
pixel-aligned canvas overlay — so we do the same and sidestep the one genuinely
hard problem (tracking a live map's pan/zoom transform).

## The three targets

| # | Target | Shows | Bridge needed? | Difficulty |
|---|---|---|---|---|
| **Q2** | **Live clients *in* InnerSpace** (UniFi console) | UniFi live clients + AP telemetry on the InnerSpace floor | **No** — same-origin | **Lowest** — build first |
| Q1 | UniFi live data *on* Hamina Live (`*.hamina.com`) | the live data Hamina can't sync (clients/util/status) | Yes (or direct-to-console) | Medium |
| P | Hamina *plan* in InnerSpace | Hamina's planned APs / coverage | Yes (`/api/plan`) | Medium |

**Q2 is first** because it is completely self-contained.

## Why Q2 is the easy win — same-origin

InnerSpace (`/innerspace/`) and the UniFi Network API live on the **same console
origin**. A content script there runs with your logged-in session cookies, so it
reads everything it needs directly — no bridge, no CORS, no mixed content, no
extra auth. All GETs (no CSRF):

- `GET /proxy/innerspace/api/project?mode=2D` — plans, the `map` shape
  (`urlImage`, `position`, `scale`) and `device` shapes (`meta.mac`, `position`).
- `GET /proxy/network/api/self/sites` — site ids.
- `GET /proxy/network/api/s/<site>/stat/device` — AP live state (per-radio
  channel/TX/util, client counts).
- `GET /proxy/network/api/s/<site>/stat/sta` — clients, each with its `ap_mac`.

Join clients→AP by `ap_mac`, place APs from the InnerSpace `device` shapes, and
render the **live client map inside the console** — effectively the bridge's new
client-map view, injected into InnerSpace and fed straight from the console.
InnerSpace natively shows *no* live clients, so this adds exactly what's missing.

### In-map overlay — RESOLVED by the DOM capture
The InnerSpace map is a WebGL **`<canvas data-engine="three.js">`** (can't inject
into it) — but each AP label is a DOM `<section data-testid="stats-tooltip-*">`
(with `data-testid="title"`/`model`) whose CSS `transform` InnerSpace **keeps in
sync with the canvas through pan/zoom**. So we get a **pixel-true overlay for
free**: read each marker's live screen rect (`getBoundingClientRect`) and pin
client bubbles to it on `requestAnimationFrame`. No WebGL hooking, no scene→pixel
math, no floor-image fetch. AP positions come from the DOM; client counts from
the Network API, joined by name. Real path is
`/network/<site>/innerspace/<plan>` (local) or
`/consoles/<id>/network/<site>/innerspace/<plan>` (unifi.ui.com); API base/site
are derived from the URL.

## Coordinate transform (InnerSpace → pixels)

Reuse the exporter's read-direction transform (scene units, image-centre origin,
**y-up → flip on read**):

```
x_px = (x - map.position.x) / map.scale.x + image_width  / 2
y_px = image_height / 2 - (y - map.position.y) / map.scale.y
```

`image_width/height` come from the (same-origin) floor image once loaded.

## Host matching for LAN consoles

Consoles are on a LAN IP (`https://192.168.x.x`) or `unifi.ui.com`; a static
`content_scripts.matches` can't enumerate them and `https://*/*` is too broad.
**Plan:** `optional_host_permissions` + a popup where the user enters their
console URL; on save, request that origin and
`chrome.scripting.registerContentScripts` for `<origin>/innerspace/*`.

## Q1 & the plan overlay (later)

- **Q1 (live data on Hamina):** content script on `*.hamina.com` injects UniFi
  live data fetched via the **background service worker** (which fetches the LAN
  bridge and hands back JSON/data-URIs, bypassing the https-page mixed-content
  block). Data *panel* / per-AP badges = easy; dots pinned on Hamina's own map =
  needs Hamina's map transform (stretch). Lucky break: Hamina imported our
  OpenIntent, so it shares our pixel coordinate system.
- **Plan overlay (Hamina→InnerSpace):** bridge gains `GET /api/plan` (parse a
  Hamina OpenIntent export via `openintent_import.parse_openintent`) → planned
  APs/coverage/walls rendered in the card; a **plan⟷live toggle** is the payoff.

## Phases (reordered — Q2 first)

- **P0.** Scoping (this doc). ✔
- **P1 — Q2 skeleton.** Manifest, popup (console URL), dynamic content-script
  registration, background worker, live per-AP client counts from the
  same-origin APIs. ✔
- **P2 — Q2 map.** ✔ — superseded by P3: the DOM capture showed the markers are
  pan/zoom-synced DOM nodes, so there was no need to re-render the floor image in
  a card. Data is pinned to the console's own map instead.
- **P3 — Q2 in-map overlay.** ✔ — pixel-true overlay pinned to the
  `stats-tooltip-*` marker rects on `requestAnimationFrame`; client rings,
  per-band chips, channel/utilization/TX-retries chips, details card.
- **P4 — Q1.** Same-pattern card on `*.hamina.com` fed by the bridge via the
  background worker. *Not built.*
- **P5 — Plan overlay.** Bridge `GET /api/plan` + plan⟷live toggle; optional
  Hamina share-image embed; "Open in Hamina" deep link. *Not built.*

## Open questions

**Resolved:**

- *InnerSpace DOM snapshot* — captured. The map is a WebGL
  `<canvas data-engine="three.js">`; AP labels are DOM
  `<section data-testid="stats-tooltip-*">` nodes, pan/zoom-synced. See above.
- *Repo home* — Q2 lives in its own repo,
  [`unifi-innerspace-live`](https://github.com/shark-fi/unifi-innerspace-live),
  since it needs no bridge at all. Q1 and the plan overlay do depend on the
  bridge and would sit with it (or here, fed over the worker).

**Still open:**

- **Heatmap?** If Hamina's computed heatmap picture matters, add the Hamina
  share-link source in P5; otherwise vector plan-vs-live is enough.
- **Chrome only or Firefox too?** (MV3 is Chrome-first.)
- **Fingerprint icons.** UniFi's own client icons live at
  `https://static.ui.com/fingerprint/ui/icons/<uuid>_101x101.png`, but the
  console request that maps a client to its fingerprint uuid hasn't been located
  on the consoles tested — the overlay falls back to glyphs/initials.
