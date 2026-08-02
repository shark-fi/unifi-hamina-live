# UniFi Live for InnerSpace

A Chrome (MV3) extension that overlays **live UniFi Network clients and AP
telemetry onto the InnerSpace floor plan**, inside the UniFi console. InnerSpace
is a planning view and shows no live clients; this fills that gap.

It is **read-only and same-origin**: the content script runs on your console and
reads the console's own Network API with your logged-in session — no bridge, no
cloud, no CORS, no CSRF, GETs only.

- `GET <base>/proxy/network/api/s/<site>/stat/device` — live AP state
- `GET <base>/proxy/network/api/s/<site>/stat/sta` — clients (joined to APs by MAC)

`<base>` and `<site>` are derived from the URL, so it works both on a local
console (`https://192.168.1.1/network/…`) and via Ubiquiti remote access
(`https://unifi.ui.com/consoles/<id>/network/…`).

## What it shows

- **Client icons** ringed around each AP — UniFi's own fingerprint icon for the
  device where it has one, otherwise a glyph inferred from name/vendor, or an
  initial. The ring outline is coloured by radio band and guests are dashed.
  Every client is drawn (no `+N` chip): ring capacity comes from circumference,
  with a clear wedge beneath the marker so the AP's own name and chips stay
  readable. Rings never reach past a fixed radius — a busy AP packs tighter and
  its chips shrink rather than throwing a ring across the floor plan.
- **Hover a client** for its name, SSID, band and channel, signal, TX/RX and IP;
  **click** for a details card adding MAC, SNR, data volume, uptime, vendor and
  guest status.
- **Per-band client chips** (2.4 / 5 / 6 GHz) styled to match UniFi's own chips.
  Click one to filter the ring to that radio.
- **Channel chips with Utilization / TX Retries**, reproduced from
  `radio_table` / `radio_table_stats` on consoles that don't render their own —
  and suppressed on the ones that do, so nothing is duplicated or covered.
- **Hovering a channel chip spotlights that radio** — its clients stay lit and
  the other bands drop back. Works on UniFi's own chips too: they carry only a
  channel number, so the band is resolved against the radios we already poll.
- **Chips scale with the map**, tracking the zoom InnerSpace applies to its own
  markers. Client icons stay a fixed size so they remain readable zoomed out.
- A **status chip** bottom-left with per-band totals, how many clients resolved
  to a real icon, and a build tag.

## How it pins to the map

InnerSpace draws the floor plan on a WebGL `<canvas>` (`three.js`) — we can't
inject into that. But it also renders each AP's label as a DOM
`<section data-testid="stats-tooltip-*">` whose CSS `transform` it **keeps in
sync with the canvas as you pan and zoom**. The overlay reads those live screen
positions straight from the DOM (via `getBoundingClientRect`) and pins client
bubbles to them on `requestAnimationFrame` — so the icons follow the APs through
pan/zoom with no coordinate math and no WebGL hooking. Client data comes from
the Network API, joined to each AP by name.

Clients have no vendor-supplied x,y — UniFi, like every non-Mist vendor, reports
them per-AP — so the rings show *which AP a client is on*, not where it is
standing.

## Where it works

| Access path | Works | Why |
|---|---|---|
| Console on its LAN address (`https://192.168.x.x/...`) | ✅ | API is same-origin |
| `unifi.ui.com` with an HTTP proxy path (`/consoles/<id>/proxy/network/...`) | ✅ | API is reachable over HTTP |
| `unifi.ui.com` with a **WebRTC-relayed** session | ❌ | No HTTP API exists to call |

Some remote sessions don't proxy the console over HTTP at all — they tunnel it
through a **WebRTC data channel** (UniFi's own telemetry calls this
`Rtc-Cloudflare` / `Ok-Relay`, negotiated via `RTCSignaling`). In that mode the
page issues no request to the console's API; the `<console-id>.id.ui.direct`
host it contacts serves only the SSO handshake and answers API paths with the
app shell. There is nothing for an extension to fetch, short of re-implementing
UniFi's signalling and speaking the data channel.

The overlay detects this and says so, rather than reporting a path error. Open
the same console on its **LAN address** and it works normally.

## Status

Confirmed working against two live consoles: a 3-AP site over an HTTP-proxied
`unifi.ui.com` session, and a 14-AP site on its LAN address showing 57 clients
(2.4 GHz 10 · 5 GHz 35 · 6 GHz 12). The status chip reports what it resolved
(and any failure) so problems are diagnosable from the page rather than by
guesswork.

## Load it (unpacked)

1. Chrome → `chrome://extensions` → enable **Developer mode**.
2. **Load unpacked** → select this folder.
3. Click the extension's icon → enter your **Console URL** (the origin, e.g.
   `https://192.168.1.1` or `https://unifi.ui.com`) → **Enable**. Approve the
   host-permission prompt.
4. Open the **InnerSpace** floor plan. Client icons appear pinned around each AP
   with per-band chips; a status chip sits bottom-left. Click an icon for
   details, click a band chip to filter.

**Disable** from the popup and reload the console tab to remove it.

## How it's wired

- `manifest.json` — MV3; `optional_host_permissions` only (no broad grant up
  front, and no `webRequest`).
- `src/popup.js` — captures the console origin, requests host permission (a user
  gesture), asks the worker to register, and injects into the current tab so it
  works without a reload.
- `src/background.js` — registers/unregisters the content script and probe for
  `<origin>/*` via `chrome.scripting.registerContentScripts`, and fetches on the
  content script's behalf when the API lives on a different origin.
- `src/probe.js` — a page-context probe (`world: "MAIN"`) that wraps
  `fetch` / `XHR` / `WebSocket` **purely to read request URLs**, so the console's
  API prefix can be discovered. It always calls the originals through and reads
  no bodies or responses.
- `src/content.js` — resolves the API base/site, polls for clients and radio
  state, and renders the overlay.

Client icons come straight from UniFi's CDN at
`https://static.ui.com/fingerprint/0/<dev_id>_101x101.png`, keyed by the
fingerprint id that `stat/sta` already returns — no lookup table and no extra
request. Ids UniFi hasn't fingerprinted, or that have no artwork, fall back to
a glyph. The status chip reports how many clients resolved to a real icon.

API-path discovery layers the page-context probe, a `PerformanceObserver`, a
persisted known-good base, and candidates derived from the URL, accepting both
v1 and v2 API shapes. State resets on console/site switch, the render loop
survives and reports errors rather than dying, and the overlay self-heals if it
notices it is holding another console's data.

[`docs/OVERLAY_EXTENSION.md`](docs/OVERLAY_EXTENSION.md) has the fuller design
notes, including two related overlay targets that build on the same technique.

## Privacy / safety

No data leaves your browser or your console. No analytics, no external hosts,
no telemetry. The extension only ever reads; it never writes to the console.

## Related

- [`unifi-hamina-export`](https://github.com/shark-fi/unifi-hamina-export) —
  export UniFi floor plans, AP placements and walls to OpenIntent for Hamina,
  and import a Hamina plan back into InnerSpace.
- [`unifi-hamina-live`](https://github.com/shark-fi/unifi-hamina-live) — the
  live bridge and dashboard this extension was scoped alongside.

## Disclaimer

Independent and unofficial; not affiliated with, endorsed by, or supported by
Ubiquiti Inc. UniFi and InnerSpace are trademarks of their respective owners.

The UniFi Network and InnerSpace endpoints it reads are **undocumented internal
APIs**, determined by observing the console's own web application in order to
interoperate with it. They carry no stability guarantee and may change in any
UniFi release — if an update breaks this, that is expected, not a defect on
Ubiquiti's part.

Use it on equipment you own or are authorised to administer. It reads only what
your own logged-in session can already see, and offers no way to reach a console
you cannot already log in to. As stated in the [license](LICENSE), the software
is provided "as is", without warranty of any kind.

## License

[MIT](LICENSE) © 2026 SharkFi
