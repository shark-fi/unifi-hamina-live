# Exposing the bridge to Hamina (or any cloud consumer)

The bridge runs on your **local network**, next to the UniFi console. Hamina
Live, and any other cloud consumer, calls in from **the cloud** — it cannot
reach an RFC1918 address like `192.168.x.x`. To be consumable it needs a
**public HTTPS URL with a CA-signed certificate** (a self-signed cert, fine for
talking to your local console, will be rejected by a cloud caller).

> Reality check: Hamina today offers a *Region* dropdown, not a custom-URL
> field, so there is nowhere to point it at your endpoint yet. Exposing the
> bridge is what makes it *integration-ready* (and useful for your own remote
> dashboards / monitoring) the moment Hamina adds a custom endpoint or native
> UniFi support. Until then the OpenIntent re-import is the path into Hamina.
> See [HAMINA.md](HAMINA.md).

Whichever method you choose, the bridge's own protections still apply: the
Meraki facade requires `MERAKI_COMPAT_API_KEY`, every route is read-only, and
you should additionally restrict access at the edge (below).

## Option 1 — Cloudflare Tunnel (recommended)

A reverse tunnel: `cloudflared` dials **out** from your LAN to Cloudflare, which
publishes a public `https://…` hostname with a valid cert. No port-forwarding,
no static IP, works behind NAT/CGNAT, and your UniFi console stays private.

1. In the **Cloudflare Zero Trust dashboard** → Networks → Tunnels, create a
   tunnel and copy its **token**.
2. Add a **public hostname** to the tunnel (e.g. `unifi-bridge.example.com`)
   with the service set to `http://unifi-hamina-live:8080`.
3. Put the token in `.env`:
   ```ini
   CF_TUNNEL_TOKEN=eyJ...
   ```
4. Start the app together with the tunnel:
   ```bash
   docker compose --profile tunnel up -d
   ```

The bridge is now at `https://unifi-bridge.example.com` — e.g.
`https://unifi-bridge.example.com/api/v1/organizations` with your
`X-Cisco-Meraki-API-Key`.

**Lock it down** with a Cloudflare **WAF / Zero Trust access policy**: allow only
the paths you expose (`/api/v1/*`), and if you know the consumer's egress IP
ranges, allow-list them. You can also require the API key at the edge.

### Access policy for the neutral `/api` routes (browser extension)

`EXPOSURE.md` was written around the Meraki facade, which has its own API key.
The **neutral `/api/*` routes do not** — they are unauthenticated by design,
expecting to sit on your LAN. Tunnelled without a policy in front, they publish
your whole client inventory (MACs, hostnames, IPs, SSIDs, per-AP association) to
anyone who learns the hostname. Put a Cloudflare Access application over them.

In **Zero Trust → Access → Applications → Add → Self-hosted**:

1. Application domain: your tunnel hostname, **path left empty** — the whole
   host. Scoping the policy to `api` leaves the dashboard at `/` open, and it
   renders the same APs and clients that `/api` returns.
2. Policy: *Allow*, with `Emails` = your own address. That is enough.
3. Save. `./scripts/check-tunnel.sh <hostname>` should now report the tunnel as
   refusing unauthenticated calls.

The **browser extension needs no credentials of its own**: open the tunnel
hostname in a tab and sign in once, and the extension's service worker reuses
that Access session cookie (it fetches with `credentials: "include"`). Only
headless callers need a **service token** (Zero Trust → Access → Service Auth),
sent as `CF-Access-Client-Id` / `CF-Access-Client-Secret`; add the token to the
application's policy, and pass it to the checker via those env vars.

### Access policy for a headless caller on `/dna/*` (Hamina's Catalyst connector)

The host-wide application above is right for the extension and **wrong for
Hamina**. Hamina's cloud calls the Catalyst facade headlessly: it follows the
Access `302` and gets an HTML login page where it expected a DNA auth token,
which surfaces in its UI as *"Connection to vendor system failed"* or
*"An unexpected error occurred"*. A service token is not a way out either —
Hamina's connector form offers only an Instance URL, a username, a password and
two TLS checkboxes, with **no field for `CF-Access-Client-*` headers**.

Access matches the **most specific path first**, so add a second application
that covers only the facade:

1. **Zero Trust → Access → Applications → Add → Self-hosted.**
2. Application domain: the same tunnel hostname, but **path `dna`** (the
   host-wide app leaves the path empty; that is what makes this one win for
   `/dna/*`).
3. Policy: **Action = Bypass**. For *Include*, prefer **IP ranges** over
   *Everyone*, so only Hamina's cloud skips Access. Hamina publishes its egress
   addresses per region — a handful of `/32`s, listed under *"which IP addresses
   do I need to allow"* in their
   [FAQ](https://docs.hamina.com/hamina/other/faqs) — and says to allow **all**
   of those under the regional instance you use (`us.hamina.com` vs
   `eu.hamina.com`). Read them from that page rather than from here: it is
   someone else's list and it will change without this file noticing.
4. Leave the host-wide application alone. `/api/*` and the dashboard stay behind
   Access, which is what the extension relies on.

**Before you do this, check `CATALYST_USERNAME` is set.** The facade
authenticates `/dna/*` itself — Basic auth, then an issued `X-Auth-Token` — so
bypassing Access does not leave the path open. But `check_basic()` falls into a
dev mode when `CATALYST_USERNAME` is **empty**, accepting any non-empty
username. Bypassing Access with an empty username genuinely does open it.

Verify both halves:

```bash
curl -s -o /dev/null -w 'dna: %{http_code}\n' \
  -X POST https://<hostname>/dna/system/api/v1/auth/token   # want 401
curl -s -o /dev/null -w 'api: %{http_code}\n' \
  https://<hostname>/api/health                             # want 302
```

`401` on the first means Access stepped aside and the facade's own auth
answered; `302` means the bypass is not matching yet. `302` on the second is the
answer you want — if it turns `200` or `401`, the new application is too broad
and you have opened the extension's data path to the internet.

### The Instance URL takes no port

Give Hamina `https://<hostname>` with **no port**. `8080` is the port *inside*
the Docker network, which `cloudflared` connects to; nothing outside the LAN
ever names it. Cloudflare's edge serves **HTTPS on 443, 2053, 2083, 2087, 2096
and 8443** — `8080` is one of its plain-**HTTP** ports, so `https://host:8080`
fails at the TLS handshake and reads as the vendor system being unreachable.

Leave both TLS checkboxes unticked: a Cloudflare Tunnel presents a real
certificate, so there is nothing self-signed to accept.

### Verifying it

```bash
./scripts/check-tunnel.sh unifi-bridge.example.com
```

Checks the bridge on the LAN, then the tunnel **without** credentials — where
the answer you want is a refusal. A tunnel published without a policy looks
perfectly healthy while serving everything to everyone, so the script treats a
JSON body there as a failure and says so loudly.

## Option 2 — Reverse proxy + DNS + Let's Encrypt

If you prefer to self-host the edge: forward `443` on your router to a reverse
proxy that terminates TLS and proxies to the bridge. [Caddy](https://caddyserver.com)
makes the cert automatic:

```caddyfile
unifi-bridge.example.com {
    reverse_proxy localhost:8080
}
```

Add a Dynamic-DNS record if you lack a static IP. This opens an inbound port, so
firewall it tightly and consider IP allow-listing the consumer.

## Option 3 — Cloud VPS relay

Keep the LAN fully sealed: run the public reverse proxy on a small VPS and have
the bridge dial out to it over WireGuard or reverse-SSH. The console and bridge
never accept inbound connections from the internet. More infra, best isolation.

## What NOT to do

- **Don't** expose the UniFi console itself — only the bridge's read-only HTTP
  surface needs to be reachable.
- **Don't** disable TLS verification on the public edge; cloud consumers require
  a valid, CA-signed certificate.
- **Don't** run the Meraki facade without `MERAKI_COMPAT_API_KEY` set once it is
  publicly reachable.
- **Don't** tunnel the neutral `/api/*` routes without an Access policy — they
  have no key of their own and will serve your client inventory to anyone.
