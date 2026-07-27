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
 * Each client is a small icon chip (coloured by radio band) with its name;
 * clicking one opens a details card. Band chips above the AP filter the ring.
 */
(() => {
  if (window.__unifiLiveInnerspace) return;
  window.__unifiLiveInnerspace = true;

  const REFRESH_MS = 5000;
  const MAX_ICONS = 14;      // icons drawn per AP before overflow chip
  const NAME_LIMIT = 12;     // show name labels only when the ring is this small
  const NS = "unifi-live";

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
  function baseFromPerf() {
    try {
      const rx = /^(https?:\/\/[^/]+(?:\/[^?#]*?)?\/proxy\/network\/api)\/s\//;
      const ents = performance.getEntriesByType("resource");
      for (let k = ents.length - 1; k >= 0; k--) {
        const m = ents[k].name.match(rx);
        if (m) return m[1];
      }
    } catch (_e) { /* ignore */ }
    return null;
  }
  function candidateBases() {
    const p = location.pathname, i = p.indexOf("/network/");
    const prefix = i < 0 ? "" : p.slice(0, i);
    const o = location.origin, list = [`${o}${prefix}/proxy/network/api`];
    const m = prefix.match(/\/consoles\/([^/]+)/);
    if (m) {
      list.push(`${o}/proxy/consoles/${m[1]}/proxy/network/api`);
      list.push(`${o}/proxy/network/${m[1]}/proxy/network/api`);
    }
    list.push(`${o}/proxy/network/api`);
    return [...new Set(list)];
  }
  async function getJson(url) {
    const r = await fetch(url, { credentials: "include", headers: { Accept: "application/json" } });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const ct = r.headers.get("content-type") || "";
    const text = await r.text();
    if (!ct.includes("json")) throw new Error("non-JSON (wrong API path)");
    return JSON.parse(text);
  }

  let apByName = {}, lastErr = null, apiBase = null;

  async function resolveBase(site) {
    if (apiBase) return apiBase;
    const perf = baseFromPerf();
    const tries = [...new Set(perf ? [perf, ...candidateBases()] : candidateBases())];
    let lastE = "no reachable API";
    for (const b of tries) {
      try {
        const j = await getJson(`${b}/s/${site}/stat/device`);
        if (Array.isArray(j.data)) { apiBase = b; return b; }
      } catch (e) { lastE = e.message; }
    }
    throw new Error(lastE);
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

  async function refreshData() {
    const site = siteId();
    try {
      const base = await resolveBase(site);
      const [dev, sta] = await Promise.all([
        getJson(`${base}/s/${site}/stat/device`),
        getJson(`${base}/s/${site}/stat/sta`),
      ]);
      const byMac = {};
      for (const d of dev.data || []) {
        if (d.type && d.type !== "uap") continue;
        byMac[macNorm(d.mac)] = { name: d.name || d.mac, online: d.state === 1 };
      }
      const cliByMac = {};
      for (const c of sta.data || []) {
        if (c.is_wired) continue;
        const ap = macNorm(c.ap_mac);
        if (!ap) continue;
        (cliByMac[ap] ||= []).push({
          mac: macNorm(c.mac),
          name: c.name || c.hostname || null,
          hostname: c.hostname || null,
          ip: c.ip || null,
          band: c.radio === "na" ? "5" : c.radio === "ng" ? "2.4" : c.radio === "6e" ? "6" : null,
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
        next[norm(ap.name)] = { online: ap.online, clients: list };
      }
      apByName = next;
      lastErr = null;
    } catch (e) {
      lastErr = e.message;
      apiBase = null;
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
  let overlay, statusEl, card;
  const groups = new Map();          // apName -> {el, sig}
  const filters = new Map();         // apName -> band | null
  let selected = null;               // {ap, mac}

  function ensureOverlay() {
    if (overlay && document.body.contains(overlay)) return;
    document.querySelectorAll(`#${NS}-overlay, #${NS}-status, #${NS}-card`).forEach((el) => el.remove());
    overlay = document.createElement("div");
    overlay.id = NS + "-overlay";
    Object.assign(overlay.style, { position: "fixed", inset: "0", pointerEvents: "none", zIndex: "2147483000" });
    const style = document.createElement("style");
    style.textContent = `
      #${NS}-overlay .grp { position: fixed; transform: translate(-50%, -50%); will-change: left, top; }
      #${NS}-overlay .badges { position: absolute; left: 50%; top: -34px;
        transform: translateX(-50%); display: flex; gap: 3px; white-space: nowrap;
        pointer-events: auto; }
      #${NS}-overlay .badge { min-width: 15px; height: 16px; padding: 0 5px;
        border-radius: 8px; font: 700 10px/16px system-ui, sans-serif; text-align: center;
        box-shadow: 0 1px 3px rgba(0,0,0,.45); cursor: pointer; opacity: .95; }
      #${NS}-overlay .badge.dim { opacity: .35; }
      #${NS}-overlay .badge.tiny { min-width: 0; width: 9px; height: 9px; padding: 0;
        border-radius: 50%; opacity: .8; }
      #${NS}-overlay .badge.tiny:hover { opacity: 1; transform: scale(1.3); }
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
      #${NS}-overlay .nm { position: absolute; transform: translate(-50%, 0);
        margin-top: 12px; max-width: 82px; overflow: hidden; text-overflow: ellipsis;
        white-space: nowrap; text-align: center; font: 600 10px/1.3 system-ui, sans-serif;
        color: #fff; text-shadow: 0 1px 2px #000, 0 0 3px #000; pointer-events: none; }
      #${NS}-overlay .more { position: absolute; width: 22px; height: 22px; margin: -11px;
        border-radius: 50%; background: #131722; border: 2px dashed #55607a; color: #cfd6e4;
        font: 700 9px/20px system-ui, sans-serif; text-align: center;
        pointer-events: auto; cursor: default; }
      #${NS}-overlay .badge.b24, #${NS}-overlay .badge.b5,
      #${NS}-overlay .badge.b6,  #${NS}-overlay .badge.bx { color: #fff; }
      #${NS}-overlay .badge.b24 { background: #e0a83c; color: #1a1206; }
      #${NS}-overlay .badge.b5  { background: #2b6cff; }
      #${NS}-overlay .badge.b6  { background: #a855f7; }
      #${NS}-overlay .badge.bx  { background: #8b93a7; color: #0b0e14; }
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

    document.addEventListener("mousedown", (e) => {
      if (card && !card.contains(e.target) && !e.target.closest?.(`#${NS}-overlay .cli`)) closeCard();
    }, true);
  }

  // Ring geometry. The AP's own name/model labels (and, on a local console, its
  // per-radio count badges) sit directly BELOW the marker, so we leave a clear
  // wedge at the bottom and keep the ring well clear of the AP icon. Radius also
  // grows with the number of icons so they never collide.
  const RING_R = 100;        // base radius (px) from the AP marker
  const RING_STEP = 36;      // extra radius per additional ring
  const BOTTOM_GAP = 62 * Math.PI / 180;  // half-width of the clear wedge below
  function ringPositions(n) {
    const out = [], per = 10;
    for (let i = 0; i < n; i++) {
      const ring = Math.floor(i / per), inRing = i - ring * per;
      const cnt = Math.min(per, n - ring * per);
      const span = 2 * Math.PI - 2 * BOTTOM_GAP;      // arc that avoids the labels
      const ang = (Math.PI / 2 + BOTTOM_GAP) +
        (cnt === 1 ? span / 2 : (inRing / (cnt - 1)) * span);
      const rad = RING_R + ring * RING_STEP + (cnt > 6 ? (cnt - 6) * 3 : 0);
      out.push([Math.cos(ang) * rad, Math.sin(ang) * rad]);
    }
    return out;
  }

  function groupFor(name) {
    let g = groups.get(name);
    if (!g) {
      const el = document.createElement("div");
      el.className = "grp";
      el.addEventListener("click", (ev) => {
        const b = ev.target.closest(".badge");
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
      g = { el, sig: "" };
      groups.set(name, g);
    }
    return g;
  }

  /* A local console renders UniFi's own per-radio client-count chips inside the
   * AP marker; remote access (unifi.ui.com) shows the model there instead. When
   * UniFi is already showing counts we suppress our band chips and defer to it.
   * Detect by looking for a numeric-only leaf node that isn't the title/model. */
  function hasNativeCounts(sec) {
    const title = sec.querySelector('[data-testid="title"]');
    const model = sec.querySelector('[data-testid="model"]');
    for (const el of sec.querySelectorAll("span, div")) {
      if (el.children.length) continue;                 // leaves only
      if (title?.contains(el) || model?.contains(el)) continue;
      if (/^\d{1,4}$/.test((el.textContent || "").trim())) return true;
    }
    return false;
  }

  function renderGroup(g, apName, clients, nativeCounts) {
    const counts = bandCounts(clients);
    const filter = filters.get(apName) || null;
    const list = filter ? clients.filter((c) => bandOf(c) === filter) : clients;
    // when there is overflow, reserve the last ring slot for the "+N more" chip
    const over = list.length > MAX_ICONS;
    const shown = list.slice(0, over ? MAX_ICONS - 1 : MAX_ICONS);
    const extra = list.length - shown.length;
    const withNames = shown.length <= NAME_LIMIT;
    const sig = [filter, nativeCounts ? "n" : "", BANDS.map((b) => counts[b]).join("/"),
      shown.map((c) => c.mac + bandOf(c) + (selected?.mac === c.mac ? "*" : "")).join(",")].join("|");
    if (g.sig === sig) return;
    g.sig = sig;

    // When UniFi renders its own count chips (local console) we don't repeat the
    // numbers — collapse to small colour dots that still act as band filters.
    const badges = BANDS.filter((b) => counts[b]).map((b) =>
      `<span class="badge ${BAND_CLS[b]}${nativeCounts ? " tiny" : ""}${filter && filter !== b ? " dim" : ""}"
         data-band="${b}" title="${b === "?" ? "unknown band" : b + " GHz"}: ${counts[b]} — click to filter"
        >${nativeCounts ? "" : counts[b]}</span>`).join("");

    const pos = ringPositions(shown.length + (extra > 0 ? 1 : 0));
    const icons = shown.map((c, i) => {
      const [dx, dy] = pos[i], band = bandOf(c), glyph = iconFor(c);
      const sel = selected && selected.ap === apName && selected.mac === c.mac ? " sel" : "";
      const tip = `${labelFor(c)}${band !== "?" ? " · " + band + " GHz" : ""}${c.signal != null ? " · " + c.signal + " dBm" : ""}`;
      const nm = withNames
        ? `<span class="nm" style="left:${dx}px;top:${dy}px">${esc(labelFor(c))}</span>` : "";
      return `<span class="cli ${BAND_CLS[band]}${c.guest ? " guest" : ""}${sel}"
          data-mac="${c.mac}" style="left:${dx}px;top:${dy}px" title="${esc(tip)}"
        >${glyph ? `<span class="g">${glyph}</span>` : esc(initialFor(c))}</span>${nm}`;
    }).join("");

    const more = extra > 0
      ? (([mx, my]) => `<span class="more" style="left:${mx}px;top:${my}px">+${extra}</span>`)(pos[pos.length - 1])
      : "";
    g.el.innerHTML = `<span class="badges">${badges}</span>${icons}${more}`;
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
    const band = bandOf(c), glyph = iconFor(c) || initialFor(c);
    const snr = (c.signal != null && c.noise != null) ? (c.signal - c.noise) + " dB" : null;
    card.innerHTML = `
      <div class="hd">
        <span style="font-size:14px">${glyph}</span>
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
  let raf = 0;
  function tickPositions() {
    raf = 0;
    const canvas = document.querySelector('[data-testid="editor-canvas"]');
    if (!overlay || !canvas) return;
    const clip = canvas.getBoundingClientRect();
    const seen = new Set();
    document.querySelectorAll('section[data-testid^="stats-tooltip-"]').forEach((sec) => {
      const title = sec.querySelector('[data-testid="title"]');
      if (!title) return;
      const name = norm(title.textContent);
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
      renderGroup(g, name, ap.clients, hasNativeCounts(sec));
      if (selected && selected.ap === name) {
        const el = g.el.querySelector(`.cli[data-mac="${selected.mac}"]`);
        if (el) positionCard(el);
      }
    });
    for (const [name, g] of groups) if (!seen.has(name)) g.el.style.display = "none";
    schedule();
  }
  function schedule() { if (!raf) raf = requestAnimationFrame(tickPositions); }

  function updateStatus() {
    if (!statusEl) return;
    if (lastErr) {
      const tried = (baseFromPerf() ? "perf, " : "") + candidateBases().length + " path(s)";
      statusEl.innerHTML = `UniFi Live: <b>API error</b> — ${esc(lastErr)} (tried ${tried})`;
      return;
    }
    const aps = Object.values(apByName).filter((a) => a.clients.length);
    const all = aps.flatMap((a) => a.clients);
    const n = bandCounts(all);
    const per = BANDS.filter((b) => n[b])
      .map((b) => `<i class="${BAND_CLS[b]}"></i>${b === "?" ? "?" : b}G <b>${n[b]}</b>`).join(" · ");
    statusEl.innerHTML = `UniFi Live: <b>${all.length}</b> client${all.length === 1 ? "" : "s"} on ` +
      `<b>${aps.length}</b> AP${aps.length === 1 ? "" : "s"}${per ? " — " + per : ""}`;
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
    if (raf) { cancelAnimationFrame(raf); raf = 0; }
    closeCard();
    overlay?.remove(); statusEl?.remove();
    overlay = statusEl = null;
    groups.clear(); filters.clear();
  }
  const check = () => onInnerspace() ? mount() : unmount();
  new MutationObserver(check).observe(document.documentElement, { childList: true, subtree: true });
  setInterval(check, 1500);
  check();
})();
