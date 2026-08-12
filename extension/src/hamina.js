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
  const BUILD = "h5";
  const POLL_MS = 15000;
  const UUID_RE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi;

  let bridgeBase = null;
  let ambiguous = false;
  let panel = null, body = null, statusEl = null;
  let timer = null, lastMapId = null, lastErr = null, lastRows = null;

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
        clients: [],
        perBand: {},
      };
    }
    for (const c of clients || []) {
      const ap = String(c.ap_mac || "").toLowerCase().replace(/[^0-9a-f]/g, "");
      const a = byMac[ap];
      if (!a) continue;
      a.clients.push({
        name: c.name || c.hostname || c.mac,
        mac: c.mac,
        ip: c.ip,
        ssid: c.essid,
        band: c.band,
        channel: c.channel,
        signal: c.signal_dbm != null ? c.signal_dbm : c.rssi,
        tx: c.tx_rate_kbps,
      });
      if (c.band) a.perBand[c.band] = (a.perBand[c.band] || 0) + 1;
    }
    // strongest first: the ones nearest the AP are the ones you are looking for
    for (const a of Object.values(byMac)) {
      a.clients.sort((x, y) => (y.signal ?? -999) - (x.signal ?? -999));
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
        user-select: none; touch-action: none;
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
      #${NS} .rrow { display: flex; justify-content: space-between; gap: 8px; }
      #${NS} .rc { color: #58a6ff; font-variant-numeric: tabular-nums; flex: none; }
      #${NS} .cl { cursor: pointer; text-decoration: underline dotted; }
      #${NS} .cl:hover { color: #79c0ff; }
      #${NS}-clients { position: fixed; z-index: 2147483001; width: 360px;
        max-height: 60vh; overflow: auto; background: #12161c; color: #e6edf3;
        border: 1px solid #2a313c; border-radius: 10px;
        font: 12px/1.45 -apple-system, "Segoe UI", Roboto, sans-serif;
        box-shadow: 0 8px 28px rgba(0,0,0,.5); }
      #${NS}-clients header { position: sticky; top: 0; background: #171c24;
        border-bottom: 1px solid #2a313c; padding: 8px 11px; display: flex;
        justify-content: space-between; gap: 8px; align-items: center;
        user-select: none; }
      #${NS}-clients h2 { font-size: 11px; margin: 0; letter-spacing: .4px;
        text-transform: uppercase; color: #9db2c9; font-weight: 600; }
      #${NS}-clients .row { padding: 7px 11px; border-bottom: 1px solid #1d232c;
        display: flex; justify-content: space-between; gap: 10px; }
      #${NS}-clients .row:last-child { border-bottom: 0; }
      #${NS}-clients .who { min-width: 0; }
      #${NS}-clients .who b { display: block; overflow: hidden;
        text-overflow: ellipsis; white-space: nowrap; }
      #${NS}-clients .meta { color: #8b97a5; }
      #${NS}-clients .sig { text-align: right; flex: none;
        font-variant-numeric: tabular-nums; }
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
    // delegated: the rows are re-rendered on every poll, so binding per row
    // would leak listeners and break the moment a name changes
    body.addEventListener("click", (e) => {
      const el = e.target.closest(".cl");
      if (el) showClients(el.dataset.ap);
    });
    makeDraggable(panel.querySelector("header"));
    restorePosition();
  }

  /* Drag by the header. The panel defaults to bottom-right, which is exactly
   * where Hamina puts its own AP detail card — so on any real floor plan one
   * covers the other. */
  function makeDraggable(handle) {
    handle.style.cursor = "move";
    handle.addEventListener("pointerdown", (e) => {
      if (e.target.closest(".x")) return;   // the close button is not a handle
      e.preventDefault();
      const r = panel.getBoundingClientRect();
      const dx = e.clientX - r.left, dy = e.clientY - r.top;
      // Switch from the right/bottom anchor to left/top before the first move,
      // or the panel jumps by its own width the moment the pointer travels.
      place(r.left, r.top);
      handle.setPointerCapture(e.pointerId);

      const move = (ev) => place(ev.clientX - dx, ev.clientY - dy);
      const up = () => {
        handle.removeEventListener("pointermove", move);
        handle.removeEventListener("pointerup", up);
        const box = panel.getBoundingClientRect();
        chrome.storage.local.set({ haminaPanelPos: { x: box.left, y: box.top } });
      };
      handle.addEventListener("pointermove", move);
      handle.addEventListener("pointerup", up);
    });
  }

  /* Keep the header reachable. A position saved from a wider window, or a drag
   * toward an edge, must not put the header outside the viewport — it is the
   * only way to move the panel back. */
  function place(x, y) {
    const r = panel.getBoundingClientRect();
    const maxX = window.innerWidth - Math.min(r.width, 120);
    const maxY = window.innerHeight - 40;
    panel.style.left = Math.max(0, Math.min(x, maxX)) + "px";
    panel.style.top = Math.max(0, Math.min(y, maxY)) + "px";
    panel.style.right = "auto";
    panel.style.bottom = "auto";
  }

  function restorePosition() {
    chrome.storage.local.get("haminaPanelPos").then(({ haminaPanelPos }) => {
      if (haminaPanelPos && panel) place(haminaPanelPos.x, haminaPanelPos.y);
    });
  }

  // A window that shrinks below a saved position would strand the panel.
  window.addEventListener("resize", () => {
    if (!panel || !panel.style.left) return;
    place(parseFloat(panel.style.left), parseFloat(panel.style.top));
  });

  function render(rows, note) {
    if (!body) return;
    lastRows = rows;
    if (!rows.length) {
      body.innerHTML = `<div class="ap warn">No APs matched between Hamina and UniFi.</div>`;
    } else {
      body.innerHTML = rows.map((r) => {
        if (!r.live) {
          return `<div class="ap"><div class="nm"><span>${esc(r.name)}</span>` +
            `<span class="warn">not in UniFi</span></div></div>`;
        }
        const perBand = r.live.perBand || {};
        const radios = r.live.radios.map((x) => {
          const n = perBand[x.band] || 0;
          const count = `<span class="rc">${n}</span>`;
          // No channel means the band is not transmitting — disabled, or the
          // radio has no live state yet. "ch?/20" read as a missing value we
          // had failed to fetch; it is a real state and worth naming. The
          // width/power/utilisation that follow are meaningless off air, so
          // they are dropped rather than shown as stale.
          if (x.channel == null) {
            return `<div class="rrow"><span><b>${esc(x.band)}G</b> ` +
              `<span class="dim">off air</span></span>${count}</div>`;
          }
          const detail = `<b>${esc(x.band)}G</b> ch${esc(x.channel)}` +
            (x.width ? `/${esc(x.width)}` : "") +
            (x.power != null ? ` ${esc(x.power)}dBm` : "") +
            (x.util != null ? ` · ${esc(Math.round(x.util))}% util` : "");
          return `<div class="rrow"><span>${detail}</span>${count}</div>`;
        }).join("");
        const off = r.live.online ? "" : ` <span class="off">offline</span>`;
        const n = r.live.clients.length;
        return `<div class="ap"><div class="nm"><span>${esc(r.name)}${off}</span>` +
          `<span class="cl" data-ap="${esc(r.name)}" title="show clients">` +
          `${n} client${n === 1 ? "" : "s"}</span></div>` +
          `<div class="rad">${radios || "no radios on air"}</div></div>`;
      }).join("");
    }
    statusEl.innerHTML = esc(note) + ` <code>${BUILD}</code>`;
    renderClients();   // follow the poll while the card is open
  }

  /* Client detail for one AP. Kept as a separate card rather than expanding
   * inline: the panel is narrow and already scrolls, and an accordion that
   * grows mid-poll pushes the row you just clicked out from under the cursor. */
  let openAp = null;

  function showClients(apName) {
    openAp = openAp === apName ? null : apName;   // clicking again closes it
    renderClients();
  }

  function renderClients() {
    const existing = document.getElementById(NS + "-clients");
    if (!openAp) { existing?.remove(); return; }
    const live = (lastRows || []).find((r) => r.name === openAp)?.live;
    let card = existing;
    if (!card) {
      card = document.createElement("div");
      card.id = NS + "-clients";
      document.body.appendChild(card);
      // sit beside the panel, not on top of it
      const p = panel.getBoundingClientRect();
      const left = p.left - 372 > 8 ? p.left - 372 : Math.min(p.right + 12,
        window.innerWidth - 372);
      card.style.left = Math.max(8, left) + "px";
      card.style.top = Math.max(8, p.top) + "px";
    }
    const rows = (live?.clients || []).map((c) => {
      const where = [c.ssid, c.band ? c.band + "G" : null,
                     c.channel ? "ch" + c.channel : null]
        .filter(Boolean).map(esc).join(" · ");
      const rate = c.tx ? ` · ${Math.round(c.tx / 1000)}M` : "";
      return `<div class="row"><span class="who"><b>${esc(c.name)}</b>` +
        `<span class="meta">${where}</span></span>` +
        `<span class="sig">${c.signal != null ? esc(c.signal) + " dBm" : "—"}` +
        `<span class="meta">${rate}</span></span></div>`;
    }).join("");
    card.innerHTML =
      `<header><h2>${esc(openAp)} — ${(live?.clients || []).length} client(s)</h2>` +
      `<span class="x" style="cursor:pointer;color:#8b97a5">✕</span></header>` +
      (rows || `<div class="row meta">No clients on this AP.</div>`);
    card.querySelector(".x").addEventListener("click", () => {
      openAp = null; renderClients();
    });
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
    if (!bridgeBase) {
      fail(ambiguous
        ? "more than one bridge is configured and none is chosen for Hamina — "
          + "pick one in the extension popup (Hamina overlay -> Bridge)"
        : "no bridge set — configure one in the extension popup");
      return;
    }

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
    // Naming the bridge is the whole point: nothing matching, with APs to spare
    // on the other side, is what pointing at the wrong console looks like, and
    // the panel used to present it as a property of the APs.
    let host = bridgeBase;
    try { host = new URL(bridgeBase).host; } catch (_e) { /* show it raw */ }
    render(rows,
      `${matched}/${hAps.length} matched` +
      (extra > 0 ? ` · ${extra} UniFi AP${extra === 1 ? "" : "s"} not on this map` : "") +
      (matched === 0 && live.length ? " — wrong bridge?" : "") +
      ` · via ${host}`);
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
    document.getElementById(NS + "-clients")?.remove();
    document.getElementById(NS + "-style")?.remove();
    openAp = null;
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
    // is configured, use it — one bridge, one site, and making the user type
    // the same URL twice earns nothing.
    //
    // Whatever is chosen, the panel says so. Observed in the field: a stored
    // haminaBridge kept pointing at the first console after a second was added
    // — it is copied from the console section when the overlay is enabled, so
    // it is only ever as right as that field happened to be — and the panel
    // reported "not in UniFi" against every AP on the map. Identical names on
    // both sides, so it reads as a name-matching bug. It was the wrong console,
    // and nothing on screen named the bridge. Ambiguity is refused out loud too.
    const map = s.bridges || {};
    const keys = Object.keys(map);
    bridgeBase = s.haminaBridge || (keys.length === 1 ? map[keys[0]] : null)
      || (keys.length === 0 ? s.bridge : null) || null;
    ambiguous = !s.haminaBridge && keys.length > 1;
    start();
  });
})();
