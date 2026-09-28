# Cisco Meraki APs on the live map

`MERAKI_LIVE_ENABLED=true` reads a Meraki organization's wireless APs from the
Dashboard API every poll and adds them to the same snapshot as the UniFi APs.
They then appear everywhere a UniFi AP does: `/api/access-points`, the
dashboard, the Meraki and Catalyst facades, and the extension's **Hamina
panel**, which joins them to the APs on the open Hamina map by name.

Not the same thing as [MERAKI_COMPAT.md](MERAKI_COMPAT.md): that serves a
Meraki-shaped API backed by UniFi; this reads a real Meraki org.

## Setup

1. Dashboard > **Organization > Settings**: enable Dashboard API access.
2. Dashboard > **My profile > API access**: generate a key.
3. In `.env`:

   ```
   MERAKI_LIVE_ENABLED=true
   MERAKI_LIVE_API_KEY=<key>
   MERAKI_LIVE_ORG_ID=<org id, if the key sees several>
   ```

4. Check `GET /api/meraki-live`: the org, networks, and each AP's radios.

For the Hamina panel, place the AP on the Hamina map under the same name it has
in Meraki Dashboard. For the console's own floor plan, anchor it to a placed
UniFi AP: `MERAKI_LIVE_ANCHORS="Home-Lab=U7-Pro-Bedroom@30,0"`.

## What is read

| Data | Endpoint | Refresh |
|---|---|---|
| APs, networks | `organizations/{org}/devices?productTypes[]=wireless`, `.../networks` | 5 min |
| Busy time per band | `organizations/{org}/wireless/devices/channelUtilization/byDevice` | 5 min |
| Online state | `organizations/{org}/devices/statuses` | every poll |
| Channel, width, power per band | `devices/{serial}/wireless/status` (a broadcasting BSS) | every poll |
| Clients | `networks/{net}/clients?timespan=300`, joined by `recentDeviceSerial` | every poll |

Meraki's client list has SSID, IP, vendor and usage but no per-client signal or
band, so those stay empty rather than guessed. A band with no broadcasting SSID
is left out. If the Dashboard can't be read, the APs seen before stay on the map
greyed out.

The API is limited to about 10 requests a second per organization; a small org
costs 3 + one per online AP + one per network each poll.
