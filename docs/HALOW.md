# Wi-Fi HaLow — an 802.11ah AP on the same map as the Wi-Fi

Point the bridge at an Alfa (or other OpenWrt) HaLow radio and its access point
— plus every station associated to it — joins the same snapshot the UniFi APs
are in. From there it reaches everything already built on that snapshot with no
new plumbing: the neutral API, the live dashboard, the Meraki facade, and the
**Catalyst Center facade Hamina can actually be pointed at**.

The result is one floor plan with your Wi-Fi APs and a sub-GHz HaLow AP on it,
live, showing coverage and clients side by side.

This is the third live source, after UniFi itself and the
[Open5GS cellular](OPEN5GS.md) one. It follows the cellular source's discipline
about what is real and what is a costume — read that page first if you want the
fuller argument; the short version is below.

## What is real, and what is a costume

A HaLow AP genuinely **is** an 802.11 access point, so far less is invented here
than for a cell. What it is **not** is a 2.4/5/6 GHz access point: 802.11ah runs
in sub-GHz spectrum (902–928 MHz in the US) with 1/2/4/8 MHz channels that have
no place on the band axis Hamina draws.

| | where it comes from |
|---|---|
| **Real, off the radio** | BSSID, SSID, TX power, noise floor, WPA mode; per-station MAC and signal — all via the radio's `ubus` / `iwinfo` API |
| **Real, declared by you** | where the AP sits on a floor plan, and optional cosmetic name / model |
| **Invented (a costume)** | the Wi-Fi band + channel reported downstream — a *stable, out-of-the-way* 5 GHz DFS channel, so the AP does not read as a co-channel neighbour of a real UniFi radio |

The invented parts never overwrite the real ones. The AP is marked
`source="halow"`, `Radio.technology` is `"halow"`, and the true carrier — that
it is 802.11ah, its country, and the channel the driver maps it to — rides on
`Radio.carrier_label`.

**One number is deliberately withheld.** The Morse Micro driver in these radios
reports the S1G channel mapped onto a legacy 2.4 GHz channel number — `iwinfo`
here says channel 8 / 2447 MHz. That is where the *mapping* sits, not where the
radio transmits, so it is kept in the human label and **`carrier_mhz` is left
`null`** rather than filled with a 2.4 GHz figure that would read as a measured
sub-GHz centre. The exact S1G centre is not exposed by the permitted `ubus`
surface, and a wrong frequency dressed as a measurement is the one thing this
project keeps refusing to draw.

## 1. Enable it

```bash
HALOW_ENABLED=true
HALOW_HOST=10.10.5.158           # the radio's address; https is assumed
HALOW_USERNAME=admin             # MatrixPro default is admin / admin
HALOW_PASSWORD=admin
```

That is enough to make the AP appear on every surface. Everything else is
placement and cosmetics.

The account only needs the read methods the web UI already uses —
`session.login`, `iwinfo info`, `iwinfo assoclist`, and (for client IPs)
`rpc-oui arp_table`. The bridge only ever *reads*; it never reconfigures the
radio.

## 2. Put the AP on the UniFi map

A HaLow AP has no position of its own, the same as a cell. Two ways to give it
one, and the first is better:

**Anchor it to a UniFi AP** that is already placed on the console's own map. The
HaLow AP then inherits that AP's position every poll — move the anchor in UniFi
and the HaLow AP follows, with nothing to re-import.

```bash
HALOW_ANCHOR_AP=Warehouse-AP     # the UniFi AP's name, as shown in the console
HALOW_DX_PX=40                   # nudge off the anchor so the two don't stack
HALOW_DY_PX=-20
```

**Or place it explicitly** on a floor plan, in image pixels — for a radio with
no UniFi AP near it:

```bash
HALOW_FLOORPLAN=<plan-id>        # from GET /api/floorplans
HALOW_X_PX=512
HALOW_Y_PX=300
```

Without either, the AP still reaches the API and dashboard; it simply has no
place on a plan until one is configured.

## 3. Check what it is doing

`GET /api/halow` is the companion to `/api/cellular`: it says plainly that the
entry is an 802.11ah AP, what its real carrier is, and which Wi-Fi channel it is
wearing instead.

```jsonc
{
  "configured": true,
  "status": { "online": true, "clients": 2, "ssid": "WLPC-HALOW",
              "bssid": "00:c0:ca:b4:74:75" },
  "access_points": [{
    "name": "WLPC HaLow", "mac": "00:c0:ca:b4:74:75", "online": true,
    "real":    { "technology": "halow", "carrier_mhz": null,
                 "carrier": "802.11ah HaLow (S1G, sub-GHz) (US, 8 MHz, driver maps to ch8/2447 MHz)",
                 "tx_power_dbm": 30.0 },
    "costume": { "band": "5", "channel": 136, "channel_width_mhz": 20 }
  }]
}
```

## When the radio goes away

A radio that stops answering does not blank the rest of the map, and it does not
drop off it either: the AP stays where it was, greyed out (`online: false`, no
radio), exactly as a UniFi AP that lost power does. A gap on a coverage map
reads as "no coverage here", which is a different and wronger thing than "this
AP is down". Before the *first* successful poll there is nothing to grey out, so
nothing is drawn — an AP invented from a first-poll failure would import and
then vanish when the radio came up with its real BSSID.

## Getting it into Hamina

Same story as everything else here: Hamina Live is pull-based and does not treat
this bridge as a native vendor, so the AP reaches Hamina through the
Catalyst-compatible facade like the Wi-Fi and cellular data do. See
[HAMINA.md](HAMINA.md) for the honest limits, and [CATALYST.md](CATALYST.md) for
the connector itself.
