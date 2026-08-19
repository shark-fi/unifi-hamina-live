# Open5GS — LTE / 5G cells on the same map as the Wi-Fi

Point the bridge at an Open5GS core and every cell it is talking to joins the
same snapshot the UniFi APs are in. From there it reaches everything already
built on that snapshot with no new plumbing: the neutral API, the live
dashboard, the Meraki facade, and — the reason this exists — the **Catalyst
Center facade Hamina can actually be pointed at**.

The result is one floor plan with Wi-Fi APs and a private LTE/5G cell on it,
live, in a product that has no idea cellular exists.

## Two ways in, and one of them is much better

| | reads | gives you |
|---|---|---|
| **Open5G2GO** (`OPEN5G2GO_URL`) | that project's own backend API | cells **with live RF** — band, EARFCN, bandwidth, TX power, PRB utilisation, real MAC/serial/model/firmware — plus named devices from the subscriber database |
| **Open5GS direct** (`OPEN5GS_AMF_URL` / `OPEN5GS_MME_URL`) | the core's metrics servers | cells, and **which cell each UE is on**; the radio has to be declared in `cells.json` |

If you run [Open5G2GO](https://github.com/Waveriders-Collective/open5G2GO),
point at that. Its backend already polls the eNodeB over SNMP, so almost
everything `cells.json` otherwise asks you to type by hand arrives measured —
and `cells.json` shrinks to placement plus the model costume:

```json
{ "network_name": "Waveriders-Private",
  "cells": [{ "id": "any-cell", "model": "CW9166I",
              "placement": { "anchor_ap": "AP-Warehouse", "dx_px": 40, "dy_px": -20 } }] }
```

```bash
OPEN5GS_ENABLED=true
OPEN5G2GO_URL=http://10.48.0.10:8080
```

That is the whole configuration. Everything below about enabling metrics servers
and declaring carriers applies to the **direct** path; skip to
[step 4](#4-put-the-cell-on-the-unifi-map) if you are on Open5G2GO.

**The one thing Open5G2GO cannot do** is say which cell a UE is on — it tracks a
single radio, so its connection list never had a cell to name. With one cell
that is exact and the bridge attributes every UE to it. With several live cells
it refuses to guess, says so in the log, and leaves each cell showing the count
its own SNMP measured; use the direct path there, which reports an NR-CGI per
UE. Setting both is not an error — Open5G2GO wins — but on a multi-cell estate
that is the wrong way round.

PRB utilisation deserves a line of its own: it is a genuine load measurement and
it lands on the radio where a Wi-Fi controller reports **channel utilisation**,
so Hamina's capacity view runs on a real number rather than a costumed one. It
is the only live RF figure in this integration that survives the trip intact.

## Read this before you show it to anyone

A cell is not an access point. Presenting it as one is what makes this work with
zero changes downstream, and it is also the thing most likely to mislead
somebody reading the map. Three tiers, and the code keeps them apart:

| | where it comes from | trust it? |
|---|---|---|
| Cell identity — PLMN, gNB/eNB id, TAC, NR-CGI, NG/S1 state, the RAN's IP | live from the core | **yes** |
| Attached UEs, which cell each is on, session state, UE IP address | live from the core | **yes** |
| Band, ARFCN, bandwidth, TX power, PRB utilisation | **live from the radio over SNMP on the Open5G2GO path**; `cells.json` on the direct path, because the core has never seen the radio | **yes** via Open5G2GO; otherwise as far as you trust your own config |
| The **Wi-Fi band and channel** the cell reports | invented here | **no** — see below |
| The **hardware model** it reports | `cells.json`, and for Hamina it must be a Cisco model | **no** |

There is no honest mapping from n48 to a Wi-Fi channel. The costume only has to
be *stable* (the same cell reports the same channel every poll, so nothing
downstream sees a radio hopping) and *out of the way* (it defaults to DFS
channels a UniFi estate rarely sits on, so the fake radio does not read as a
co-channel neighbour of a real one). Pin it with `wifi_channel` when you want it
somewhere specific.

The real carrier is never thrown away: it rides alongside the costume on every
radio as `technology`, `carrier_mhz` and `carrier_label`, and
`GET /api/cellular` shows the two side by side. **Signal strength is left
empty on purpose** — a core never sees the air, and an invented RSSI is the one
thing that would make a Hamina heatmap actively wrong rather than merely
costumed.

## What you need (direct path)

* **Open5GS 2.7.7 or newer** for per-cell and per-UE detail. That release added
  JSON dumpers to each NF's metrics server: `/gnb-info`, `/enb-info`,
  `/ue-info` (AMF/MME) and `/pdu-info` (SMF). They are the whole basis of this
  integration — `/metrics` alone publishes only core-wide totals (`ran_ue`,
  `gnb`, `amf_session`, `enb_ue`, `enb`) with no per-cell labels, so it can
  never say which UE is on which cell.
* An **older core still works, less well**: an unknown path on that server
  answers `400 Bad Request`, the bridge notices once, says so in the log, and
  falls back to the `/metrics` totals. You get a cell on the map with a client
  count — provided you declare exactly one cell of that technology, because a
  core-wide total cannot be split across several — and no individual UEs.
* A **RAN**. The core knows nothing about the radio, so the band/ARFCN/power in
  `cells.json` come from your gNB or eNB config (srsRAN, Amarisoft, or the small
  cell's own UI).

## 1. Expose the metrics servers

Each NF serves this on the address in its own YAML, and the shipped default is a
loopback address — fine on a bare-metal core, useless from another container:

```yaml
# amf.yaml (likewise smf.yaml, upf.yaml, mme.yaml)
amf:
  metrics:
    server:
      - address: 0.0.0.0     # was 127.0.0.5
        port: 9090
```

Restart the NF, then check from wherever the bridge runs — this is also how you
find out which release you are on:

```bash
curl -s http://<amf-host>:9090/gnb-info | jq .
# 2.7.7+ -> {"items":[{"gnb_id":100,...,"num_connected_ues":2}],"pager":{...}}
# older  -> Bad Request

curl -s http://<amf-host>:9090/ue-info  | jq '.items[] | {supi, cm_state, gnb}'
curl -s http://<smf-host>:9090/pdu-info | jq '.items[] | {supi, pdu}'
```

In Docker, publish or attach: if the bridge runs on the core's compose network
the NF container names are the hostnames (`http://amf:9090`); otherwise publish
`9090` per NF on distinct host ports and point at those. All reads are GETs, and
the endpoints are unauthenticated — keep them on an internal network.

## 2. Point the bridge at it

```bash
OPEN5GS_ENABLED=true
OPEN5GS_AMF_URL=http://amf:9090        # 5G
OPEN5GS_MME_URL=http://mme:9090        # 4G — set either or both
OPEN5GS_SMF_URL=http://smf:9090        # optional: UE IP address and DNN
OPEN5GS_CELLS_PATH=./cells.json
OPEN5GS_SITE_ID=                       # blank = the first UniFi site
```

Restart, then:

```bash
curl -s localhost:8080/api/cellular | jq .
```

That endpoint is the one place that says plainly what these entries are: which
cells were found, whether each is placed on a plan, the real carrier, and the
Wi-Fi channel it is reporting instead. If `cells` is empty, `status` and `error`
say why.

## 3. Describe the radios — `cells.json`

Copy [`cells.example.json`](../cells.example.json). One entry per cell:

```json
{
  "network_name": "SharkFi-Private",
  "cells": [{
    "name": "CBRS Cell - Warehouse",
    "match": { "gnb_id": 100 },
    "model": "CW9166I",
    "radio": { "technology": "nr", "band": "n48", "arfcn": 636667,
               "bandwidth_mhz": 40, "tx_power_dbm": 30 },
    "placement": { "anchor_ap": "AP-Warehouse" }
  }]
}
```

* `match` takes any subset of `gnb_id` / `enb_id` / `cell_id` / `plmn` / `tac`.
  Get the values from `/api/cellular` or straight from `/gnb-info`.
* **A spec with no `match` is the fallback** for any cell nothing else claimed —
  which is what a one-cell proof of concept wants: declare the radio once and it
  applies whatever gNB id turns up. An explicit match always beats a fallback,
  whatever the file order.
* A cell you declared explicitly but that the core is **not** reporting appears
  **offline** rather than vanishing — the same thing a UniFi AP does when it
  loses power. A cell that disappears reads as "no such site"; one that goes
  grey reads as "go and look at that one".
* `arfcn` is an NR-ARFCN for `technology: nr` and an EARFCN for `lte` (which
  also needs `band`, since an EARFCN alone is ambiguous). Bands outside the
  table in [`rf.py`](../unifi_hamina_live/cellular/rf.py) are not guessed at —
  give `frequency_mhz` instead.

## 4. Put the cell on the UniFi map

The UniFi console places devices **it manages**. There is no way to drop a
third-party radio onto a UniFi floor plan directly, and the InnerSpace placement
this bridge reads is keyed on device MAC, so a planned marker with no MAC is
skipped. Two ways round it:

### Anchor it to a UniFi device (recommended)

Put any adopted UniFi device at the cell's physical location, place it on the
plan in UniFi as normal, and name the cell's anchor after it:

```json
"placement": { "anchor_ap": "AP-Warehouse", "dx_px": 40, "dy_px": -20 }
```

The cell then inherits that device's live position every poll. **Drag the anchor
in UniFi and the cell moves** — no file to edit, nothing to re-import, and the
console stays the only place anything is positioned. That matters more than it
sounds: pixels in a config file go stale the moment somebody rescales the plan.

The anchor does not have to be an AP — any device the console will place works,
and a spare/older AP mounted next to the cell is the tidiest version. `dx_px` /
`dy_px` nudge the cell off its anchor; without them the two draw as one icon on
top of another, which on a map looks exactly like the cell failed to import. A
24 px offset is applied by default.

### Or pin it by hand

```json
"placement": { "floorplan": "<id from GET /api/floorplans>", "x_px": 980, "y_px": 410 }
```

Pixels on that plan's image. The `/sensors` page in this bridge lets you click a
position off the plan rather than reading coordinates by eye — it was built for
sensor placement but the coordinate space is the same one.

## 5. Get it into Hamina

Use the **Catalyst Center (DNA Center) connector** — the one Hamina integration
that can be pointed at your own host. Set it up per
[CATALYST.md](CATALYST.md), and note the one extra step this adds:

**Hamina resolves an AP's model against Cisco hardware only.** No non-Cisco
model string is accepted in any spelling — that was established for UniFi APs
(see [CATALYST.md](CATALYST.md) and issue #1) and it applies just as much to a
cell. So a cell that is to appear in Hamina must declare a Cisco AP model:

```json
"model": "CW9166I"
```

Per-cell, in `cells.json`, which is better than the global
`CATALYST_MODEL_OVERRIDE`: the cell was never going to report honest hardware
anyway, and this leaves your real UniFi APs reporting their real models. (For a
Hamina import the UniFi APs need the global override too, for the same reason —
that trade-off is unchanged and documented where it already was.)

What lands in Hamina:

* the cell on the floor plan, at the position it inherited from the UniFi map;
* its channel, TX power, channel width and per-radio client count, live;
* Hamina's capacity analysis running on those numbers.

What does not:

* **the individual UE list.** Client sync over the Catalyst connector is still
  unresolved for Wi-Fi clients (see *What is still open* in
  [CATALYST.md](CATALYST.md)) and cells are on the same path. Per-radio client
  **counts** work, because those arrive via the assurance layer.
* **the antenna pattern.** The coverage simulation runs on a CW9166I's antenna,
  not your cell's, and at a Wi-Fi channel rather than your carrier. Treat the
  map as live monitoring — who is on air, on what, at what load. It is not a
  planning model of your cellular coverage, and nothing on the map announces
  that.

## What this is worth showing Hamina

The demo is not "we made LTE look like Wi-Fi". It is:

> Your Live map is already the right picture for a mixed-radio site. The only
> thing stopping a real private-5G cell appearing on it beside the Wi-Fi is that
> the connector resolves models against a Cisco-only catalog and the data model
> has nowhere to put a carrier that is not a Wi-Fi channel. Everything else —
> position, radio state, live client counts, per-cell attribution — is already
> there, and here it is running.

Concretely, what the product would need in order to stop this being a costume:

1. a make/model catalog that admits non-Cisco and non-Wi-Fi radios;
2. a radio record that can carry a real centre frequency and bandwidth rather
   than a Wi-Fi channel number (the data is already on the wire here as
   `carrier_mhz` / `carrier_label`);
3. clients that can be attributed to a cell without a Wi-Fi RSSI, which is the
   same gap that blocks the Catalyst client sync today.

## Troubleshooting

`status.source` on `/api/cellular` says which path is in use.

| symptom | cause |
|---|---|
| `/api/cellular` says `configured: false` | none of `OPEN5G2GO_URL`, `OPEN5GS_AMF_URL`, `OPEN5GS_MME_URL` is set |
| cells appear but every UE is missing, log says "no cell association" | Open5G2GO path with more than one live cell — it will not guess; use the direct path |
| cell has real identity but `radios: []` on the Open5G2GO path | SNMP could not reach the radio — check `snmp.enabled` and the allowed-hosts list on the eNodeB, **and that the address Open5G2GO polls matches the one S1AP connected from**; a radio configured under a stale address times out silently. The cell stays online on the S1 link's word; only the RF detail is missing |
| `cells: []`, no error | the core is up but no gNB/eNB has completed NG/S1 setup — check the RAN, not the bridge |
| log: `has no /gnb-info (HTTP 400)` | core older than 2.7.7; the `/metrics` fallback is in use |
| cell appears, `radios: []` | no `cells.json` entry matched it — the log names the `match` to add |
| cell appears, `placed: false` | no placement configured, or the anchor is not on this console / not on a plan; the log says which |
| cell is on the map, Hamina refuses the import | the model is not a Cisco one — see step 5 |
| UE count on the cell but no clients listed | `OPEN5GS_INCLUDE_UES=false`, or a pre-2.7.7 core |
| UEs listed with no IP | `OPEN5GS_SMF_URL` is unset — the AMF/MME does not know the UE's address |

Subscriber identifiers are masked to PLMN + last four by default
(`OPEN5GS_MASK_SUPI`): a SUPI is an IMSI, it identifies a person's SIM, and this
ends up on a dashboard that gets screen-shared.
