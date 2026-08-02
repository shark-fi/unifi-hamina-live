/* UniFi Live for InnerSpace — content script (same-origin, read-only).
 *
 * Overlays live UniFi clients onto the InnerSpace floor plan. InnerSpace draws
 * the map on a WebGL <canvas>, but it also renders each AP's label as a DOM
 * <section data-testid="stats-tooltip-*"> whose CSS transform it keeps in sync
 * with the canvas as you pan/zoom. We read those live screen positions straight
 * from the DOM (no coordinate math, auto-tracks pan/zoom) and arrange each AP's
 * clients in a ring around it — UniFi reports clients per-AP, not with x,y, so
 * a ring around the AP is the honest representation.
 *
 * Each client is a small icon chip, outlined in its radio band's colour. Hover
 * one for its name and signal, click for full details. Band chips above the AP
 * filter the ring; hovering a channel chip spotlights that radio's clients.
 */
(() => {
  if (window.__unifiLiveInnerspace) return;
  window.__unifiLiveInnerspace = true;

  const REFRESH_MS = 5000;
  const MIN_SEP = 36;        // preferred px between client icon centres
  const DENSE_SEP = 30;      // below this the chips shrink to keep the gap open
  const NS = "unifi-live";
  const BUILD = "b23";        // shown in the status chip; bump on every change

  const BANDS = ["2.4", "5", "6", "?"];
  const BAND_CLS = { "2.4": "b24", "5": "b5", "6": "b6", "?": "bx" };
  const bandOf = (c) => (c.band === "2.4" || c.band === "5" || c.band === "6") ? c.band : "?";
  function bandCounts(cs) {
    const n = { "2.4": 0, "5": 0, "6": 0, "?": 0 };
    for (const c of cs) n[bandOf(c)]++;
    return n;
  }
  const norm = (s) => String(s || "").trim().toLowerCase();
  const macNorm = (m) => String(m || "").replace(/[^0-9a-fA-F]/g, "").toUpperCase();
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // --- API base + site --------------------------------------------------
  function siteId() {
    const p = location.pathname, i = p.indexOf("/network/");
    return i < 0 ? "default" : (p.slice(i + 9).split("/")[0] || "default");
  }
  /* Only URLs that FAILED for us are excluded from discovery. Excluding every
   * URL we fetched was wrong: on remote access the working base is the very one
   * the app itself uses, so once we fetched it we started skipping it. */
  const failedUrls = new Set();
  /* Derive the API prefix from ANY request the console made to its network
   * proxy — v1 (/proxy/network/api/s/...), v2 (/proxy/network/v2/api/site/...)
   * or integration (/proxy/network/integration/v1/...).
   *
   * Sampling performance.getEntriesByType() alone is not enough: the resource
   * timing buffer holds a few hundred entries and the map app can evict the
   * API calls before we look (a big console reported "0 seen" this way). So we
   * observe entries as they arrive, keep our own list, and raise the buffer. */
  const PROXY_RX = /^(https?:\/\/[^/]+(?:\/[^?#]*?)?\/proxy\/network)\//;
  /* Remote access can reach the console over a direct tunnel host —
   * https://<console-id>.id.ui.direct/... — which is a DIFFERENT ORIGIN from
   * the page. A console reached that way makes no /proxy/network/ request on
   * the page's own origin at all, which is why discovery kept coming up empty.
   * The tunnel host is the console root, so its API sits at /proxy/network/api. */
  const DIRECT_RX = /^https?:\/\/([a-z0-9-]+\.id\.ui\.direct)(?::\d+)?\//i;
  const perfBases = [];                 // newest first, deduped
  function notePerfUrl(url) {
    const s = String(url || "");
    const m = s.match(PROXY_RX);
    const d = m ? null : s.match(DIRECT_RX);
    if (!m && !d) return;
    const base = m ? m[1] + "/api" : `https://${d[1]}/proxy/network/api`;
    const i = perfBases.indexOf(base);
    if (i === 0) return;
    if (i > 0) perfBases.splice(i, 1);
    perfBases.unshift(base);
  }
  // src/probe.js runs in the page's own context and reports every API URL the
  // app requests — the only vantage point that survives buffer eviction and
  // also covers websockets.
  window.addEventListener("unifi-live-api", (e) => notePerfUrl(e.detail));
  try { performance.setResourceTimingBufferSize?.(3000); } catch (_e) { /* ignore */ }
  try {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) notePerfUrl(e.name);
    }).observe({ type: "resource", buffered: true });
  } catch (_e) { /* fall back to sampling below */ }

  function basesFromPerf() {
    try {   // sweep whatever is still buffered, in case the observer missed it
      const ents = performance.getEntriesByType("resource");
      for (let k = ents.length - 1; k >= 0; k--) notePerfUrl(ents[k].name);
    } catch (_e) { /* ignore */ }
    return perfBases.slice();
  }
  function candidateBases() {
    const p = location.pathname, i = p.indexOf("/network/");
    const prefix = i < 0 ? "" : p.slice(0, i);
    const o = location.origin, list = [`${o}${prefix}/proxy/network/api`];
    const m = prefix.match(/\/consoles\/([^/]+)/);
    if (m) {
      const full = m[1], hex = full.split(":")[0];   // id may drop the :epoch part
      for (const id of [...new Set([full, hex])]) {
        list.push(`${o}/proxy/consoles/${id}/proxy/network/api`);
        list.push(`${o}/consoles/${id}/proxy/network/api`);
        list.push(`${o}/proxy/network/${id}/proxy/network/api`);
      }
    }
    list.push(`${o}/proxy/network/api`);
    return [...new Set(list)];
  }
  const sameOrigin = (u) => {
    try { return new URL(u, location.href).origin === location.origin; }
    catch (_e) { return true; }
  };

  async function rawGet(url) {
    if (sameOrigin(url)) {
      const r = await fetch(url, { credentials: "include", headers: { Accept: "application/json" } });
      return { ok: r.ok, status: r.status, type: r.headers.get("content-type") || "",
               text: await r.text() };
    }
    // cross-origin (the tunnel host): the worker is not bound by page CORS
    const reply = await chrome.runtime.sendMessage({ type: "get", url });
    if (!reply?.ok) throw new Error(reply?.error || "worker fetch failed");
    return reply.res;
  }

  /* Judge the body, not the Content-Type header: some consoles answer with
   * text/plain, and an HTML body tells us plainly whether we hit an app shell
   * (wrong path) or a login page (not authenticated on that host). The HTTP
   * status is carried into the message so those cases stay distinguishable. */
  async function getJson(url) {
    let r;
    try { r = await rawGet(url); } catch (e) { failedUrls.add(url); throw e; }
    const body = String(r.text || "");
    const head = body.trimStart().slice(0, 1);
    if (head === "<") {
      failedUrls.add(url);
      throw new Error(`HTML not JSON (HTTP ${r.status}${r.status === 200 ? ", wrong path" : ""})`);
    }
    if (!r.ok) { failedUrls.add(url); throw new Error(`HTTP ${r.status}`); }
    try {
      return JSON.parse(body);
    } catch (_e) {
      failedUrls.add(url);
      throw new Error(`unparseable body (HTTP ${r.status}, ${body.length}B)`);
    }
  }

  let apByName = {}, lastErr = null, apiBase = null, apiV2 = false;
  /* Some remote sessions carry console traffic inside a WebRTC data channel
   * (UniFi reports "Rtc-Cloudflare / Ok-Relay"): the page makes no HTTP API
   * request at all, and the <id>.id.ui.direct host it does contact serves only
   * the SSO handshake, answering 200-with-HTML for API paths. Nothing we can
   * fetch exists in that mode, so say so instead of probing forever. */
  let relayOnly = false;

  /* Remember a base that worked. The performance resource buffer is finite and
   * evicts old entries, so on a long-lived page the app's own API call can age
   * out of it — without this, discovery would have nothing to learn from. */
  const BASE_KEY = () => "apiBase:" + location.origin +
    (location.pathname.split("/network/")[0] || "");
  let savedBase = null, savedLoaded = false;
  async function loadSavedBase() {
    if (savedLoaded) return savedBase;
    savedLoaded = true;
    try {
      const k = BASE_KEY();
      savedBase = (await chrome.storage.local.get(k))[k] || null;
    } catch (_e) { savedBase = null; }
    return savedBase;
  }
  function rememberBase(b) {
    savedBase = b;
    try { chrome.storage.local.set({ [BASE_KEY()]: b }); } catch (_e) { /* ignore */ }
  }

  let lastTriedBase = null, workerBases = 0, seenBases = [];
  /* The tunnel host answering an API path with the app shell (HTML at 200) is
   * the signature of a relayed session — the host serves only the SSO handshake
   * and there is no HTTP API behind it. Checked after BOTH the v1 and v2
   * attempts: previously only v1 set it, so whenever v1 failed for some other
   * reason and v2 was the one that hit the shell, the overlay reported a plain
   * "API error" and sent you hunting for a path bug that doesn't exist. */
  const isShell = (msg) => /HTTP 200, wrong path/.test(msg);
  // Are we on a remote-access page at all? A LAN console is an IP or a bare
  // hostname and never has a /consoles/ path, so this keeps the relay verdict
  // off local consoles, where 200-with-HTML really does mean a wrong path.
  const isRemotePage = () =>
    /\/consoles\//.test(location.pathname) ||
    /(^|\.)ui\.com$/i.test(location.hostname) ||
    seenBases.some((b) => /id\.ui\.direct/.test(b));

  async function resolveBase(site) {
    if (apiBase) return apiBase;
    const saved = await loadSavedBase();
    seenBases = basesFromPerf();
    workerBases = seenBases.length;
    if (seenBases.length) console.warn("[UniFi Live] API bases observed:", seenBases);
    const tries = [...new Set([...seenBases, saved, ...candidateBases()]
      .filter(Boolean)
      .filter((b) => !failedUrls.has(`${b}/s/${site}/stat/device`)))];
    if (!tries.length) tries.push(...candidateBases());
    let lastE = "no reachable API", shells = 0;
    const fail = (b, e) => {
      lastE = e.message;
      if (isShell(lastE)) shells++;
      // the tunnel host itself answering with the app shell is conclusive
      if (/id\.ui\.direct/.test(b) && isShell(lastE)) relayOnly = true;
    };
    for (const b of tries) {
      lastTriedBase = b;
      try {
        const j = await getJson(`${b}/s/${site}/stat/device`);
        if (Array.isArray(j.data)) { apiBase = b; apiV2 = false; rememberBase(b); return b; }
      } catch (e) { fail(b, e); }
      // some consoles serve only the v2 API — accept that shape too
      try {
        const v2 = b.replace(/\/api$/, "/v2/api");
        const j2 = await getJson(`${v2}/site/${site}/device`);
        const list = Array.isArray(j2) ? j2 : (j2.network_devices || j2.data);
        if (Array.isArray(list)) { apiBase = b; apiV2 = true; rememberBase(b); return b; }
      } catch (e) { fail(b, e); }
    }
    /* An API path answering with the app shell, on a remote-access page, means
     * there is no HTTP API there to find — the relayed session. Checking only
     * the tunnel-host URL wasn't enough: on unifi.ui.com the same shell comes
     * back for our own-origin candidates too, so whichever happened to be tried
     * last decided the message, and a run could end reporting a bare path error
     * on a session where no API exists at all. (A local console is excluded:
     * there, 200-with-HTML really does mean we asked for the wrong path.) */
    if (shells && isRemotePage()) relayOnly = true;
    throw new Error(lastE);
  }

  /* UniFi's own client icons. The console serves them from its CDN keyed by the
   * numeric fingerprint device id that stat/sta already carries:
   *   https://static.ui.com/fingerprint/0/<dev_id>_101x101.png
   * So there is no database to fetch and no uuid to resolve — the id is already
   * in the payload we poll. The console loads icons from that host itself, so
   * the page CSP allows them; no host permission is involved either, since the
   * page requests the image, not us. An id with no artwork 404s and the chip
   * falls back to its glyph (bound in renderGroup, since CSP blocks onerror=). */
  const ICON_URL = (id) => `https://static.ui.com/fingerprint/0/${id}_101x101.png`;

  // dev_id_override wins when set: that's a user-corrected fingerprint, and it's
  // what the console's own client list renders.
  const devIdOf = (c) => c.dev_id_override ?? c.dev_id ??
    c.fingerprint?.computed_dev_id ?? c.fingerprint?.dev_id ?? null;
  const uiIconFor = (c) => (c.devId != null ? ICON_URL(c.devId) : null);

  /* Per-radio channel state, so we can reproduce UniFi's own channel chips
   * (and their Utilization / TX Retries panel) on remote access, where the
   * console renders the model there instead. Field names match the ones the
   * bridge's normalizer uses against real payloads. */
  const RADIO_BAND = { ng: "2.4", na: "5", "6e": "6", ad: "6" };
  const HT_WIDTHS = [20, 40, 80, 160, 320];
  function radiosOf(d) {
    const stats = {};
    for (const st of d.radio_table_stats || []) stats[st.radio] = st;
    const table = (d.radio_table || []).length
      ? d.radio_table : Object.keys(stats).map((r) => ({ radio: r }));
    const out = [];
    for (const rt of table) {
      const st = stats[rt.radio] || {};
      const band = RADIO_BAND[rt.radio || st.radio];
      if (!band) continue;
      const ch = Number.isInteger(st.channel) ? st.channel
        : (Number.isInteger(rt.channel) ? rt.channel : null);
      const ht = parseInt(rt.ht, 10);
      const util = typeof st.cu_total === "number" ? st.cu_total
        : (typeof st.channel_utilization === "number" ? st.channel_utilization : null);
      out.push({
        band, channel: ch,
        width: HT_WIDTHS.includes(ht) ? ht : null,
        util,
        retries: typeof st.tx_retries_pct === "number" ? st.tx_retries_pct : null,
      });
    }
    return out.filter((r) => r.channel != null)
      .sort((a, b) => BANDS.indexOf(a.band) - BANDS.indexOf(b.band));
  }

  const kbps = (v) => v == null ? null : (v >= 1000 ? (v / 1000).toFixed(1) + " Mbps" : v + " Kbps");
  const bytes = (v) => {
    if (v == null) return null;
    const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0, n = v;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return n.toFixed(n >= 10 || i === 0 ? 0 : 1) + " " + u[i];
  };
  const dur = (s) => {
    if (s == null) return null;
    const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
    return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
  };

  let lastCtx = null;
  function resetConsoleState() {
    apiBase = null; savedBase = null; savedLoaded = false;
    apByName = {}; lastErr = null; lastTriedBase = null; lastCtx = null;
    failedUrls.clear();
    for (const [, g] of groups) { g.sig = ""; g.el.style.display = "none"; }
  }

  /* v1 and v2 differ in both paths and field names; keep the differences here
   * and hand the rest of the code one shape. */
  async function fetchLists(base, site) {
    if (!apiV2) {
      const [dev, sta] = await Promise.all([
        getJson(`${base}/s/${site}/stat/device`),
        getJson(`${base}/s/${site}/stat/sta`),
      ]);
      return [dev.data || [], sta.data || []];
    }
    const v2 = base.replace(/\/api$/, "/v2/api");
    const [dev, sta] = await Promise.all([
      getJson(`${v2}/site/${site}/device`),
      getJson(`${v2}/site/${site}/clients/active`),
    ]);
    const dl = Array.isArray(dev) ? dev : (dev.network_devices || dev.data || []);
    const cl = Array.isArray(sta) ? sta : (sta.clients || sta.data || []);
    return [dl, cl];
  }

  function bandFromRadio(c) {
    const r = String(c.radio || "").toLowerCase();
    if (r === "na") return "5";
    if (r === "ng") return "2.4";
    if (r === "6e") return "6";
    const b = String(c.band || c.radio_band || "").toLowerCase();
    if (b.startsWith("2")) return "2.4";
    if (b.startsWith("5")) return "5";
    if (b.startsWith("6")) return "6";
    const ch = c.channel;
    if (Number.isInteger(ch)) return ch <= 14 ? "2.4" : null;   // 5/6 overlap, stay honest
    return null;
  }

  async function refreshData() {
    const site = siteId();
    if (relayOnly) { updateStatus(); return; }   // nothing to fetch in relay mode
    // switching console (or site) changes the API prefix entirely — drop any
    // base/icon state learned for the previous one instead of reusing it
    const ctx = location.origin + (location.pathname.split("/network/")[0] || "") + "|" + site;
    if (ctx !== lastCtx) { resetConsoleState(); lastCtx = ctx; }
    try {
      const base = await resolveBase(site);
      const [devList, staList] = await fetchLists(base, site);
      const byMac = {};
      for (const d of devList) {
        // v2 payloads name the type differently; an AP is anything with radios
        const isAp = d.type ? d.type === "uap"
          : !!(d.radio_table || d.radio_table_stats || d.radios);
        if (!isAp) continue;
        byMac[macNorm(d.mac)] = { name: d.name || d.mac, online: d.state === 1, radios: radiosOf(d) };
      }
      const cliByMac = {};
      for (const c of staList) {
        if (c.is_wired || c.type === "WIRED") continue;
        const ap = macNorm(c.ap_mac || c.uplink_mac || c.uplink?.mac);
        if (!ap) continue;
        (cliByMac[ap] ||= []).push({
          mac: macNorm(c.mac),
          name: c.name || c.hostname || null,
          hostname: c.hostname || null,
          ip: c.ip || null,
          band: bandFromRadio(c),
          channel: c.channel ?? null,
          signal: c.signal ?? null,
          noise: c.noise ?? null,
          essid: c.essid || null,
          tx: c.tx_rate ?? null,
          rx: c.rx_rate ?? null,
          txb: c.tx_bytes ?? null,
          rxb: c.rx_bytes ?? null,
          uptime: c.uptime ?? null,
          guest: !!c.is_guest,
          devId: devIdOf(c),
          oui: c.oui || null,
          vendor: c.dev_vendor || null,
          note: c.note || null,
        });
      }
      const next = {};
      for (const [mac, ap] of Object.entries(byMac)) {
        const list = (cliByMac[mac] || []).sort(
          (a, b) => BANDS.indexOf(bandOf(a)) - BANDS.indexOf(bandOf(b)) ||
                    (b.signal ?? -999) - (a.signal ?? -999));
        next[norm(ap.name)] = { online: ap.online, clients: list, radios: ap.radios || [] };
      }
      apByName = next;
      lastErr = null;
    } catch (e) {
      lastErr = e.message;   // keep any validated base; re-probe only if unset
    }
    refreshOpenCard();
    updateStatus();
  }

  // --- client icons -----------------------------------------------------
  const ICONS = [
    [/iphone|android|pixel|galaxy|phone|moto|oneplus/, "📱"],
    [/ipad|tablet|kindle|fire hd/, "📒"],
    [/macbook|laptop|thinkpad|notebook|xps/, "💻"],
    [/imac|desktop|pc\b|workstation|nuc/, "🖥️"],
    [/\btv\b|roku|appletv|apple tv|firestick|chromecast|shield|vizio|samsung tv|lg tv/, "📺"],
    [/echo|alexa|sonos|homepod|speaker|soundbar|nest mini|nest audio/, "🔊"],
    [/cam|camera|ring|doorbell|protect|g4|g5|ptz/, "📷"],
    [/print|hp |epson|brother|canon/, "🖨️"],
    [/xbox|playstation|\bps4\b|\bps5\b|switch\b|steam/, "🎮"],
    [/watch|fitbit|garmin/, "⌚"],
    [/thermostat|ecobee|nest|hvac|furnace|water|softener|filter|sensor|siren|chime|gateway/, "🏠"],
    [/light|lamp|bulb|hue|lifx|led/, "💡"],
    [/plug|outlet|kasa|switch bot/, "🔌"],
    [/door|lock|fob|garage/, "🚪"],
    [/dishwasher|washer|dryer|fridge|refriger|oven/, "🧺"],
  ];
  function iconFor(c) {
    const t = norm([c.name, c.hostname, c.oui, c.vendor, c.note].filter(Boolean).join(" "));
    for (const [rx, glyph] of ICONS) if (rx.test(t)) return glyph;
    return null; // caller falls back to an initial
  }
  const labelFor = (c) => c.name || c.hostname || c.mac;
  const initialFor = (c) => {
    const s = labelFor(c).replace(/[^a-z0-9]/gi, "");
    return (s[0] || "?").toUpperCase();
  };

  // --- overlay ----------------------------------------------------------
  let overlay, statusEl, card, tip;
  const groups = new Map();          // apName -> {el, sig}
  const filters = new Map();         // apName -> band | null
  let selected = null;               // {ap, mac}

  function ensureOverlay() {
    if (overlay && document.body.contains(overlay)) return;
    document.querySelectorAll(`#${NS}-overlay, #${NS}-status, #${NS}-card, #${NS}-tip`)
      .forEach((el) => el.remove());
    overlay = document.createElement("div");
    overlay.id = NS + "-overlay";
    Object.assign(overlay.style, { position: "fixed", inset: "0", pointerEvents: "none", zIndex: "2147483000" });
    const style = document.createElement("style");
    style.textContent = `
      #${NS}-overlay .grp { position: fixed; transform: translate(-50%, -50%); will-change: left, top; }
      /* chips mirror UniFi's channel chips: dark pill + band-coloured underline.
         client counts sit ABOVE the marker; our channel chips below the labels. */
      /* --s tracks the zoom InnerSpace has applied to its own marker, so our
         chip rows grow and shrink in step with its channel chips */
      #${NS}-overlay .badges { position: absolute; left: 50%;
        transform: translateX(-50%) scale(var(--s, 1)); transform-origin: top center;
        display: flex; gap: 4px; white-space: nowrap; pointer-events: auto; }

      #${NS}-overlay .chip { position: relative; display: inline-flex;
        align-items: center; justify-content: center; min-width: 24px; height: 20px;
        padding: 0 6px 3px; border-radius: 6px; background: #24272c; color: #fff;
        font: 600 11px/17px system-ui, sans-serif; overflow: hidden;
        box-shadow: 0 2px 6px rgba(0,0,0,.45); }
      #${NS}-overlay .chip.cl { cursor: pointer; }
      #${NS}-overlay .chip .bar { position: absolute; left: 0; right: 0; bottom: 0; height: 3px; }
      #${NS}-overlay .chip.on { background: #006fff; }
      #${NS}-overlay .chip.dim { opacity: .45; }
      /* stay out of the way of UniFi's own marker tooltips */
      #${NS}-overlay .grp.fade { opacity: .12; }
      #${NS}-overlay .grp.fade .chip,
      #${NS}-overlay .grp.fade .cli { pointer-events: none; }
      /* hovering a channel chip — ours or UniFi's — spotlights that radio:
         everything on the other bands drops back instead of the whole group */
      #${NS}-overlay .grp[data-hi] .cli { opacity: .12; transition: opacity .12s ease; }
      #${NS}-overlay .grp[data-hi="2.4"] .cli.b24,
      #${NS}-overlay .grp[data-hi="5"] .cli.b5,
      #${NS}-overlay .grp[data-hi="6"] .cli.b6,
      #${NS}-overlay .grp[data-hi="?"] .cli.bx { opacity: 1; }
      #${NS}-overlay .cli { position: absolute; width: 22px; height: 22px; margin: -11px;
        border-radius: 50%; background: #131722; border: 2px solid #2b6cff;
        box-shadow: 0 2px 6px rgba(0,0,0,.5); cursor: pointer; pointer-events: auto;
        display: flex; align-items: center; justify-content: center;
        font: 600 11px/1 system-ui, sans-serif; color: #e6e9ef;
        transition: left .7s ease, top .7s ease, transform .12s ease; }
      #${NS}-overlay .cli:hover { transform: scale(1.25); z-index: 5; }
      #${NS}-overlay .cli.sel { outline: 2px solid #fff; outline-offset: 1px; }
      #${NS}-overlay .cli.guest { border-style: dashed; }
      #${NS}-overlay .cli.b24 { border-color: #e0a83c; }
      #${NS}-overlay .cli.b5  { border-color: #2b6cff; }
      #${NS}-overlay .cli.b6  { border-color: #a855f7; }
      #${NS}-overlay .cli.bx  { border-color: #8b93a7; }
      #${NS}-overlay .cli .g { font-size: 12px; line-height: 1; }
      #${NS}-overlay .cli img.ci { width: 16px; height: 16px; display: block;
        border-radius: 3px; object-fit: contain; }
      /* a crowded AP: smaller chips so the capped rings still breathe */
      #${NS}-overlay .grp.dense .cli { width: 18px; height: 18px; margin: -9px;
        border-width: 1.5px; font-size: 9px; }
      #${NS}-overlay .grp.dense .cli .g { font-size: 10px; }
      #${NS}-overlay .grp.dense .cli img.ci { width: 13px; height: 13px; }
      #${NS}-overlay .bar.b24 { background: #e0a83c; } #${NS}-overlay .bar.b5 { background: #2b6cff; }
      #${NS}-overlay .bar.b6  { background: #a855f7; } #${NS}-overlay .bar.bx { background: #8b93a7; }
      /* hover panel, matching UniFi's chip tooltip */
      #${NS}-tip { position: fixed; z-index: 2147483003; background: #24272c; color: #fff;
        border-radius: 10px; padding: 11px 13px; min-width: 176px; max-width: 260px;
        font: 12px/1.5 system-ui, sans-serif; box-shadow: 0 10px 28px rgba(0,0,0,.5);
        pointer-events: none; opacity: 0; transition: opacity .12s ease; }
      #${NS}-tip .t { font: 600 13px/1.35 system-ui, sans-serif; margin-bottom: 6px;
        overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      #${NS}-tip .r { display: flex; justify-content: space-between; gap: 18px; }
      #${NS}-tip .r span:first-child { color: #9aa0a6; }
      #${NS}-tip .r span:last-child { white-space: nowrap; }
      #${NS}-status { position: fixed; left: 14px; bottom: 14px; z-index: 2147483001;
        background: #131722ee; color: #cfd6e4; font: 12px/1.4 system-ui, sans-serif;
        padding: 7px 11px; border-radius: 8px; border: 1px solid #2a3346;
        pointer-events: none; box-shadow: 0 6px 20px rgba(0,0,0,.4); }
      #${NS}-status b { color: #fff; font-weight: 700; }
      #${NS}-status i { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
        margin-right: 4px; }
      #${NS}-status i.b24 { background: #e0a83c; } #${NS}-status i.b5 { background: #2b6cff; }
      #${NS}-status i.b6 { background: #a855f7; } #${NS}-status i.bx { background: #8b93a7; }
      #${NS}-card { position: fixed; z-index: 2147483002; width: 236px;
        background: #131722f7; color: #e6e9ef; border: 1px solid #2a3346; border-radius: 10px;
        font: 12px/1.45 system-ui, sans-serif; box-shadow: 0 10px 30px rgba(0,0,0,.55);
        pointer-events: auto; overflow: hidden; }
      #${NS}-card .hd { display: flex; align-items: center; gap: 7px; padding: 9px 10px;
        border-bottom: 1px solid #2a3346; }
      #${NS}-card .hd .t { font-weight: 700; overflow: hidden; text-overflow: ellipsis;
        white-space: nowrap; }
      #${NS}-card .hd .x { margin-left: auto; cursor: pointer; color: #8b93a7; padding: 0 2px; }
      #${NS}-card .hd .x:hover { color: #fff; }
      #${NS}-card dl { margin: 0; padding: 8px 10px 10px; display: grid;
        grid-template-columns: auto 1fr; gap: 3px 10px; }
      #${NS}-card dt { color: #8b93a7; }
      #${NS}-card dd { margin: 0; text-align: right; overflow: hidden;
        text-overflow: ellipsis; white-space: nowrap; }
      #${NS}-card .pill { padding: 0 6px; border-radius: 8px; font-weight: 700; font-size: 10px; }`;
    overlay.appendChild(style);
    document.body.appendChild(overlay);

    statusEl = document.createElement("div");
    statusEl.id = NS + "-status";
    statusEl.textContent = "UniFi Live: starting…";
    document.body.appendChild(statusEl);

    tip = document.createElement("div");
    tip.id = NS + "-tip";
    document.body.appendChild(tip);

    overlay.addEventListener("mouseover", (e) => {
      const chip = e.target.closest?.(".chip");
      const cli = e.target.closest?.(".cli");
      const grp = e.target.closest?.(".grp");
      if (!grp) return;
      const apName = [...groups].find(([, g]) => g.el === grp)?.[0];
      if (!apName) return;
      if (chip) {
        // our own chips spotlight their band too, same as UniFi's channel chips
        hoveredAp = apName;
        hoverBand = chip.dataset.band || null;
        showTip(chip.classList.contains("ch")
          ? chanTip(apName, chip.dataset.band) : chipTip(apName, chip.dataset.band), chip);
      } else if (cli) showTip(clientTip(apName, cli.dataset.mac), cli);
    });
    overlay.addEventListener("mouseout", (e) => {
      if (e.target.closest?.(".chip")) { hoveredAp = null; hoverBand = null; }
      if (e.target.closest?.(".chip, .cli")) hideTip();
    });

    document.addEventListener("mouseover", onDocHover, true);
    document.addEventListener("mouseout", onDocHover, true);

    document.addEventListener("mousedown", (e) => {
      if (card && !card.contains(e.target) && !e.target.closest?.(`#${NS}-overlay .cli`)) closeCard();
    }, true);
  }

  /* While the pointer is over UniFi's own AP marker we fade our overlay for that
   * AP, so its channel tooltip is never covered by our chips/icons.
   *
   * Except on one of its channel chips: there the useful thing is to see which
   * clients are on that radio, so instead of fading everything we spotlight that
   * band and drop the other bands back. UniFi's chips carry only the channel
   * number, so we match it against the radios we already polled to learn which
   * band the chip is. */
  let hoveredAp = null, hoverBand = null;
  function chanBandAt(el, sec, ap) {
    for (let n = el; n && n !== sec; n = n.parentElement) {
      const t = (n.textContent || "").trim();
      if (!/^\d{1,4}$/.test(t)) continue;
      const hit = (ap?.radios || []).find((r) => r.channel === Number(t));
      return hit ? hit.band : null;
    }
    return null;
  }
  function onDocHover(e) {
    const sec = e.target.closest?.('section[data-testid^="stats-tooltip-"]');
    const name = sec ? norm(sec.querySelector('[data-testid="title"]')?.textContent) : null;
    const next = e.type === "mouseout" && !sec ? null : (name || null);
    const band = (next && e.type !== "mouseout" && sec)
      ? chanBandAt(e.target, sec, apByName[next]) : null;
    if (next === hoveredAp && band === hoverBand) return;
    hoveredAp = next;
    hoverBand = band;
    if (hoveredAp) hideTip();
  }

  // --- hover panel ------------------------------------------------------
  const rows = (pairs) => pairs.filter(([, v]) => v != null && v !== "")
    .map(([k, v]) => `<div class="r"><span>${esc(k)}</span><span>${esc(v)}</span></div>`).join("");
  const avg = (ns) => ns.length ? Math.round(ns.reduce((a, b) => a + b, 0) / ns.length) : null;

  function chipTip(apName, band) {
    const all = apByName[apName]?.clients || [];
    const cs = all.filter((c) => bandOf(c) === band);
    const sig = avg(cs.map((c) => c.signal).filter((s) => s != null));
    const guests = cs.filter((c) => c.guest).length;
    return `<div class="t">${band === "?" ? "Unknown band" : band + " GHz"}</div>` + rows([
      ["Clients", cs.length],
      ["Avg signal", sig != null ? sig + " dBm" : null],
      ["Guests", guests || null],
      ["", filters.get(apName) === band ? "click to clear filter" : "click to filter"],
    ]);
  }

  function chanTip(apName, band) {
    const r = (apByName[apName]?.radios || []).find((x) => x.band === band);
    if (!r) return "";
    const pct = (v) => v == null ? null : (Math.round(v * 10) / 10) + "%";
    return `<div class="t">Ch. ${r.channel} (${band} GHz${r.width ? ", " + r.width + " MHz" : ""})</div>` +
      rows([["Utilization", pct(r.util)], ["TX Retries", pct(r.retries)]]);
  }

  function clientTip(apName, mac) {
    const c = findClient(apName, mac);
    if (!c) return "";
    const band = bandOf(c);
    return `<div class="t">${esc(labelFor(c))}</div>` + rows([
      ["SSID", c.essid],
      ["Band", band === "?" ? null : `${band} GHz${c.channel ? ` · ch ${c.channel}` : ""}`],
      ["Signal", c.signal != null ? c.signal + " dBm" : null],
      ["TX / RX", (kbps(c.tx) && kbps(c.rx)) ? `${kbps(c.tx)} / ${kbps(c.rx)}` : null],
      ["IP", c.ip],
      ["", "click for details"],
    ]);
  }

  function showTip(html, anchorEl) {
    if (!tip || !html) return;
    tip.innerHTML = html;
    const r = anchorEl.getBoundingClientRect();
    const w = tip.offsetWidth, h = tip.offsetHeight;
    let x = r.left + r.width / 2 - w / 2;
    let y = r.top - h - 10;                       // above the chip, like UniFi's
    if (y < 8) y = r.bottom + 10;                 // flip below if it would clip
    x = Math.max(8, Math.min(x, innerWidth - w - 8));
    tip.style.left = x + "px";
    tip.style.top = y + "px";
    tip.style.opacity = "1";
  }
  function hideTip() { if (tip) tip.style.opacity = "0"; }

  /* Ring geometry. The AP's own name/model labels (and, on a local console, its
   * per-radio count badges) sit directly BELOW the marker, so we leave a clear
   * wedge at the bottom and keep the ring well clear of the AP icon.
   *
   * Radius grows with the client count, but only to RING_MAX: an AP with 50+
   * clients would otherwise throw a ring right across the floor plan and bury
   * the room labels. Past the cap the stack packs tighter instead of reaching
   * further: the rings share the load in proportion to their arc length and the
   * chips shrink to suit (the .dense class), so every client is still drawn. */
  const RING_R = 100;        // innermost ring radius (px) from the AP marker
  const RING_STEP = 36;      // nominal gap between rings
  const RING_MAX = 175;      // hard cap on how far a ring may reach
  const MAX_RINGS = 6;
  const BOTTOM_GAP = 70 * Math.PI / 180;  // half-width of the clear wedge below

  function ringRadii() {
    const out = [];
    for (let r = RING_R; r <= RING_MAX && out.length < MAX_RINGS; r += RING_STEP) out.push(r);
    return out;
  }
  const ringCap = (radii, sep, span) =>
    radii.reduce((t, r) => t + Math.max(3, Math.floor((r * span) / sep)), 0);

  function ringPositions(n) {
    const span = 2 * Math.PI - 2 * BOTTOM_GAP;   // arc that avoids the labels
    const radii = ringRadii();
    const roomy = ringCap(radii, MIN_SEP, span) >= n;
    // roomy: fill ring by ring at the preferred spacing, inner rings first.
    // packed: hand every ring a share proportional to its arc, so the crowding
    // is spread evenly instead of dumped on the outermost ring.
    let counts;
    if (roomy) {
      counts = []; let left = n;
      for (const r of radii) {
        const c = Math.min(Math.max(3, Math.floor((r * span) / MIN_SEP)), left);
        counts.push(c); left -= c;
      }
    } else {
      const total = radii.reduce((a, b) => a + b, 0);
      counts = radii.map((r) => Math.floor((n * r) / total));
      let left = n - counts.reduce((a, b) => a + b, 0);   // at most one per ring
      for (let i = counts.length - 1; left > 0; i = (i + counts.length - 1) % counts.length) {
        counts[i]++; left--;
      }
    }
    const pts = [];
    let sep = Infinity;
    radii.forEach((rad, i) => {
      const cnt = counts[i];
      if (!cnt) return;
      const cap = roomy ? Math.max(3, Math.floor((rad * span) / MIN_SEP)) : cnt;
      const step = span / Math.max(1, cap - 1);
      const lead = roomy ? (span - step * (cnt - 1)) / 2 : 0;   // centre a partial ring
      if (cnt > 1) sep = Math.min(sep, step * rad);
      for (let j = 0; j < cnt; j++) {
        const ang = (Math.PI / 2 + BOTTOM_GAP) + lead + j * step;
        pts.push([Math.cos(ang) * rad, Math.sin(ang) * rad]);
      }
    });
    return { pts, sep: Number.isFinite(sep) ? sep : MIN_SEP };
  }

  function groupFor(name) {
    let g = groups.get(name);
    if (!g) {
      const el = document.createElement("div");
      el.className = "grp";
      el.addEventListener("click", (ev) => {
        const b = ev.target.closest(".chip.cl");
        if (b) {
          const band = b.dataset.band;
          filters.set(name, filters.get(name) === band ? null : band);
          g.sig = ""; // force re-render
          return;
        }
        const c = ev.target.closest(".cli");
        if (c) openCard(name, c.dataset.mac, c);
      });
      overlay.appendChild(el);
      g = { el, sig: "", below: 84, nativeCh: false, measuredAt: 0 };
      groups.set(name, g);
    }
    return g;
  }

  /* The numeric chips UniFi draws under an AP on a local console are the
   * per-radio CHANNELS (hovering shows "Ch. N (band, width) / Utilization / TX
   * Retries") — not client counts. Remote access shows the model there instead,
   * so we render equivalent channel chips ourselves only when UniFi doesn't. */
  function nativeChipsBottom(sec) {
    const title = sec.querySelector('[data-testid="title"]');
    const model = sec.querySelector('[data-testid="model"]');
    let bottom = null;
    for (const el of sec.querySelectorAll("span, div")) {
      if (el.children.length) continue;                 // leaves only
      if (title?.contains(el) || model?.contains(el)) continue;
      if (!/^\d{1,4}$/.test((el.textContent || "").trim())) continue;
      const r = el.getBoundingClientRect();
      if (r.height) bottom = Math.max(bottom ?? 0, r.bottom);
    }
    return bottom;    // null when the console draws no channel chips
  }

  function renderGroup(g, apName, ap, nativeCh, below) {
    const clients = ap.clients, counts = bandCounts(clients);
    const filter = filters.get(apName) || null;
    const list = filter ? clients.filter((c) => bandOf(c) === filter) : clients;
    const shown = list;                     // every client gets an icon
    const chans = nativeCh ? [] : (ap.radios || []);
    const sig = [filter, nativeCh ? "n" : "", below, BANDS.map((b) => counts[b]).join("/"),
      chans.map((r) => r.band + r.channel).join(","),
      shown.map((c) => c.mac + bandOf(c) + (selected?.mac === c.mac ? "*" : "")).join(",")].join("|");
    if (g.sig === sig) return;
    g.sig = sig;

    // client counts — same pill as UniFi's channel chips, with a band-coloured
    // underline, sitting ABOVE the AP so they never cover the marker art
    const badges = BANDS.filter((b) => counts[b]).map((b) =>
      `<span class="chip cl${filter === b ? " on" : ""}${filter && filter !== b ? " dim" : ""}"
         data-band="${b}">${counts[b]}<i class="bar ${BAND_CLS[b]}"></i></span>`).join("");
    // channel chips, reproducing UniFi's when the console doesn't draw them
    const chanChips = chans.map((r) =>
      `<span class="chip ch" data-band="${r.band}">${r.channel}<i class="bar ${BAND_CLS[r.band]}"></i></span>`
    ).join("");

    const { pts, sep } = ringPositions(shown.length);
    // packed tighter than the chips' own size likes? shrink them to suit
    g.el.classList.toggle("dense", sep < DENSE_SEP);
    const icons = shown.map((c, i) => {
      const [dx, dy] = pts[i], band = bandOf(c), glyph = iconFor(c);
      const sel = selected && selected.ap === apName && selected.mac === c.mac ? " sel" : "";
      // prefer UniFi's own fingerprint icon; fall back to our glyph / initial
      const fb = glyph ? `<span class="g">${glyph}</span>` : esc(initialFor(c));
      const url = uiIconFor(c);
      // no crossorigin: we only display the image, and asking for CORS would
      // make it fail outright if the CDN doesn't send the header
      const inner = url ? `<img class="ci" src="${esc(url)}" alt="">` : fb;
      // the client's name is one hover away, so the ring stays icons only —
      // labels collided at close spacing and competed with the room names
      return `<span class="cli ${BAND_CLS[band]}${c.guest ? " guest" : ""}${sel}"
          data-mac="${c.mac}" data-fb="${esc(fb)}" style="left:${dx}px;top:${dy}px"
        >${inner}</span>`;
    }).join("");

    // both chip rows sit BELOW the AP name/model, mirroring UniFi's own chips:
    // our channel chips (remote only) first, then the client counts under them
    g.el.innerHTML =
      (chanChips ? `<span class="badges ch" style="top:${below}px">${chanChips}</span>` : "") +
      // the gap between the two rows scales too, so they stay adjacent at any zoom
      `<span class="badges cl" style="top:calc(${below}px + ${chanChips ? 26 : 0}px * var(--s,1))"
        >${badges}</span>` +
      icons;
    // inline handlers are blocked by the page CSP, so bind the fallback here:
    // a fingerprint icon that 404s reverts the chip to its glyph/initial.
    g.el.querySelectorAll("img.ci").forEach((img) => {
      img.addEventListener("error", () => {
        const chip = img.parentElement;
        if (chip) chip.innerHTML = chip.dataset.fb || "?";
      }, { once: true });
    });
  }

  // --- details card -----------------------------------------------------
  function findClient(ap, mac) {
    return (apByName[ap]?.clients || []).find((c) => c.mac === mac) || null;
  }
  function openCard(ap, mac, anchorEl) {
    selected = { ap, mac };
    if (!card) {
      card = document.createElement("div");
      card.id = NS + "-card";
      document.body.appendChild(card);
    }
    renderCard();
    positionCard(anchorEl);
    for (const [n, g] of groups) if (n === ap) g.sig = "";
  }
  function closeCard() {
    selected = null;
    card?.remove();
    card = null;
    for (const [, g] of groups) g.sig = "";
  }
  function refreshOpenCard() {
    if (!selected) return;
    if (!findClient(selected.ap, selected.mac)) { closeCard(); return; }
    renderCard();
  }
  function row(k, v) { return v == null || v === "" ? "" : `<dt>${k}</dt><dd>${esc(v)}</dd>`; }
  function renderCard() {
    if (!card || !selected) return;
    const c = findClient(selected.ap, selected.mac);
    if (!c) return;
    const band = bandOf(c), glyph = iconFor(c) || initialFor(c), url = uiIconFor(c);
    const snr = (c.signal != null && c.noise != null) ? (c.signal - c.noise) + " dB" : null;
    card.innerHTML = `
      <div class="hd">
        ${url ? `<img class="ci" src="${esc(url)}" alt=""
                  style="width:18px;height:18px;object-fit:contain">`
              : `<span style="font-size:14px">${esc(glyph)}</span>`}
        <span class="t">${esc(labelFor(c))}</span>
        <span class="pill ${BAND_CLS[band]}" style="background:${
          band === "2.4" ? "#e0a83c;color:#1a1206" : band === "5" ? "#2b6cff;color:#fff"
          : band === "6" ? "#a855f7;color:#fff" : "#8b93a7;color:#0b0e14"}">${band === "?" ? "?" : band + "G"}</span>
        <span class="x" title="Close">✕</span>
      </div>
      <dl>
        ${row("AP", selected.ap)}
        ${row("SSID", c.essid)}
        ${row("IP", c.ip)}
        ${row("MAC", c.mac.replace(/(..)(?=.)/g, "$1:"))}
        ${row("Signal", c.signal != null ? c.signal + " dBm" : null)}
        ${row("SNR", snr)}
        ${row("Channel", c.channel)}
        ${row("TX / RX", (kbps(c.tx) && kbps(c.rx)) ? `${kbps(c.tx)} / ${kbps(c.rx)}` : null)}
        ${row("Data", (bytes(c.txb) && bytes(c.rxb)) ? `${bytes(c.txb)} ↑ ${bytes(c.rxb)} ↓` : null)}
        ${row("Uptime", dur(c.uptime))}
        ${row("Vendor", c.vendor || c.oui)}
        ${c.guest ? "<dt>Network</dt><dd>Guest</dd>" : ""}
      </dl>`;
    card.querySelector(".x").addEventListener("click", closeCard);
    // same fallback as the ring chips: an id with no artwork reverts to a glyph
    card.querySelector("img.ci")?.addEventListener("error", (e) => {
      e.target.outerHTML = `<span style="font-size:14px">${esc(glyph)}</span>`;
    }, { once: true });
  }
  function positionCard(anchorEl) {
    if (!card) return;
    const r = anchorEl?.getBoundingClientRect();
    const w = 236, h = card.offsetHeight || 240;
    let x = (r ? r.right + 10 : 60), y = (r ? r.top - 10 : 60);
    if (x + w > innerWidth - 8) x = (r ? r.left - w - 10 : 8);
    if (y + h > innerHeight - 8) y = Math.max(8, innerHeight - h - 8);
    card.style.left = Math.max(8, x) + "px";
    card.style.top = Math.max(8, y) + "px";
  }

  // --- position loop ----------------------------------------------------
  let raf = 0, renderErr = null, mismatchSince = 0;
  function tickPositions() {
    raf = 0;
    // Never let a render error kill the loop permanently: report it in the
    // status chip and keep scheduling, so the overlay recovers on its own.
    try { drawFrame(); } catch (e) {
      if (renderErr !== e.message) { renderErr = e.message; updateStatus(); }
    }
    schedule();
  }

  function drawFrame() {
    const canvas = document.querySelector('[data-testid="editor-canvas"]');
    if (!overlay || !canvas) return;
    const clip = canvas.getBoundingClientRect();
    const seen = new Set(), domNames = new Set();
    document.querySelectorAll('section[data-testid^="stats-tooltip-"]').forEach((sec) => {
      const title = sec.querySelector('[data-testid="title"]');
      if (!title) return;
      const name = norm(title.textContent);
      domNames.add(name);
      const ap = apByName[name];
      if (!ap || !ap.clients.length) return;
      const r = sec.getBoundingClientRect();
      const x = r.left + r.width / 2, y = r.top - 22;
      if (x < clip.left || x > clip.right || y < clip.top || y > clip.bottom) return;
      seen.add(name);
      const g = groupFor(name);
      g.el.style.left = x + "px";
      g.el.style.top = y + "px";
      g.el.style.display = "block";
      // InnerSpace scales its markers with the map zoom; track that so our chip
      // rows stay the size of its channel chips instead of drifting apart
      const s = sec.offsetWidth ? r.width / sec.offsetWidth : 1;
      g.el.style.setProperty("--s", Math.min(3, Math.max(0.5, s)).toFixed(3));
      const hovered = hoveredAp === name;
      g.el.classList.toggle("fade", hovered && !hoverBand);
      if (hovered && hoverBand) g.el.dataset.hi = hoverBand;
      else if (g.el.dataset.hi) delete g.el.dataset.hi;
      // Measure where UniFi's own content actually ends — its channel chips can
      // sit outside the section's own box — and drop our rows below that. This
      // walks the marker's DOM, so cache it rather than doing it every frame.
      const now = performance.now();
      if (!g.measuredAt || now - g.measuredAt > 500) {
        g.measuredAt = now;
        const chipsBottom = nativeChipsBottom(sec);
        g.nativeCh = chipsBottom != null;
        g.below = Math.round(Math.max(chipsBottom ?? 0, r.bottom) - y + 12);
      }
      renderGroup(g, name, ap, g.nativeCh, g.below);
      if (selected && selected.ap === name) {
        const el = g.el.querySelector(`.cli[data-mac="${selected.mac}"]`);
        if (el) positionCard(el);
      }
    });
    for (const [name, g] of groups) if (!seen.has(name)) g.el.style.display = "none";

    /* Self-heal a console switch: if none of the APs on screen appear in our
     * dataset, we're holding another console's data (its API prefix differs).
     * Drop it and re-resolve rather than showing stale numbers indefinitely. */
    const known = Object.keys(apByName);
    if (domNames.size && known.length && !known.some((n) => domNames.has(n))) {
      if (!mismatchSince) mismatchSince = performance.now();
      else if (performance.now() - mismatchSince > 2000) {
        mismatchSince = 0;
        resetConsoleState();
        refreshData();
      }
    } else mismatchSince = 0;

    renderErr = null;
  }
  function schedule() { if (!raf) raf = requestAnimationFrame(tickPositions); }

  function updateStatus() {
    if (!statusEl) return;
    if (relayOnly) {
      statusEl.innerHTML =
        `UniFi Live: this remote session is <b>relayed (WebRTC)</b> — the console's ` +
        `API isn't reachable over HTTP here. Open the console on its <b>LAN address</b> ` +
        `to use the overlay. <span style="opacity:.5">${BUILD}</span>`;
      return;
    }
    if (lastErr) {
      const saw = seenBases[0] ? seenBases[0].replace(location.origin, "") : "nothing";
      statusEl.innerHTML = `UniFi Live: <b>API error</b> — ${esc(lastErr)} · saw ` +
        `<b>${esc(saw)}</b> (${seenBases.length}) · site <b>${esc(siteId())}</b>` +
        ` <span style="opacity:.5">${BUILD}</span>`;
      return;
    }
    if (renderErr) {
      statusEl.innerHTML = `UniFi Live: <b>render error</b> — ${esc(renderErr)}`;
      return;
    }
    const aps = Object.values(apByName).filter((a) => a.clients.length);
    const all = aps.flatMap((a) => a.clients);
    const n = bandCounts(all);
    const per = BANDS.filter((b) => n[b])
      .map((b) => `<i class="${BAND_CLS[b]}"></i>${b === "?" ? "?" : b}G <b>${n[b]}</b>`).join(" · ");
    // how many clients UniFi has actually fingerprinted (the rest show a glyph)
    const withIcon = all.filter((c) => c.devId != null).length;
    const icons = all.length ? ` · icons <b>${withIcon || "none"}</b>` : "";
    statusEl.innerHTML = `UniFi Live: <b>${all.length}</b> client${all.length === 1 ? "" : "s"} on ` +
      `<b>${aps.length}</b> AP${aps.length === 1 ? "" : "s"}${per ? " — " + per : ""}${icons}` +
      ` <span style="opacity:.5">${BUILD}</span>`;
  }

  // --- lifecycle --------------------------------------------------------
  let mounted = false, dataTimer = 0;
  const onInnerspace = () => location.pathname.includes("/innerspace") &&
    document.querySelector('[data-testid="editor-canvas"]');
  function mount() {
    if (mounted) return;
    mounted = true;
    ensureOverlay();
    refreshData();
    dataTimer = setInterval(refreshData, REFRESH_MS);
    schedule();
  }
  function unmount() {
    if (!mounted) return;
    mounted = false;
    clearInterval(dataTimer);
    document.removeEventListener("mouseover", onDocHover, true);
    document.removeEventListener("mouseout", onDocHover, true);
    hoveredAp = null;
    if (raf) { cancelAnimationFrame(raf); raf = 0; }
    closeCard();
    resetConsoleState();
    overlay?.remove(); statusEl?.remove(); tip?.remove();
    overlay = statusEl = tip = null;
    groups.clear(); filters.clear();
  }
  let lastHref = location.href;
  function check() {
    if (location.href !== lastHref) {
      const before = lastHref.split("/network/")[0];
      lastHref = location.href;
      // moved to a different console/site: forget its data at once so the
      // overlay can never paint the previous console's clients
      if (before !== location.href.split("/network/")[0]) {
        resetConsoleState();
        if (mounted) refreshData();
      }
    }
    return onInnerspace() ? mount() : unmount();
  }
  new MutationObserver(check).observe(document.documentElement, { childList: true, subtree: true });
  setInterval(check, 1500);
  check();
})();
