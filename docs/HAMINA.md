# Getting UniFi data into Hamina — the honest version

This document exists so nobody is surprised. It explains why the **Meraki**
route is blocked, and points to the route that actually works.

> **Update — the Catalyst Center route works today.** Hamina's *Cisco Catalyst
> (DNA) Center API* connector accepts a free-text **Instance URL**, a
> **username/password**, and **self-signed / disable-TLS** options — none of
> the Meraki blockers below apply to it. The bridge ships a Catalyst Center
> facade for exactly this; see [CATALYST.md](CATALYST.md). The rest of this doc
> explains why Meraki specifically can't be used, which is still worth knowing.

## How Hamina Live actually works

Hamina Live is **pull-based and cloud-to-cloud**. For every supported vendor
(Cisco Meraki, Catalyst Center, Juniper Mist, HPE Aruba, Extreme, Ruckus,
Arista) the flow is identical:

1. In a Hamina Planner Plus project you open the **Live** tab and pick a
   **vendor**.
2. You choose a **Region** and paste an **API key**.
3. **Hamina's own cloud backend** then calls *that vendor's* cloud API
   (e.g. `api.meraki.com`) and pulls floor plans, AP models, channels, TX power,
   device status and client data to build the live heatmap.

Three hard constraints fall out of this:

- **There is no inbound / push API.** You cannot send data *to* Hamina. Hamina
  is always the client. (This matches the companion exporter's README: "Hamina
  has no public write API.")
- **Live only talks to hard-coded vendor clouds, selected by Region.** There is
  no documented "custom endpoint URL" field, and **UniFi is not in the vendor
  list.**
- **The API calls originate from Hamina's cloud**, not your browser — so you
  cannot DNS-redirect `api.meraki.com` to a self-hosted bridge and have Hamina
  follow it.

### Why not "just emulate Meraki"?

This tool deliberately speaks the **Meraki Dashboard API v1** shape, because
Meraki is the cleanest, best-documented vendor Hamina already supports. If
Hamina ever exposes a **custom / self-hosted endpoint** option for its Meraki
integration, you point it at this bridge, paste the `MERAKI_COMPAT_API_KEY`, and
UniFi shows up as if it were Meraki. The bridge is built and ready for that day.

What blocks it *today* is purely the "select a Region, not a URL" limitation
above and the fact that the calls come from Hamina's cloud. Nothing in the data
model is the problem — the shapes line up (see
[MERAKI_COMPAT.md](MERAKI_COMPAT.md)).

## So how do I get UniFi into Hamina right now?

Two realistic paths, in order of preference:

### 1. Near-live OpenIntent re-import (works today)

Hamina Planner imports [OpenIntent 2.0](https://github.com/google/openintent)
zips directly — floor plans, AP placement, models, channels, TX power. This repo
can regenerate that zip on a schedule:

```bash
OPENINTENT_REFRESH_ENABLED=true
OPENINTENT_EXPORTER_PATH=/path/to/unifi-hamina-export/unifi_export.py
OPENINTENT_REFRESH_SECONDS=900
```

The freshest zip is always at `GET /openintent/latest.zip`. Re-import it into
your Hamina project to refresh AP config and placement. It's periodic
re-import, **not** a live heatmap — but it's real UniFi data flowing into Hamina
with no manual export step.

### 2. Ask Hamina to add UniFi (or a custom endpoint)

Hamina onboards vendors by partnership. The two asks that would make this bridge
"just work":

- **Add UniFi as a native Live vendor**, or
- **Add a custom base-URL option** to the existing Meraki integration.

Either one turns the `/api/v1` facade in this repo into a live UniFi source. If
you have a Hamina account rep, this is the request to make; point them at this
repo as a working reference implementation of the data source.

## What this tool is genuinely useful for regardless

Even without Hamina cooperating, the bridge is a live UniFi telemetry service:

- The **dashboard** (`/`) and **`/api/summary`** answer "which clients are on
  which AP, on what channel, at what power" live.
- The **`/api/v1`** facade lets any Meraki-API-literate tool (dashboards,
  scripts, monitoring) read UniFi as if it were Meraki.
- The **OpenIntent refresh** keeps a current import artifact ready at all times.
