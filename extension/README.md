# UniFi Live for InnerSpace

A Chrome (MV3) extension that overlays **live UniFi Network clients and AP
telemetry onto the InnerSpace floor plan**, inside the UniFi console. InnerSpace
is a planning view and shows no live clients; this fills that gap.

It is **read-only**: GETs only, no writes, ever.

By default it needs nothing configured. The content script runs on your console
and reads the console's own Network API with your logged-in session — same
origin, no CORS, no CSRF, nothing to install:

- `GET <base>/proxy/network/api/s/<site>/stat/device` — live AP state
- `GET <base>/proxy/network/api/s/<site>/stat/sta` — clients (joined to APs by MAC)

`<base>` and `<site>` are derived from the URL, so this covers a local console
(`https://192.168.1.1/network/…`) and Ubiquiti remote access
(`https://unifi.ui.com/consoles/<id>/network/…`) alike.

Where that API can't be reached — see [Where it works](#where-it-works) — an
optional [`unifi-hamina-live`](https://github.com/shark-fi/unifi-hamina-live)
bridge can supply the same data from outside the page.

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

| Access path | On its own | With a bridge |
|---|---|---|
| Console on its LAN address (`https://192.168.x.x/…`) | ✅ | not needed |
| `unifi.ui.com` proxied over HTTP (`/consoles/<id>/proxy/network/…`) | ✅ | not needed |
| `unifi.ui.com` **WebRTC-relayed** | ❌ | ✅ |

Some remote sessions don't proxy the console over HTTP at all — they tunnel it
through a **WebRTC data channel** (UniFi's own telemetry calls this
`Rtc-Cloudflare` / `Ok-Relay`, negotiated via `RTCSignaling`). In that mode the
page issues no request to the console's API; the `<console-id>.id.ui.direct`
host it contacts serves only the SSO handshake and answers API paths with the
app shell.

**Which mode you get is UniFi's decision, not a setting** — not in this
extension, and not anywhere in the UniFi UI. It depends on whether the console
is reachable directly from your browser; the relay is the fallback when it
isn't. To tell which you're on: DevTools → Network, and look for any request to
a `/proxy/network/` path. Present, it's proxied. None at all, it's relayed.

What a relayed session still does is **render the map** — and the overlay pins
to the DOM markers InnerSpace draws, not to any API. So the markers were always
there; only the data was missing. It never had to come from that page.

## The bridge (optional)

[`unifi-hamina-live`](https://github.com/shark-fi/unifi-hamina-live) polls your
console over the LAN and serves neutral JSON. Point the extension at one and it
becomes a second source for exactly the data the page can't supply:

```
GET <bridge>/api/access-points   name, online, radios (channel, width, utilisation, TX retries)
GET <bridge>/api/clients         ap_mac, name, band, channel, signal, rates, dev_id, vendor
```

The console's own API stays **preferred** and is tried first. The bridge is used
when it fails, and directly when the session is relayed, where there is nothing
to try. The status chip says `via bridge` when the data came from there.

Set it in the popup's **Bridge URL** field and press **Test bridge** — it fetches
`/api/health` from the service worker, the same path the overlay uses, and
reports which stage failed rather than a bare failure.

A few things worth knowing:

- **It's stored per console.** One bridge instance polls one console, so pointing
  the wrong one at a plan produces a healthy-looking fetch whose AP names all
  miss. The popup keys the setting to the console in the active tab, and the
  overlay says so when the bridge covers a different one.
- **Reach it over HTTPS.** A LAN address (`http://192.168.1.50:8080`) works, but
  publishing the bridge through a **Cloudflare Tunnel** gives it a real
  certificate and works from anywhere — which is the point on a remote session.
  The scheme is guessed from the address: an IP or `localhost` gets `http`, a
  hostname gets `https`.
- **Put an Access policy in front of it.** The bridge's `/api` routes are
  unauthenticated by design, expecting a LAN. Tunnelled bare they publish your
  whole client inventory to anyone who learns the hostname. With Cloudflare
  Access, the extension needs no credential of its own: sign in once in a tab and
  the service worker reuses that session cookie. The bridge repo ships
  `scripts/check-tunnel.sh` to verify the policy actually took — see its
  `docs/EXPOSURE.md`.

## Status

Confirmed against three live consoles:

- a 14-AP site on its LAN address — 57 clients (2.4 GHz 10 · 5 GHz 35 · 6 GHz 12)
- a 3-AP site over an HTTP-proxied `unifi.ui.com` session
- a **relayed** `unifi.ui.com` session **through a tunnelled bridge** — 58 clients
  on 3 APs, 57 of them with UniFi's own fingerprint icons

That last one is the case this README used to call impossible. It isn't the
relay that changed: a relayed session still has no HTTP API. What changed is
where the data comes from.

The status chip reports what it resolved, which source it used, and any failure,
so problems are diagnosable from the page rather than by guesswork.

## Load it (unpacked)

1. Chrome → `chrome://extensions` → enable **Developer mode**.
2. **Load unpacked** → select this folder.
3. Open your console's tab, then click the extension's icon. **Console URL**
   prefills from that tab — it is the page you want the overlay drawn on, not a
   bridge. → **Enable**, and approve the host-permission prompt.
4. Open the **InnerSpace** floor plan. Client icons appear pinned around each AP
   with per-band chips; a status chip sits bottom-left. Click an icon for
   details, click a band chip to filter.
5. *Optional, and only if the console's own API can't be reached:* fill in
   **Bridge URL** and press **Test bridge**.

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
  state (falling back to a bridge), and renders the overlay.

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

The extension **only ever reads** — it never writes to the console. No
analytics, no telemetry.

Three things worth stating precisely rather than as a blanket claim:

**One external host.** Client icons are `<img>` tags pointing at UniFi's own CDN,
`static.ui.com/fingerprint/0/<dev_id>_101x101.png` — the same images the console
itself loads. So that CDN sees requests for the fingerprint ids of your clients
(a device *type*, not a device: `4488`, not a MAC). Nothing else leaves the
browser. If you'd rather it didn't, deleting `uiIconFor` falls back to the
built-in glyphs and the overlay works unchanged.

**The overlay draws into the page's DOM**, so scripts on that page can read what
it draws — client names, MACs, IPs, SSIDs. On your console that discloses
nothing: it is the console's own data, on the console's own page. It would
matter on any *other* page, so the overlay only runs on the origin you enable,
and any address it reports (an API base, a bridge URL) is printed **only when
the page is that console** and redacted everywhere else.

Relatedly, discovery accepts an API address only from the page's own origin or a
`<console-id>.id.ui.direct` tunnel host. The page-context probe reports URLs over
a `window` event, which a page could forge; the host check means a forged one is
ignored.

**A configured bridge is contacted, and a tunnelled one transits Cloudflare.**
Only if you set one — by default the extension talks to nothing but the console
in front of it. When set, the service worker fetches that origin with
credentials, so a Cloudflare Access session cookie goes with the request (that is
what avoids storing a credential here). It sends nothing about you or your
browsing; it asks for two endpoints and reads the reply. If the bridge is
published through a tunnel, that traffic crosses Cloudflare's edge like any other
request to it — your own inventory, on your own hostname, but not a LAN-only path
any more. A bridge on a plain LAN address avoids that entirely and works
wherever the console's own API would have.

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
