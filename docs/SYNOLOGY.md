# Deploy on Synology (Container Manager)

DSM 7.2+ ships **Container Manager** (older DSM: **Docker**) — install it from
Package Center. Run the bridge on a Synology that's on the **same LAN as the
UniFi console** so it can reach `UNIFI_HOST`.

Two routes: pull the prebuilt image (recommended) or build from source.

## Route A — prebuilt image from GHCR (recommended)

A multi-arch image (Intel + ARM) is published to
`ghcr.io/shark-fi/unifi-hamina-live:latest`, so the NAS never has to build.
The image is **private**, so the NAS authenticates to GHCR to pull it.

### One-time: a token to pull the private image

GHCR doesn't accept your GitHub account password — create a token:

1. GitHub → **Settings → Developer settings → Personal access tokens → Tokens
   (classic) → Generate new token (classic)**.
2. Scope: **`read:packages`** only. Copy the token (starts `ghp_…`).

### Add GHCR to Container Manager

**Container Manager → Registry → Settings → Add**:
- Registry URL: `https://ghcr.io`
- Username: your GitHub username (the account that owns/can read the package)
- Password: the `read:packages` token

(Equivalent on the CLI: `docker login ghcr.io -u <github-user> -p <token>`.)

### Deploy

1. **SSH in** (Control Panel → Terminal & SNMP → Enable SSH) and make a folder:
   ```bash
   mkdir -p /volume1/docker/unifi-hamina-live/exports
   cd /volume1/docker/unifi-hamina-live
   ```
2. **Create `.env`** here with your settings (UniFi creds, `CATALYST_*`,
   `CF_TUNNEL_TOKEN`, `OPENINTENT_*` — see the main README / `.env.example`).
3. **Create `docker-compose.yml`** here:
   ```yaml
   services:
     unifi-hamina-live:
       image: ghcr.io/shark-fi/unifi-hamina-live:latest
       container_name: unifi-hamina-live
       ports: ["8080:8080"]          # change left side if DSM already uses 8080
       env_file: [.env]
       restart: unless-stopped
       volumes:
         - /volume1/docker/unifi-hamina-live/exports:/app/exports
         # OpenIntent refresh (optional): drop unifi_export.py on the NAS and mount it
         # - /volume1/docker/unifi_export.py:/exporter/unifi_export.py:ro
       # environment:
       #   OPENINTENT_EXPORTER_PATH: /exporter/unifi_export.py
     cloudflared:                     # omit this service if you don't need the tunnel
       image: cloudflare/cloudflared:latest
       container_name: cloudflared
       command: tunnel --no-autoupdate run
       environment:
         TUNNEL_TOKEN: ${CF_TUNNEL_TOKEN}
       depends_on: [unifi-hamina-live]
       restart: unless-stopped
   ```
4. **Container Manager → Project → Create** → Name `unifi-hamina-live`, Path =
   `/volume1/docker/unifi-hamina-live`, use the existing `docker-compose.yml` →
   **Next → Done**. It pulls the image and starts the containers.
5. **Verify:** the container shows **healthy**; open `http://<nas-ip>:8080/`.

### OpenIntent refresh with the prebuilt image

The exporter isn't inside the image. If you want the scheduled zip, put the one
file on the NAS and mount it (uncomment the two lines above):

```bash
# from a machine with repo access:
scp unifi_export.py admin@<nas-ip>:/volume1/docker/unifi_export.py
```

Because AP positions flow live, you can also set `OPENINTENT_REFRESH_SECONDS=0`
(generate once for the initial import) or skip the exporter entirely and seed
the import zip from any PC.

### Updating

```bash
cd /volume1/docker/unifi-hamina-live
docker compose pull && docker compose up -d
```
…or in the GUI: Project → **Action → Build/Pull** then **Up**.

## Route B — build from source on the NAS

No workflow needed; the NAS compiles the image.

1. `cd /volume1/docker && git clone https://github.com/shark-fi/unifi-hamina-live.git`
   and (for OpenIntent) `git clone https://github.com/shark-fi/unifi-hamina-export.git`
   as a sibling.
2. Create `.env` in `unifi-hamina-live/`.
3. Use a `docker-compose.yml` with `build: .` instead of `image:` and absolute
   volume paths, e.g. `- /volume1/docker/unifi-hamina-export:/exporter:ro` plus
   `OPENINTENT_EXPORTER_PATH: /exporter/unifi_export.py`.
4. Container Manager → Project → Create → point at the folder → **Build**.

## Notes

- **Port conflicts:** DSM services may hold `:8080`. Remap the host side, e.g.
  `"8686:8080"`, and browse `http://<nas-ip>:8686/`.
- **Reachability:** the NAS is on the LAN, so it reaches the console directly;
  no extra networking needed. For the tunnel, `cloudflared` dials out — no
  inbound ports on the NAS.
- **Permissions:** the `exports` folder is written by the container; keep it
  under `/volume1/docker/…` which the container runtime can write.
