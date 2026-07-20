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
