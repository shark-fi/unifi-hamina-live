# unifi-hamina-live

Pull **live** Wi-Fi data from a UniFi console — access points, per-radio channel
and TX power, and the clients connected to each AP — and serve it in the shape
Hamina Live's supported vendors expose, so it's drop-in the day Hamina can point
at it. Ships four surfaces over one live poll:

1. **Meraki Dashboard API v1 compatible facade** (`/api/v1`) — the same
   organizations → networks → devices → radios/clients vocabulary Hamina Live
   already consumes from Cisco Meraki.
2. **Vendor-neutral REST API** (`/api`) + a **live dashboard** (`/`) — clean
   JSON and a browser view of "which devices are on which AP", updating live.
3. **Scheduled OpenIntent refresh** (`/openintent`) — regenerates the
   [OpenIntent](https://github.com/shark-fi/unifi-hamina-export) zip on an
   interval so you can re-import fresh AP config into Hamina Planner **today**.
4. **UniFi Live for InnerSpace** (`extension/`) — a Chrome extension that draws
   live clients and AP telemetry onto the InnerSpace floor plan *inside the
   UniFi console*. It reads the console directly where it can, and falls back to
   this bridge where it can't — see [The extension](#the-extension).

> **Read this first:** Hamina Live is *pull-based*. It reaches out to a vendor's
> cloud API; there is **no API to push data into Hamina**, and UniFi is not a
> supported vendor. What that means for actually wiring this into Hamina — and
> the honest limits — is in **[docs/HAMINA.md](docs/HAMINA.md)**. Please read it
> before expecting a live heatmap to appear in Hamina on its own.

Companion to [**unifi-hamina-export**](https://github.com/shark-fi/unifi-hamina-export)
(the static OpenIntent exporter). This repo is the *live* side.

## Install

**One command** — full integration in one shot. It builds a venv, installs the
package, seeds `.env`, **and also fetches the companion OpenIntent exporter
([unifi-hamina-export](https://github.com/shark-fi/unifi-hamina-export)) and
enables the scheduled refresh** — so you get both surfaces: the live
Meraki-compatible feed *and* the near-live OpenIntent zip.

```bash
curl -fsSL https://raw.githubusercontent.com/shark-fi/unifi-hamina-live/main/install.sh | bash
```

Or from a checkout — and install it as a service in the same step:

```bash
git clone https://github.com/shark-fi/unifi-hamina-live.git
cd unifi-hamina-live
./install.sh --systemd --start        # enable + start a systemd unit (needs root/sudo)
```

By default the exporter lands next to the install dir and the installer writes
`OPENINTENT_EXPORTER_PATH` + `OPENINTENT_REFRESH_ENABLED=true` into a fresh
`.env`. The fresh import zip is then served at `/openintent/latest.zip`.

On a terminal the installer **prompts** for any UniFi `.env` values still empty
or at their example defaults (host / username / password) and **generates a
random `MERAKI_COMPAT_API_KEY`** — so a fresh install is ready to run without
hand-editing `.env`. Piped installs (`curl | bash`) prompt too, reading from
`/dev/tty`; pass `--non-interactive` (`-y`) to skip prompting and leave `.env`
as-is, or `--interactive` to force it.

Installer flags: `--dir PATH`, `--branch NAME`, `--systemd`, `--user NAME`,
`--start`, `--no-openintent` (live API only), `--exporter-dir PATH`,
`--non-interactive`/`-y`, `--interactive` (`./install.sh --help`). Running it as
a service is covered under [Run as a systemd service](#run-as-a-systemd-service).

## Quick start (manual)

```bash
cp .env.example .env      # then edit UNIFI_HOST / UNIFI_USERNAME / UNIFI_PASSWORD
pip install -e .
python -m unifi_hamina_live
```

Open <http://localhost:8080/> for the live dashboard, or:

```bash
# per-AP connected-client counts + radio state (the "who's on which AP" view)
curl -s localhost:8080/api/summary | jq

# Meraki-compatible, exactly as a Meraki API client would call it
curl -s localhost:8080/api/v1/organizations \
  -H "X-Cisco-Meraki-API-Key: $MERAKI_COMPAT_API_KEY" | jq
curl -s localhost:8080/api/v1/organizations/O_UniFi/devices/statuses \
  -H "X-Cisco-Meraki-API-Key: $MERAKI_COMPAT_API_KEY" | jq
```

Use a **local admin account** (UniFi → Admins & Users → "Restrict to local
access only"). A ui.com cloud account hits MFA and cannot log in from a script.
Interactive OpenAPI docs live at `/docs`.

## What it collects

Every `POLL_INTERVAL_SECONDS` it logs into the console (UniFi OS *or* classic
controller) and reads, per site:

| Source | Endpoint | Data |
|---|---|---|
| Access points | `…/stat/device` | model, MAC, IP, state, uptime, firmware, per-radio **channel / width / TX power / client count / channel utilization** |
| Clients | `…/stat/sta` | per client: associated **AP**, SSID, band, channel, RSSI/signal, TX/RX rates and bytes, uptime |
| Sites | `…/self/sites` | site inventory + rollup counts |
| Placement | classic Maps (`stat/device` x,y) or InnerSpace | floor plans + **live AP x,y** — so an AP move flows through the API without an OpenIntent rebuild |

All reads are GETs; the only write is the login POST. Poll failures are logged
and the last good snapshot is kept — the server never falls over because the
console blips.

**Live push (experimental):** set `WEBSOCKET_ENABLED=true` to also subscribe to
the controller's event stream, so client connect/disconnect/roam and AP up/down
land in near real time instead of at the poll interval. The poll stays on as the
authoritative reconciler, so a missed event self-heals. The event stream is
undocumented and varies by Network version — hence experimental, and off by
default.

## The surfaces

### Meraki-compatible facade — `/api/v1`
Implements the subset of Meraki Dashboard API v1 that a Live/observability
client needs, backed by live UniFi data. Auth via `X-Cisco-Meraki-API-Key` or
`Authorization: Bearer`. Full endpoint list and field mapping in
[docs/MERAKI_COMPAT.md](docs/MERAKI_COMPAT.md).

### Catalyst Center (DNA Center) facade — `/dna/*` — **does not sync**
Hamina's **Cisco Catalyst (DNA) Center API** connector takes an Instance URL +
username/password and can disable TLS verification, so unlike Meraki it *can*
be pointed at this bridge. It was, and **Hamina's vendor sync still fails** —
closed **not planned** in [issue #1](https://github.com/shark-fi/unifi-hamina-live/issues/1),
because the sync needs a fuller Catalyst topology (real WLC, uplink switches, a
self-consistent device graph) than a UniFi-backed facade can fabricate. Don't
enable it expecting a live heatmap; use the OpenIntent path.

What remains true: the facade speaks the DNA Center Intent API backed by live
UniFi data, is a faithful DNAC 2.3.7.x mock through the assurance layer, and its
placement model (AP x,y in metres on a sized floor) maps natively from the
placement layer. A request logger records every `/dna/*` call Hamina makes (read
it at `/catalyst/_captured`). Set `CATALYST_ENABLED=true` +
`CATALYST_USERNAME/PASSWORD`. Full walkthrough and the finding:
[docs/CATALYST.md](docs/CATALYST.md).

### Neutral REST API — `/api`
`/api/health`, `/api/sites`, `/api/access-points`, `/api/clients`,
`/api/summary`, `/api/map`, `/api/floorplans/{id}/image`, `POST /api/refresh`.
Unauthenticated; meant to sit behind your own network and power the dashboard.

### Live client map — dashboard `/`
The dashboard renders a **live map of connected clients** on the floor-plan
image: each placed AP is drawn at its live `x`/`y`, with the clients currently
associated to it clustered around it (UniFi, like every non-Mist vendor,
reports clients per-AP, not with real x,y) and animating as they roam. Backed
by `GET /api/map` (floor-plan list + placed APs with their clients in one call)
and `GET /api/floorplans/{id}/image` (the floor-plan image proxied from the
console). Pick a floor from the selector; click an AP to list its clients.

### Live AP placement — `/api/floorplans`
Floor plans and per-AP `x`/`y` are collected every poll from classic Maps or
InnerSpace (`unifi/placement.py`), in the **same pixel space the OpenIntent
exporter uses**, so live positions line up with what Hamina imported. Positions
live on each access point (`/api/access-points`, `/api/summary`) and on the
Meraki `floorPlans` endpoint. Because positions flow live, **an AP move no
longer needs an OpenIntent rebuild** — set `OPENINTENT_REFRESH_SECONDS=0` to
generate the zip once for the initial import and rely on live positions after.

### Scheduled OpenIntent refresh — `/openintent`
Set `OPENINTENT_REFRESH_ENABLED=true` and point `OPENINTENT_EXPORTER_PATH` at
`unifi_export.py` from the companion repo. With `OPENINTENT_REFRESH_SECONDS>0`
it re-runs the exporter on that interval; with `=0` it generates once at startup
(**initial import** — floor-plan images + geometry) and then leaves positions to
the live placement layer. The newest zip is served at `/openintent/latest.zip`
for import into Hamina Planner — see [docs/HAMINA.md](docs/HAMINA.md).

The exporter is run as a subprocess with the console password in its
**environment**, never on its command line — argv is readable from `ps` by every
user on the host, and this runs on a schedule. That requires an exporter from
[unifi-hamina-export#9](https://github.com/shark-fi/unifi-hamina-export/pull/9)
onward, which reads `UNIFI_PASSWORD`; an older one exits with a message rather
than hanging on a prompt it cannot show.

**Stale-import detection:** since the zip is baked once, a *map* change
(rescale, resize, replaced image, plan added/removed — **not** an AP move) would
leave Hamina's imported image out of date. The refresher watches the floor-plan
structure and, on such a change, sets `stale: true` on `/openintent/status`,
logs it, and POSTs `OPENINTENT_STALE_WEBHOOK` if set — so you re-import
deliberately. Set `OPENINTENT_AUTO_REGENERATE=true` to regenerate automatically
instead.

## The extension

[`extension/`](extension/) is **UniFi Live for InnerSpace** — a Chrome (MV3)
extension that overlays live clients and AP telemetry onto the InnerSpace floor
plan inside the UniFi console. InnerSpace is a planning view and shows no live
clients; this fills that gap. Load it unpacked from `extension/`; its own
[README](extension/README.md) covers install and configuration.

It lives here because it and this bridge are two ends of one contract.

### Where it works, and when the bridge is required

| Access path | On its own | With this bridge |
|---|---|---|
| Console on its LAN address (`https://192.168.x.x/…`) | ✅ | not needed |
| `unifi.ui.com` proxied over HTTP (`/consoles/<id>/proxy/network/…`) | ✅ | not needed |
| `unifi.ui.com` **WebRTC-relayed** | ❌ | ✅ |

The extension prefers the console's own Network API, which is same-origin and
needs nothing configured. A **WebRTC-relayed** `unifi.ui.com` session is the
case it cannot serve alone: the page holds no HTTP API to call at all, only a
signalling channel. There the bridge is not a fallback but the *only* source —
a service worker fetches it out-of-page, which also sidesteps the
mixed-content block a relayed HTTPS page would otherwise impose.

### The contract

The extension consumes three neutral endpoints, and nothing else here:

| Endpoint | Used for |
|---|---|
| `GET /api/health` | the popup's reachability test |
| `GET /api/access-points` | AP identity + per-radio channel, width, utilization, TX retries |
| `GET /api/clients` | per-client `ap_mac` (the join key), band, signal, and `dev_id` for the fingerprint icon |

**Changing the shape of those three is a breaking change for the extension in
this repo.** APs join to the overlay by *name*, and clients join to APs by
`ap_mac` — so renaming or dropping either field breaks the overlay silently
rather than loudly: the fetch still succeeds and the overlay simply draws
nothing. `tests/test_neutral_api.py` is the guard; extend it rather than
loosening it.

One bridge instance polls **one** console, so the extension stores its bridge
URL **per console**. Pointing a console at a bridge that covers a different one
yields a healthy-looking fetch whose AP names all miss; the extension detects
that and says so instead of drawing an empty overlay.

## Configuration

All via environment / `.env` — see [`.env.example`](.env.example) for the full
annotated list (UniFi connection, poll interval, WebSocket push, Meraki facade
key, OpenIntent refresh, host/port, Cloudflare Tunnel token).

## Expose it to Hamina / the cloud

The bridge runs on your LAN; a cloud consumer calls in from outside and can't
reach a private IP. To make it reachable you need a public HTTPS endpoint — the
easiest is the built-in **Cloudflare Tunnel** profile:

```bash
# put CF_TUNNEL_TOKEN in .env, then:
docker compose --profile tunnel up -d
```

Full walkthrough and alternatives (reverse proxy + Let's Encrypt, VPS relay) in
[docs/EXPOSURE.md](docs/EXPOSURE.md).

## Run as a systemd service

`./install.sh --systemd` renders [`deploy/unifi-hamina-live.service`](deploy/unifi-hamina-live.service)
with your install path and user, drops it in `/etc/systemd/system/`, and enables
it. To do it by hand instead:

```bash
sudo cp deploy/unifi-hamina-live.service /etc/systemd/system/
sudo sed -i "s#__INSTALL_DIR__#$PWD#g; s#__USER__#$(id -un)#g" \
  /etc/systemd/system/unifi-hamina-live.service
sudo systemctl daemon-reload
sudo systemctl enable --now unifi-hamina-live
```

The unit runs `.venv/bin/python -m unifi_hamina_live`, reads config from
`.env` via `EnvironmentFile`, and restarts on failure. Manage it with:

```bash
sudo systemctl status unifi-hamina-live
sudo journalctl -u unifi-hamina-live -f      # live logs
sudo systemctl restart unifi-hamina-live     # after editing .env
```

## Deploy on a new host and a new console

Start to finish on a machine that has never run this, against a console it has
never seen. Roughly ten minutes, most of it waiting for the first poll.

### 1. A local admin on the console

UniFi → **Settings → Admins & Users → Add Admin**, and tick **"Restrict to local
access only"**. Give it a password you are willing to put in a file.

This is not optional politeness: a ui.com **cloud account cannot be used**. It
hits MFA and the login fails from a script with an error that does not say so.
Read-only is enough — everything here is GETs apart from the login POST.

Note the console's URL as the host sees it (`https://192.168.1.1`,
`https://10.0.0.1:8443`, a UDM's address, whatever it is). No trailing slash.

### 2. Files on the host

```bash
git clone https://github.com/shark-fi/unifi-hamina-live.git
cd unifi-hamina-live
cp .env.example .env
```

Edit `.env` — four values matter to start:

```bash
UNIFI_HOST=https://192.168.1.1
UNIFI_USERNAME=the-local-admin-you-just-made
UNIFI_PASSWORD=its-password
UNIFI_VERIFY_TLS=false        # local consoles use self-signed certs
```

Leave everything else at its default for now. `.env` is gitignored; keep it that
way.

### 3. Start it

The image is **private**, so authenticate to GHCR first with a token that has
`read:packages`:

```bash
echo "$GHCR_TOKEN" | docker login ghcr.io -u <github-user> --password-stdin
docker compose pull && docker compose up -d
```

`docker compose up --build` will **not** pick up a newer published image — the
compose file uses `image:`, so `--build` has nothing to build. It is always
`pull` first.

### 4. Check it actually reached the console

```bash
curl -s localhost:8080/api/health | jq
```

`ok: true` means a poll succeeded. `ok: false` with an `error` means it started
but could not read the console — almost always credentials, the host URL, or a
cloud account used by mistake. The container logs name which:

```bash
docker compose logs -f --tail=50
```

Then confirm there is real data behind it:

```bash
curl -s localhost:8080/api/access-points | jq '.[] | {name, model, online}'
curl -s localhost:8080/api/floorplans   | jq '.[] | {name, width_px, meters_per_px}'
```

Empty `floorplans` is normal and not an error — it means no plans in InnerSpace
or classic Maps yet. APs still report; they just have no placement.

Open <http://host:8080/> for the dashboard.

### 5. Optional: the OpenIntent refresh

This is the path that gets UniFi floor plans into Hamina Planner, and it needs
the companion exporter, which is **not in the image**:

```bash
cd .. && git clone https://github.com/shark-fi/unifi-hamina-export.git
cd unifi-hamina-live
cp docker-compose.override.example.yml docker-compose.override.yml
```

The override mounts that sibling checkout read-only at `/exporter`. Then in
`.env`:

```bash
OPENINTENT_REFRESH_ENABLED=true
OPENINTENT_MODE=innerspace
OPENINTENT_REFRESH_SECONDS=0     # build once at startup; AP moves flow live
```

`docker compose up -d`, then:

```bash
curl -s localhost:8080/openintent/status | jq
```

`exporter not found` means the mount is missing — that is the step people skip.
The zip lands at `/openintent/latest.zip` for import into Hamina.

Requires an exporter from
[unifi-hamina-export#9](https://github.com/shark-fi/unifi-hamina-export/pull/9)
onward: the password is passed in the environment, never on the command line,
and an older exporter would not read it.

### 6. Optional: reach it from outside the LAN

Only needed for a cloud consumer, or for the browser extension on a
**WebRTC-relayed** `unifi.ui.com` session. See [docs/EXPOSURE.md](docs/EXPOSURE.md)
— and read the access-policy part of it, because `/api/*` is unauthenticated by
design and a tunnel without a policy publishes your whole client inventory to
anyone who learns the hostname.

### When something is wrong

Ask the container what it is running before diagnosing anything else. A stale
image has explained more "the fix did not work" reports here than every real
bug combined:

```bash
C=$(docker ps --filter publish=8080 --format '{{.Names}}' | head -1)
docker exec -i "$C" python -c "import unifi_hamina_live, sys; print(sys.version)"
docker compose images
```

On a NAS your user is usually not in the `docker` group (prefix everything with
`sudo`), and a stack created through the NAS UI often has a different compose
service name than this repo's — find the container by published port rather than
by name, as above.

## Run with Docker

Build locally:
```bash
docker compose up --build        # reads .env, serves on :8080
```

Or pull the prebuilt multi-arch image (Intel + ARM) from GHCR. The image is
**private**, so log in first with a GitHub token that has `read:packages`:
```bash
echo $GHCR_TOKEN | docker login ghcr.io -u <github-user> --password-stdin
docker run -d --env-file .env -p 8080:8080 \
  ghcr.io/shark-fi/unifi-hamina-live:latest
```

**Synology (Container Manager):** see [docs/SYNOLOGY.md](docs/SYNOLOGY.md) —
pull the image via a Project, no on-NAS build needed.

## Development

```bash
pip install -e '.[dev]'
pytest                           # 63 tests, no network required
```

Tests run entirely off sample UniFi payloads (`tests/conftest.py`) through a
fake collector, so they exercise normalization, the Meraki mapping, auth, and
both API layers without touching a console.

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). In short: one background
poller produces an immutable `Snapshot`; every endpoint is a pure projection of
the current snapshot, so all three surfaces always agree.

## License

MIT — see [LICENSE](LICENSE).
