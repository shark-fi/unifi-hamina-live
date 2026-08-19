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

It also reads an **Open5GS core**, if you have one: every LTE/5G cell it is
talking to joins the same snapshot as the Wi-Fi APs and lands on the same floor
plan, in a product that has no idea cellular exists. That presentation is partly
a costume and the honest accounting is in
[docs/OPEN5GS.md](docs/OPEN5GS.md) — see
[LTE / 5G cells](#lte--5g-cells-from-an-open5gs-core).

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
| LTE/5G cells | an Open5GS core's `/gnb-info`, `/enb-info`, `/ue-info`, `/pdu-info`, or an Open5G2GO backend's `/enodeb/status`, `/gnodeb/status`, `/connections` | cells as access points, attached UEs as clients — optional, see [LTE / 5G cells](#lte--5g-cells-from-an-open5gs-core) |

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

### Catalyst Center (DNA Center) facade — `/dna/*` — **works, at a cost**

Hamina's **Cisco Catalyst (DNA) Center API** connector takes an Instance URL +
username/password and can disable TLS verification, so unlike Meraki it can be
pointed at this bridge. It syncs: APs land on a Hamina Live map with real MACs,
per-radio channel, TX power, channel width, per-radio client counts, firmware,
and Hamina's own capacity analysis running on those numbers.

**The cost, and it is not small.** The connector resolves an AP against Cisco
hardware only — a bare Cisco model token, with the make implied by the connector
type. No UniFi model is accepted in any spelling (UniFi's code, our slug,
Hamina's catalog display name, and Hamina's own fully-qualified
`ubiquiti:` catalog id were all refused). So every AP must declare itself as
Cisco hardware:

```bash
CATALYST_MODEL_OVERRIDE=CW9166      # every AP reports as a Cisco 9166i
```

Channels, power and client counts are then genuine, but the **hardware identity
is not**, which means Hamina's coverage simulation runs on a CW9166's antenna
pattern rather than your actual AP's. Acceptable for live monitoring — who is
connected, on what channel, at what utilisation. **Misleading for planning**, and
it does not announce itself on the map.

If you want live UniFi data in Hamina *without* misrepresenting your hardware,
use the browser extension instead ([The extension](#the-extension)): it draws
the same telemetry over Hamina's map from the browser, with your APs correctly
identified, and needs no vendor integration at all.

**Still failing:** client sync. `GET /dna/data/api/v1/clients` returns 200 with
well-formed clients and Hamina reports "Failed to synchronize client
information". Three payload shapes were tried, including per-client coordinates
after finding that a working Juniper Mist project returns them for 371 of 440
clients. Per-radio client *counts* work regardless — they come from the
assurance layer — so what is missing is the individual client list, which the
extension does show.

A request logger records every call Hamina makes, matched or not, at
`GET /catalyst/_captured`; it is how all of the above was established. Set
`CATALYST_ENABLED=true` + `CATALYST_USERNAME/PASSWORD`. Full walkthrough and the
findings: [docs/CATALYST.md](docs/CATALYST.md) and
[issue #1](https://github.com/shark-fi/unifi-hamina-live/issues/1).

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

## LTE / 5G cells from an Open5GS core

Set `OPEN5GS_ENABLED=true` and point the bridge at either your core's metrics
servers (`OPEN5GS_AMF_URL` / `OPEN5GS_MME_URL`) or, better where you run it, an
[**Open5G2GO**](https://github.com/Waveriders-Collective/open5G2GO) backend
(`OPEN5G2GO_URL`). Every cell is folded into the same snapshot as the Wi-Fi APs,
so it reaches all four surfaces above with no new plumbing — including the
Catalyst facade, which means **a private 5G cell on a Hamina Live map beside the
Wi-Fi**.

Open5G2GO is the better source because it already polls the radio over SNMP:
band, EARFCN, bandwidth, TX power, real MAC/serial/model/firmware, named devices
from the subscriber database, and **PRB utilisation** — a genuine load figure
that lands where a Wi-Fi controller reports channel utilisation, so Hamina's
capacity view runs on a real number. Its one limit is that it tracks a single
radio and so cannot say which cell a UE is on; on a multi-cell estate read the
core directly, which reports an NR-CGI per UE.

```bash
curl -s localhost:8080/api/cellular | jq '.cells[] | {name, placed, real, costume}'
{
  "name": "CBRS Cell - Warehouse",
  "placed": true,
  "real":    { "technology": "nr", "carrier_mhz": 3550.005, "tx_power_dbm": 30,
               "carrier": "NR n48 ARFCN 636667 (3550.0 MHz, 40 MHz wide)" },
  "costume": { "band": "5", "channel": 104, "channel_width_mhz": 40 }
}
```

**A cell is not an access point.** Its identity, its attached UEs and their
session state are read live and are real; its band, ARFCN and TX power are read
from the radio on the Open5G2GO path and declared by you in
[`cells.json`](cells.example.json) on the direct one, because a core has never
seen the radio; and its Wi-Fi band, channel and hardware model are
invented, because nothing downstream can express a 3.5 GHz carrier. The real
carrier is kept beside the costume on every radio rather than replaced by it, and
signal strength is left empty — a core never sees the air, and an invented RSSI
is the one thing that would make a heatmap actively wrong.

Needs **Open5GS 2.7.7+** for per-cell and per-UE detail (that release added the
JSON dumpers to the metrics server); an older core falls back to the `/metrics`
totals, which give a client count but cannot say which cell a UE is on.

**Placing a cell on a floor plan** is the neat part: name a UniFi device that is
already placed on the console's own map and the cell rides on its position every
poll, so dragging the anchor in UniFi moves the cell with nothing to edit and
nothing to re-import.

```json
"placement": { "anchor_ap": "AP-Warehouse", "dx_px": 40, "dy_px": -20 }
```

Full walkthrough, the Cisco-model requirement for the Hamina import, and what to
say to Hamina about it: **[docs/OPEN5GS.md](docs/OPEN5GS.md)**.

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

**Give the account access to InnerSpace as well as Network.** InnerSpace is a
separate UniFi application, and an admin scoped to Network alone authenticates
fine, reads APs and clients fine, and gets **403 on every floor-plan request**.
Nothing errors: `/api/health` still reports `ok: true`, and `/api/floorplans`
just returns `[]`. Everything positional is then silently unavailable — the
extension overlay has nothing to pin to, the OpenIntent export has no plans, and
Hamina gets no floors. The logs are the only place it shows:

```
GET /proxy/innerspace/api/project?mode=2D "HTTP/1.1 403 Forbidden"
```

Note the console's URL as the host sees it, **including the port**. No trailing
slash.

| console | URL |
|---|---|
| UniFi OS appliance — UDM, UDR, UNVR, Cloud Key Gen2 | `https://192.168.1.1` |
| **UniFi OS Server** — UniFi OS on your own Linux box | `https://10.0.0.5:11443` |
| classic software controller (Java, self-hosted) | `https://10.0.0.5:8443` |

**UniFi OS Server publishes management on 11443, not 443**, because 443 on a
general-purpose host is usually already taken. Everything else about it is
ordinary UniFi OS — same `/proxy/network` and `/proxy/innerspace` paths, same
login, InnerSpace and Protect present — so only the port differs. Getting it
wrong costs more time than it should, because the failure is a bare TCP refusal
with nothing pointing at the port:

```
cannot reach https://10.0.0.5: All connection attempts failed
```

If you are unsure, look at what is listening on the console itself. UniFi OS
Server runs its stack in **rootless Podman**, so every port belongs to `pasta`
rather than to anything recognisable, and 443 is absent:

```
$ sudo ss -tlnp | grep pasta
LISTEN  *:11443   users:(("pasta",pid=1455,...))    <- management: this one
LISTEN  *:8080    users:(("pasta",pid=1455,...))    <- device inform, NOT the API
LISTEN  *:8880    users:(("pasta",pid=1455,...))    <- guest portal
```

`8080` being open is a trap worth naming: it is the device inform port, it
answers, and it is not the API. Pointing `UNIFI_HOST` at it fails later and
less clearly than pointing it nowhere.

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

This repo's `docker-compose.yml` **builds from source** — no registry, no
credentials:

```bash
docker compose up -d --build
```

If port 8080 is already taken on this host, set `HOST_PORT=8081` in `.env`
rather than adding a `ports:` key to an override file: compose *appends* to
sequences instead of replacing them, so an override publishes both and collides
anyway. The container always listens on 8080 internally.

<details>
<summary>Running the prebuilt image instead</summary>

The published image is **private**, so authenticate to GHCR with a token that
has `read:packages`, and point compose at `image:` rather than `build:`:

```bash
read -rsp "GHCR token: " T; echo; echo "$T" | docker login ghcr.io -u <github-user> --password-stdin; unset T
docker compose pull && docker compose up -d
```

On that path `docker compose up --build` will **not** pick up a newer image —
there is nothing to build — so it is always `pull` first. Log in as the same
user you pull as: credentials are per-user, and `sudo docker compose pull` reads
root's, not yours.
</details>

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

**Empty `floorplans` means one of two things**, and they are worth telling
apart. If this console genuinely has no plans in InnerSpace, `[]` is correct and
APs simply have no placement. If it *does* have plans, the account cannot see
InnerSpace — check the logs for the 403 from step 1:

```bash
docker compose logs --tail=200 | grep -i innerspace
```

Everything positional depends on this, and nothing else reports it: health stays
`ok: true` either way. Repeated `rest/map` 400s and `stat/map` 404s in the log
are normal on a modern console — classic Maps is gone; InnerSpace replaced it.

Open <http://host:8080/> for the dashboard.

### 5. Optional: the OpenIntent refresh

The path that gets UniFi floor plans into Hamina Planner. The companion exporter
is **baked into the image** at a pinned commit, so there is nothing to clone and
nothing to mount — one setting turns it on:

```bash
OPENINTENT_REFRESH_ENABLED=true
OPENINTENT_MODE=innerspace
OPENINTENT_REFRESH_SECONDS=0     # build once at startup; AP moves flow live
```

`docker compose up -d`, then:

```bash
curl -s localhost:8080/openintent/status | jq
```

The zip lands at `/openintent/latest.zip` for import into Hamina.
`exporter not found` now only happens if you have pointed
`OPENINTENT_EXPORTER_PATH` somewhere else.

To develop against your own exporter checkout, mount it over the baked-in copy:

```yaml
    volumes:
      - ../unifi-hamina-export/unifi_export.py:/opt/exporter/unifi_export.py:ro
```

Bumping the baked version is a deliberate change to `EXPORTER_REF` in the
Dockerfile — pinned to a commit rather than a branch, so two builds of the same
Dockerfile ship the same exporter.

### 6. Optional: locate transmitters with WLAN Pi sensors

Off by default. This is the **only** endpoint that accepts data — everything
else projects a snapshot the collector fetched — so it is opt-in and will not
start without a token:

```bash
SENSORS_ENABLED=true
SENSOR_TOKEN=$(openssl rand -hex 24)
SENSOR_CONFIG_PATH=./sensors.json
```

Build the layout by clicking the plan at **`http://host:8080/sensors`** rather
than reading pixel coordinates out of an image editor — it emits the JSON, and
warns about the two things that make a deployment produce nothing: fewer than
three sensors, and sensors nearly in a straight line (those fit the data well
and stay ambiguous between two mirrored positions, and the residual will not
tell you).

**In a container `./sensors.json` is `/app/sensors.json`**, not the file beside
your compose file. Mount it:

```yaml
    volumes:
      - ./sensors.json:/app/sensors.json:ro
```

Missing or invalid, ingest stays off and `/api/located` says why — the rest of
the bridge keeps running.

Then point each sensor at this host (see
[wlanpi-rssi-locate](https://github.com/shark-fi/wlanpi-rssi-locate)):

```bash
SENSOR_TOKEN=… python3 rssi_sensor.py --collector http://host:8080 --id pi-1 \
    --iface wlan1@36 --capture aps --setup
```

Fixes appear on the dashboard as dashed rings sized to their own fitting error,
and at `GET /api/located`. They are estimates, kept deliberately separate from
`/api/access-points`, which is surveyed.

Before trusting a number, calibrate the path loss: `rssi_sensor.py --calibrate`,
or fit it from Protect BLE sensors already placed on a plan with
`pathloss_calibrate.py` in the exporter repo.

### 7. Reach it from outside the LAN — needed more often than it sounds

Marked optional because a LAN-only setup works without it. But **both of the
things people usually want this for require it**:

- The browser extension on a **WebRTC-relayed** `unifi.ui.com` session. That
  page holds no HTTP API at all, so the bridge is the only possible source —
  and the bridge has to be reachable from the browser over HTTPS. A LAN address
  is blocked as mixed content on an HTTPS page.
- **Hamina** reaching the bridge. Its cloud calls *in* to your Instance URL;
  there is no path from Hamina to a private address.

The built-in profile is the shortest route — put `CF_TUNNEL_TOKEN` in `.env`
after creating a tunnel in the Cloudflare Zero Trust dashboard, point its public
hostname at `http://unifi-hamina-live:8080`, then:

```bash
docker compose --profile tunnel up -d
```

**Then put an access policy in front of it, before you use it.** `/api/*` is
unauthenticated by design, assuming a LAN. Tunnelled without a policy it
publishes your entire client inventory — MACs, hostnames, IPs, SSIDs — to anyone
who learns the hostname.

The extension needs no credentials of its own: sign in to the tunnel hostname
once in a tab and its service worker reuses that session cookie. A headless
caller like Hamina cannot, so it needs a path-scoped bypass — and scope that
bypass to **every** path the facade serves, not just `/dna`, which is a mistake
that cost a day here.

Full walkthrough, alternatives to Cloudflare, and the exact policy setup:
[docs/EXPOSURE.md](docs/EXPOSURE.md).

### When something is wrong

**`cannot reach <host>: All connection attempts failed`** is a TCP failure, not
a credentials problem — nothing was rejected because nothing answered. Wrong
address, wrong port (see the table in step 1 — UniFi OS Server is on 11443), or
the console is down. Confirm from inside the container, since its view can
differ from the host's:

```bash
docker compose exec unifi-hamina-live python -c "import socket;socket.create_connection(('<console-ip>',11443),5);print('ok')"
```

`ConnectionRefusedError` means the host is up and nothing is listening on that
port. A timeout instead means a firewall is dropping it.

**Changing `.env` needs the container recreated**, and `docker compose up -d`
does not always notice. Ask the container what it actually has rather than
trusting the file:

```bash
docker compose exec unifi-hamina-live printenv UNIFI_HOST
docker compose up -d --force-recreate   # if it disagrees
```


Ask the container what it is running before diagnosing anything else. A stale
image has explained more "the fix did not work" reports here than every real
bug combined:

```bash
curl -s localhost:8080/version | jq
```

```json
{ "name": "unifi-hamina-live", "version": "0.1.0", "sha": "4d1fa90…",
  "code": "7ca348a2e903", "built_at": "…", "python": "3.12.x" }
```

`sha` is the commit CI built the image from. It reads `"unknown"` for **any
local build** — including `docker compose up --build`, which is what this
runbook tells you to do — so on your own host it answers nothing.

`code` is the field to use there: a hash of the Python actually loaded in the
process. Reproduce it from a checkout and compare:

```bash
find unifi_hamina_live -name '*.py' | sort | xargs cat | shasum -a 256
```

Same digest, same code. Different digest, the container is not running what you
are reading.

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
