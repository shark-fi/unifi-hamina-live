# LoRaWAN — a MikroTik gateway on the same map as the Wi-Fi

Point the bridge at a MikroTik LoRa gateway (a wAP LR8 running RouterOS 6) and
the gateway joins the same snapshot the UniFi APs are in. From there it reaches
everything already built on that snapshot with no new plumbing: the neutral API,
the live dashboard, the Meraki facade, and the **Catalyst Center facade Hamina
can actually be pointed at**.

The result is one floor plan with your Wi-Fi APs and a sub-GHz LoRaWAN gateway
on it, live, showing where sub-GHz coverage is anchored alongside the Wi-Fi.

This is the fourth live source, after UniFi itself, the
[Open5GS cellular](OPEN5GS.md) one, and the [HaLow](HALOW.md) one. It follows
the same discipline about what is real and what is a costume — read the cellular
or HaLow page first if you want the fuller argument; the short version is below.

## What is real, and what is a costume

A LoRa gateway is **not** a Wi-Fi radio at all, and — unlike a HaLow AP — it is
**not an association point**. It is a LoRaWAN *packet forwarder*: it listens
across a sub-GHz band plan (US915 here, 902–928 MHz, eight 125 kHz uplink
channels plus a 500 kHz one) and forwards what it hears to a network server. It
keeps no table of the devices it hears, so there is exactly one thing to draw —
the gateway itself.

| | where it comes from |
|---|---|
| **Real, off the box** | gateway EUI, channel-plan/region, the concrete enabled channels it listens on (`/lora/channels`), the network server it forwards to, whether it is enabled, and the box's ethernet MAC — all via the RouterOS binary API on TCP 8728 (RouterOS 6 has no REST) |
| **Real, declared by you** | where the gateway sits on a floor plan, and optional cosmetic name / model |
| **Invented (a costume)** | the Wi-Fi band + channel reported downstream — a *stable, out-of-the-way* 5 GHz DFS channel, so the gateway does not read as a co-channel neighbour of a real UniFi radio |

The invented parts never overwrite the real ones. The AP is marked
`source="lora"`, `Radio.technology` is `"lora"`, and the true carrier — that it
is a LoRaWAN gateway, its region/plan, EUI and upstream server — rides on
`Radio.carrier_label`.

**One number is deliberately withheld.** A LoRaWAN gateway does not sit on a
single centre frequency — it spreads across a whole sub-band — so there is no
carrier to report and **`carrier_mhz` is left `null`** rather than filled with a
made-up figure. Identity is the box's own ethernet MAC, because the 64-bit
gateway EUI is not itself a 48-bit MAC.

**Why the end devices are not drawn.** The box's live *Traffic* sniffer does
show individual uplinks — DevAddr, real channel frequency, RSSI, SNR — and it is
genuinely useful RF. But it is not map data: a LoRaWAN `DevAddr` is reassigned
on every OTAA join (ephemeral, not a stable identity), and a *single* gateway's
RSSI cannot position a device (that needs multilateration across three or more
gateways). Piling every heard device on top of the gateway would be a wrong
answer dressed as a location, so nothing here fabricates client positions. If
you want device-level RF, the honest source is the packet-forwarder stream the
gateway already sends to its network server (here `DockerNUC` on UDP 1700), not
the coverage map.

## 1. Enable it

```bash
LORA_ENABLED=true
LORA_HOST=10.10.5.206            # the gateway's address (RouterOS API, TCP 8728)
LORA_USERNAME=admin              # a RouterOS user; read-only is ideal
LORA_PASSWORD=admin
```

That is enough to make the gateway appear on every surface. Everything else is
placement and cosmetics.

The account only needs to *read*: `/lora/print`, `/lora/servers/print`,
`/lora/channels/print`, `/interface/ethernet/print`, and
`/system/routerboard/print`. The bridge never reconfigures the gateway. A read-only RouterOS group (with `api` and `read`
policies) is enough, and better than reusing the admin login.

## 2. Put the gateway on the UniFi map

A LoRa gateway has no position of its own, the same as a cell or a HaLow AP. Two
ways to give it one, and the first is better:

**Anchor it to a UniFi AP** that is already placed on the console's own map. The
gateway then inherits that AP's position every poll — move the anchor in UniFi
and the gateway follows, with nothing to re-import.

```bash
LORA_ANCHOR_AP=Warehouse-AP      # the UniFi AP's name, as shown in the console
LORA_DX_PX=40                    # nudge off the anchor so the two don't stack
LORA_DY_PX=-20
```

**Or place it explicitly** on a floor plan, in image pixels — for a gateway with
no UniFi AP near it:

```bash
LORA_FLOORPLAN=<plan-id>         # from GET /api/floorplans
LORA_X_PX=512
LORA_Y_PX=300
```

Without either, the gateway still reaches the API and dashboard; it simply has
no place on a plan until one is configured.

## 3. Check what it is doing

`GET /api/lora` is the companion to `/api/cellular` and `/api/halow`: it says
plainly that the entry is a sub-GHz LoRaWAN gateway, what its real carrier is,
and which Wi-Fi channel it is wearing instead.

```jsonc
{
  "configured": true,
  "status": { "online": true, "name": "WLPC-Gateway",
              "gateway_id": "3235313254002800", "channel_plan": "us-915-1",
              "server": "DockerNUC (10.10.5.147)", "status": "Enabled" },
  "access_points": [{
    "name": "WLPC-Gateway", "mac": "2c:c8:1b:01:5f:a1", "online": true,
    "model": "RBwAPR-2nD",
    "real":    { "technology": "lora", "carrier_mhz": null,
                 "carrier": "LoRaWAN gateway (sub-GHz) (902-928 MHz (US915), plan us-915-1, active 8×125 kHz 902.3–903.7 MHz + 500 kHz @ 903 MHz, EUI 3235313254002800, → DockerNUC (10.10.5.147), Enabled)" },
    "costume": { "band": "5", "channel": 120, "channel_width_mhz": 20 }
  }]
}
```

## When the gateway goes away

A gateway that stops answering does not blank the rest of the map, and it does
not drop off it either: the AP stays where it was, greyed out (`online: false`,
no radio), exactly as a UniFi AP that lost power does. A gap on a coverage map
reads as "no coverage here", which is a different and wronger thing than "this
gateway is down". Before the *first* successful poll there is nothing to grey
out, so nothing is drawn — an AP invented from a first-poll failure would import
and then vanish when the gateway came up with its real MAC.

## Getting it into Hamina

Same story as everything else here: Hamina Live is pull-based and does not treat
this bridge as a native vendor, so the gateway reaches Hamina through the
Catalyst-compatible facade like the Wi-Fi, cellular and HaLow data do. See
[HAMINA.md](HAMINA.md) for the honest limits, and [CATALYST.md](CATALYST.md) for
the connector itself.
