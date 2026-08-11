/* UniFi Live on Hamina — the "Q1" surface from docs/OVERLAY_EXTENSION.md.
 *
 * Hamina Live cannot pull UniFi data: its vendor connectors reach out from
 * Hamina's own cloud to a supported vendor's cloud, and every attempt to feed
 * it through the Catalyst facade is refused at model resolution (six different
 * model strings across two vendors, all rejected — see issue #1). So instead of
 * getting UniFi data *into* Hamina, this draws it *on top of* Hamina, in the
 * browser, where both sides are already reachable:
 *
 *   - Hamina's own GraphQL, same-origin with the user's session, tells us which
 *     APs are on the open map and where they sit (mapById.accessPoints).
 *   - The unifi-hamina-live bridge, fetched through the service worker, tells
 *     us what those APs are actually doing right now.
 *
 * Nothing is written back. Hamina never has to have an opinion about UniFi
 * hardware, which is the whole point: this path depends on no vendor
 * integration and no one else's roadmap.
 *
 * Why a panel and not dots on the map: Hamina's map is a single <canvas> with
 * no per-AP DOM nodes — verified on both a UniFi site and a working Juniper
 * Mist site, which render identically. The InnerSpace overlay pins to
 * `stats-tooltip-*` elements; there is no equivalent here, and pinning to
 * canvas pixels needs a pan/zoom transform Hamina does not expose. The panel
 * carries the same data and cannot drift out of alignment.
 */
(() => {
  "use strict";
  if (window.__unifiLiveHamina) return;
  window.__unifiLiveHamina = true;

  const NS = "unifi-live-hamina";
  const BUILD = "h2";
  const POLL_MS = 15000;
  const UUID_RE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi;

  let bridgeBase = null;
  let panel = null, body = null, statusEl = null;
  let timer = null, lastMapId = null, lastErr = null;

  /* ---------- helpers ---------------------------------------------------- */

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  /* Join key. Hamina's AP name comes from whatever placed it — for this repo's
   * users that is the OpenIntent import, so it matches the UniFi plan name. Be
   * forgiving about case, separators and the " (imported)" suffix UniFi used to
   * add (fixed in unifi-hamina-export#8, but existing plans still carry it). */
  const nameKey = (s) => String(s || "")
    .toLowerCase()
    .replace(/\s*\(imported\)\s*$/, "")
    .replace(/[\s_]+/g, "-")
    .replace(/[^a-z0-9-]/g, "")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");

  /* The open map's id. Hamina's URLs carry it as the last UUID in the path. */
  function mapIdFromUrl() {
    const found = location.pathname.match(UUID_RE);
    return found && found.length ? found[found.length - 1] : null;
  }

  /* Hamina's own API, same-origin with the user's session — the same call its
   * page makes. Read-only; we never mutate a project. */
  async function haminaGraphQL(operationName, query, variables) {
    const r = await fetch("/graphql", {
      method: "POST",
      credentials: "include",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ operationName, query, variables }),
    });
    if (!r.ok) throw new Error(`Hamina API HTTP ${r.status}`);
    const j = await r.json();
    if (j.errors?.length) throw new Error(j.errors[0]?.message || "Hamina API error");
    return j.data;
  }

  /* A deliberately minimal projection of Hamina's own MapAccessPoints query:
   * asking for fewer fields than its page does keeps us off schema churn we do
   * not need. Field names verified against a captured request. */
  const MAP_APS = `query UnifiLiveMapAccessPoints($id: ID!) {
  mapById(id: $id) { id accessPoints { id name x y make model } }
}`;

  async function haminaAccessPoints(mapId) {
    const d = await haminaGraphQL("UnifiLiveMapAccessPoints", MAP_APS, { id: mapId });
    return d?.mapById?.accessPoints || [];
  }

  /* The bridge is cross-origin and often plain HTTP on a LAN, so it goes
   * through the service worker — a content-script fetch would be blocked as
   * mixed content on this HTTPS page. Through a tunnel it carries the browser's
   * Cloudflare Access cookie, so no credential lives in the extension. */
  async function bridgeJson(path) {
    const reply = await chrome.runtime.sendMessage(
      { type: "get", url: bridgeBase.replace(/\/+$/, "") + path });
    if (!reply?.ok) throw new Error(reply?.error || "bridge unreachable");
    const r = reply.res;
    const text = String(r.text || "");
    if (/^\s*</.test(text)) {
      throw new Error(/cloudflareaccess\.com/i.test(text)
        ? "bridge needs an Access login — open it in a tab and sign in"
        : `bridge returned HTML (HTTP ${r.status})`);
    }
    if (!r.ok) throw new Error(`bridge HTTP ${r.status}`);
    return JSON.parse(text);
  }

  async function unifiLive() {
    const [aps, clients] = await Promise.all([
      bridgeJson("/api/access-points"), bridgeJson("/api/clients"),
    ]);
    const byMac = {};
    for (const a of aps || []) {
      const mac = String(a.mac || "").toLowerCase().replace(/[^0-9a-f]/g, "");
      if (!mac) continue;
      byMac[mac] = {
        name: a.name || a.mac,
        online: !!a.online,
        model: a.model || "",
        radios: (a.radios || []).filter((r) => r.band).map((r) => ({
          band: r.band,
          channel: r.channel,
          width: r.channel_width_mhz,
          power: r.tx_power_dbm,
          util: r.channel_utilization_pct,
          retries: r.tx_retries_pct,
        })),
        clients: 0,
      };
    }
    for (const c of clients || []) {
      const ap = String(c.ap_mac || "").toLowerCase().replace(/[^0-9a-f]/g, "");
      if (byMac[ap]) byMac[ap].clients += 1;
    }
    return Object.values(byMac);
  }

  /* ---------- rendering -------------------------------------------------- */

  function mount() {
    if (panel) return;
    const style = document.createElement("style");
    style.id = NS + "-style";
    style.textContent = `
      #${NS} { position: fixed; right: 16px; bottom: 16px; z-index: 2147483000;
        width: 330px; max-height: 62vh; overflow: auto; background: #12161c;
        color: #e6edf3; border: 1px solid #2a313c; border-radius: 10px;
        font: 12px/1.45 -apple-system, "Segoe UI", Roboto, sans-serif;
        box-shadow: 0 8px 28px rgba(0,0,0,.42); }
      #${NS} header { position: sticky; top: 0; background: #171c24;
        border-bottom: 1px solid #2a313c; padding: 8px 11px; display: flex;
        justify-content: space-between; align-items: center; gap: 8px; }
      #${NS} h1 { font-size: 11px; margin: 0; letter-spacing: .4px;
        text-transform: uppercase; color: #9db2c9; font-weight: 600; }
      #${NS} .x { cursor: pointer; color: #8b97a5; padding: 0 3px; }
      #${NS} .ap { padding: 8px 11px; border-bottom: 1px solid #1d232c; }
      #${NS} .ap:last-child { border-bottom: 0; }
      #${NS} .nm { font-weight: 600; display: flex; justify-content: space-between;
        gap: 8px; }
      #${NS} .cl { color: #58a6ff; font-variant-numeric: tabular-nums; }
      #${NS} .rad { color: #9db2c9; margin-top: 3px; }
      #${NS} .rad b { color: #e6edf3; font-weight: 600; }
      #${NS} .warn { color: #d29922; }
      #${NS} .dim { color: #6e7681; font-style: italic; }
      #${NS} .off { color: #f2544b; }
      #${NS} footer { padding: 7px 11px; color: #8b97a5; border-top: 1px solid #2a313c;
        position: sticky; bottom: 0; background: #171c24; }
      #${NS} code { color: #9db2c9; }
    `;
    document.documentElement.appendChild(style);

    panel = document.createElement("div");
    panel.id = NS;
    panel.innerHTML =
      `<header><h1>UniFi Live</h1><span class="x" title="hide">✕</span></header>` +
      `<div class="body"></div><footer class="status">starting…</footer>`;
    document.body.appendChild(panel);
    body = panel.querySelector(".body");
    statusEl = panel.querySelector(".status");
    panel.querySelector(".x").addEventListener("click", teardown);
  }

  function render(rows, note) {
    if (!body) return;
    if (!rows.length) {
      body.innerHTML = `<div class="ap warn">No APs matched between Hamina and UniFi.</div>`;
    } else {
      body.innerHTML = rows.map((r) => {
        if (!r.live) {
          return `<div class="ap"><div class="nm"><span>${esc(r.name)}</span>` +
            `<span class="warn">not in UniFi</span></div></div>`;
        }
        const radios = r.live.radios.map((x) => {
          // No channel means the band is not transmitting — disabled, or the
          // radio has no live state yet. "ch?/20" read as a missing value we
          // had failed to fetch; it is a real state and worth naming. The
          // width/power/utilisation that follow are meaningless off air, so
          // they are dropped rather than shown as stale.
          if (x.channel == null) {
            return `<b>${esc(x.band)}G</b> <span class="dim">off air</span>`;
          }
          return `<b>${esc(x.band)}G</b> ch${esc(x.channel)}` +
            (x.width ? `/${esc(x.width)}` : "") +
            (x.power != null ? ` ${esc(x.power)}dBm` : "") +
            (x.util != null ? ` · ${esc(Math.round(x.util))}% util` : "");
        }).join("<br>");
        const off = r.live.online ? "" : ` <span class="off">offline</span>`;
        return `<div class="ap"><div class="nm"><span>${esc(r.name)}${off}</span>` +
          `<span class="cl">${r.live.clients} client${r.live.clients === 1 ? "" : "s"}</span></div>` +
          `<div class="rad">${radios || "no radios on air"}</div></div>`;
      }).join("");
    }
    statusEl.innerHTML = esc(note) + ` <code>${BUILD}</code>`;
  }

  function fail(msg) {
    if (!statusEl) return;
    statusEl.innerHTML = `<span class="warn">${esc(msg)}</span> <code>${BUILD}</code>`;
  }

  /* ---------- the loop --------------------------------------------------- */

  async function tick() {
    const mapId = mapIdFromUrl();
    if (!mapId) { fail("open a floor plan to see live data"); return; }
    lastMapId = mapId;
    if (!bridgeBase) { fail("no bridge set — configure one in the extension popup"); return; }

    let hAps, live;
    try {
      hAps = await haminaAccessPoints(mapId);
    } catch (e) {
      fail("Hamina API: " + (e.message || e)); return;
    }
    try {
      live = await unifiLive();
    } catch (e) {
      lastErr = e.message || String(e);
      fail("bridge: " + lastErr); return;
    }

    const liveByKey = new Map(live.map((l) => [nameKey(l.name), l]));
    const rows = hAps.map((a) => ({
      name: a.name || a.id, live: liveByKey.get(nameKey(a.name)) || null,
    })).sort((a, b) => String(a.name).localeCompare(String(b.name)));

    const matched = rows.filter((r) => r.live).length;
    const extra = live.length - matched;
    render(rows,
      `${matched}/${hAps.length} matched` +
      (extra > 0 ? ` · ${extra} UniFi AP${extra === 1 ? "" : "s"} not on this map` : ""));
  }

  function start() {
    mount();
    tick();
    clearInterval(timer);
    timer = setInterval(tick, POLL_MS);
  }

  function teardown() {
    clearInterval(timer);
    timer = null;
    panel?.remove();
    document.getElementById(NS + "-style")?.remove();
    panel = body = statusEl = null;
  }

  /* Hamina is an SPA: the map changes without a navigation, so watch the URL. */
  let lastHref = location.href;
  setInterval(() => {
    if (location.href === lastHref) return;
    lastHref = location.href;
    if (panel && mapIdFromUrl() !== lastMapId) tick();
  }, 1200);

  chrome.storage.local.get(["haminaBridge", "bridges", "bridge"]).then((s) => {
    // An explicit Hamina bridge wins. Otherwise, if exactly one console bridge
    // is configured, use it — the common case is one bridge, one site, and
    // making the user type the same URL twice earns nothing.
    const map = s.bridges || {};
    const only = Object.keys(map).length === 1 ? map[Object.keys(map)[0]] : null;
    bridgeBase = s.haminaBridge || only || s.bridge || null;
    start();
  });
})();
