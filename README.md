# unifi-hamina-live

Pull **live** Wi-Fi data from a UniFi console — access points, per-radio channel
and TX power, and the clients connected to each AP — and serve it in the shape
Hamina Live's supported vendors expose, so it's drop-in the day Hamina can point
at it. Ships three surfaces over one live poll:

1. **Meraki Dashboard API v1 compatible facade** (`/api/v1`) — the same
   organizations → networks → devices → radios/clients vocabulary Hamina Live
   already consumes from Cisco Meraki.
2. **Vendor-neutral REST API** (`/api`) + a **live dashboard** (`/`) — clean
   JSON and a browser view of "which devices are on which AP", updating live.
3. **Scheduled OpenIntent refresh** (`/openintent`) — regenerates the
   [OpenIntent](https://github.com/shark-fi/unifi-hamina-export) zip on an
   interval so you can re-import fresh AP config into Hamina Planner **today**.

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

All reads are GETs; the only write is the login POST. Poll failures are logged
and the last good snapshot is kept — the server never falls over because the
console blips.

## The three surfaces

### Meraki-compatible facade — `/api/v1`
Implements the subset of Meraki Dashboard API v1 that a Live/observability
client needs, backed by live UniFi data. Auth via `X-Cisco-Meraki-API-Key` or
`Authorization: Bearer`. Full endpoint list and field mapping in
[docs/MERAKI_COMPAT.md](docs/MERAKI_COMPAT.md).

### Neutral REST API — `/api`
`/api/health`, `/api/sites`, `/api/access-points`, `/api/clients`,
`/api/summary`, `POST /api/refresh`. Unauthenticated; meant to sit behind your
own network and power the dashboard.

### Scheduled OpenIntent refresh — `/openintent`
Set `OPENINTENT_REFRESH_ENABLED=true` and point `OPENINTENT_EXPORTER_PATH` at
`unifi_export.py` from the companion repo. It re-runs the exporter every
`OPENINTENT_REFRESH_SECONDS` and serves the newest zip at
`/openintent/latest.zip` for re-import into Hamina Planner. This is the only
path that works with Hamina **today** — see [docs/HAMINA.md](docs/HAMINA.md).

## Configuration

All via environment / `.env` — see [`.env.example`](.env.example) for the full
annotated list (UniFi connection, poll interval, Meraki facade key, OpenIntent
refresh, host/port).

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

## Run with Docker

```bash
docker compose up --build        # reads .env, serves on :8080
```

## Development

```bash
pip install -e '.[dev]'
pytest                           # 21 tests, no network required
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
