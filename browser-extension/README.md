# UniFi Live for InnerSpace (browser extension)

A Chrome (MV3) extension that overlays **live UniFi Network clients and AP
telemetry onto the InnerSpace floor plan**, inside the UniFi console. InnerSpace
is a planning view and shows no live clients; this fills that gap.

It is **read-only and same-origin**: the content script runs on your console and
reads the console's own APIs with your logged-in session — no bridge, no cloud,
no CORS, no CSRF, GETs only.

- `GET /proxy/innerspace/api/project?mode=2D` — floor image + AP positions
- `GET /proxy/network/api/self/sites` — site ids
- `GET /proxy/network/api/s/<site>/stat/device` — live AP state
- `GET /proxy/network/api/s/<site>/stat/sta` — clients (joined to APs by MAC)

See [`../docs/OVERLAY_EXTENSION.md`](../docs/OVERLAY_EXTENSION.md) for the full
design (this is target **Q2**; Q1 "live data on Hamina" and the Hamina-plan
overlay build on the same card pattern).

## Status — scaffold

Functional skeleton, **not yet run against a live console**. The card floats
bottom-right; its final anchor inside the InnerSpace layout (and whether client
dots can be injected straight into InnerSpace's own SVG) is pending a DOM capture
of the InnerSpace page. Field names in the join/transform are from the exporter
and captured project JSON and may need small tweaks against your Network version.

## Load it (unpacked)

1. Chrome → `chrome://extensions` → enable **Developer mode**.
2. **Load unpacked** → select this `browser-extension/` folder.
3. Click the extension's icon → enter your **Console URL** (e.g.
   `https://192.168.1.1`), optionally a **site id** (default `default`) → **Enable**.
   Approve the host-permission prompt.
4. Open the **InnerSpace** floor plan on that console. The live client map
   appears bottom-right; pick a floor, click an AP to list its clients.

**Disable** from the popup and reload the console tab to remove it.

## How it's wired

- `manifest.json` — MV3; `optional_host_permissions` only (no broad grant up
  front). No static content script.
- `src/popup.js` — captures the console origin, requests host permission (a user
  gesture), and asks the worker to register; injects into the current tab so it
  works without a reload.
- `src/background.js` — registers/unregisters `src/content.js` for
  `<origin>/innerspace/*` via `chrome.scripting.registerContentScripts`, and
  re-asserts on startup.
- `src/content.js` — fetches + joins the data, and renders the client map in a
  shadow-DOM card (style-isolated from the console).

## Privacy / safety

No data leaves your browser or your console. No analytics, no external hosts.
The extension only ever reads; it never writes to the console.
