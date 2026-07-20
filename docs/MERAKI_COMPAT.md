# Meraki Dashboard API v1 — compatibility surface

The `/api/v1` facade implements the subset of the Meraki REST API a Live /
observability consumer needs to enumerate infrastructure and read wireless RF
state, backed by live UniFi data. Field names follow Meraki v1; expected but
unsupported fields are present with `null`/empty values so strict clients parse
cleanly.

## Auth

Present the configured `MERAKI_COMPAT_API_KEY` as either header:

```
X-Cisco-Meraki-API-Key: <key>
Authorization: Bearer <key>
```

If `MERAKI_COMPAT_API_KEY` is empty the facade runs **open** (no auth) — handy
for local testing, but set a key before exposing it anywhere.

## Identifier mapping

| Meraki concept | Maps to | ID form |
|---|---|---|
| Organization | the whole UniFi console/account | `O_<MERAKI_ORG_NAME>` (e.g. `O_UniFi`) |
| Network | a UniFi **site** | `N_<site-id>` (e.g. `N_default`) |
| Device | an **access point** | synthesized serial `Q2XX-XXXX-XXXX` (stable hash of the AP MAC) |
| Model | closest Meraki `MR*` model | real UniFi model kept in `notes` + `tags` |

Client radio bands map `2.4 → twoFourGhzSettings`, `5 → fiveGhzSettings`,
`6 → sixGhzSettings`.

## Endpoints

| Method & path | Returns |
|---|---|
| `GET /api/v1/organizations` | the single synthesized org |
| `GET /api/v1/organizations/{orgId}` | that org |
| `GET /api/v1/organizations/{orgId}/networks` | one network per UniFi site |
| `GET /api/v1/organizations/{orgId}/devices` | all APs |
| `GET /api/v1/organizations/{orgId}/devices/statuses` | AP online/offline + `lastReportedAt` |
| `GET /api/v1/organizations/{orgId}/devices/availabilities` | AP availability |
| `GET /api/v1/networks/{networkId}` | one network |
| `GET /api/v1/networks/{networkId}/devices` | APs on that site |
| `GET /api/v1/networks/{networkId}/floorPlans` | `[]` (placement comes via OpenIntent refresh — see HAMINA.md) |
| `GET /api/v1/networks/{networkId}/clients` | clients on that site |
| `GET /api/v1/networks/{networkId}/wireless/rfProfiles` | `[]` |
| `GET /api/v1/devices/{serial}` | one AP |
| `GET /api/v1/devices/{serial}/wireless/radio/settings` | per-band channel / width / target power |
| `GET /api/v1/devices/{serial}/wireless/status` | `basicServiceSets` per radio (channel, power, client count) |
| `GET /api/v1/devices/{serial}/clients` | clients on that AP |

## Deliberate gaps

- **Floor plans / geometry** are not part of the live poll; `floorPlans` is an
  empty list. Placement flows through the scheduled OpenIntent zip instead.
- **rfProfiles** are UniFi-managed and not exposed as Meraki profiles.
- Model mapping is approximate — it exists so model-keyed consumers don't choke,
  not to claim hardware equivalence. The true UniFi model is always in `notes`.

## Example

```bash
KEY=your-long-random-token
curl -s localhost:8080/api/v1/organizations -H "X-Cisco-Meraki-API-Key: $KEY"
curl -s localhost:8080/api/v1/organizations/O_UniFi/devices/statuses \
     -H "X-Cisco-Meraki-API-Key: $KEY"
# radio channel + tx power for one AP
SERIAL=$(curl -s localhost:8080/api/v1/organizations/O_UniFi/devices \
     -H "X-Cisco-Meraki-API-Key: $KEY" | jq -r '.[0].serial')
curl -s "localhost:8080/api/v1/devices/$SERIAL/wireless/radio/settings" \
     -H "X-Cisco-Meraki-API-Key: $KEY"
```
