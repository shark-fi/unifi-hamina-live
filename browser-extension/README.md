# UniFi Live for InnerSpace (browser extension)

A Chrome (MV3) extension that overlays **live UniFi Network clients and AP
telemetry onto the InnerSpace floor plan**, inside the UniFi console. InnerSpace
is a planning view and shows no live clients; this fills that gap.

It is **read-only and same-origin**: the content script runs on your console and
reads the console's own Network API with your logged-in session — no bridge, no
CORS, no CSRF, GETs only.

- `GET <base>/proxy/network/api/s/<site>/stat/device` — live AP state
- `GET <base>/proxy/network/api/s/<site>/stat/sta` — clients (joined to APs by MAC)

`<base>` and `<site>` are derived from the URL, so it works both on a local
console (`https://192.168.1.1/network/…`) and via Ubiquiti remote access
(`https://unifi.ui.com/consoles/<id>/network/…`).

See [`../docs/OVERLAY_EXTENSION.md`](../docs/OVERLAY_EXTENSION.md) for the full
design (this is target **Q2**; Q1 "live data on Hamina" and the Hamina-plan
overlay build on the same technique).

## How it pins to the map

InnerSpace draws the floor plan on a WebGL `<canvas>` (`three.js`) — we can't
inject into that. But it also renders each AP's label as a DOM
`<section data-testid="stats-tooltip-*">` whose CSS `transform` it **keeps in
sync with the canvas as you pan and zoom**. The overlay reads those live screen
positions straight from the DOM (via `getBoundingClientRect`) and pins client
bubbles to them on `requestAnimationFrame` — so the dots follow the APs through
pan/zoom with no coordinate math and no WebGL hooking. Client counts come from
the Network API, joined to each AP by name.

## Where it works

| Access path | Works | Why |
|---|---|---|
| Console on its LAN address (`https://192.168.x.x/...`) | ✅ | API is same-origin |
| `unifi.ui.com` with an HTTP proxy path (`/consoles/<id>/proxy/network/...`) | ✅ | API is reachable over HTTP |
| `unifi.ui.com` with a **WebRTC-relayed** session | ❌ | No HTTP API exists to call |

Some remote sessions don't proxy the console over HTTP at all — they tunnel it
through a **WebRTC data channel** (UniFi's own telemetry calls this
`Rtc-Cloudflare` / `Ok-Relay`). In that mode the page issues no request to the
console's API; the `<console-id>.id.ui.direct` host it contacts serves only the
SSO handshake and answers API paths with the app shell. There is nothing for an
extension to fetch, short of re-implementing UniFi's signalling and speaking the
data channel.

The overlay detects this and says so, rather than reporting a path error. Open
the same console on its **LAN address** and it works normally.

## Status

Confirmed working against two live consoles: a 3-AP site over an HTTP-proxied
`unifi.ui.com` session, and a 14-AP site on its LAN address showing 57 clients
(2.4 GHz 10 · 5 GHz 35 · 6 GHz 12). The status chip reports what it resolved
(and any failure) so problems are diagnosable from the page rather than by
guesswork.

Client icons currently fall back to device glyphs/initials — the status chip
says `icons none` because UniFi's fingerprint-database endpoint isn't among the
requests these consoles make, so there is nothing to discover it from.

## Load it (unpacked)

1. Chrome → `chrome://extensions` → enable **Developer mode**.
2. **Load unpacked** → select this `browser-extension/` folder.
3. Click the extension's icon → enter your **Console URL** (the origin, e.g.
   `https://unifi.ui.com` or `https://192.168.1.1`) → **Enable**. Approve the
   host-permission prompt.
4. Open the **InnerSpace** floor plan. Client bubbles appear pinned to each AP,
   with a count badge; a status chip sits bottom-left. Hover a bubble for the
   client's hostname/band/signal.

**Disable** from the popup and reload the console tab to remove it.

## How it's wired

- `manifest.json` — MV3; `optional_host_permissions` only (no broad grant up front).
- `src/popup.js` — captures the console origin, requests host permission (a user
  gesture), asks the worker to register, and injects into the current tab so it
  works without a reload.
- `src/background.js` — registers/unregisters `src/content.js` for `<origin>/*`
  via `chrome.scripting.registerContentScripts` (the script guards on the path).
- `src/content.js` — derives the API base/site from the URL, polls the Network
  API for clients-per-AP, and pins bubbles to the live AP marker DOM nodes.

## Privacy / safety

No data leaves your browser or your console. No analytics, no external hosts.
The extension only ever reads; it never writes to the console.
