/* UniFi Live for InnerSpace — content script (same-origin, read-only).
 *
 * Overlays live UniFi clients onto the InnerSpace floor plan. InnerSpace draws
 * the map on a WebGL <canvas>, but it also renders each AP's label as a DOM
 * <section data-testid="stats-tooltip-*"> whose CSS transform it keeps in sync
 * with the canvas as you pan/zoom. We read those live screen positions straight
 * from the DOM (no coordinate math, auto-tracks pan/zoom) and pin client bubbles
 * to them. Client counts come from the console's own Network API (same origin,
 * logged-in session, GETs only) joined to APs by name.
 */
(() => {
  if (window.__unifiLiveInnerspace) return;
  window.__unifiLiveInnerspace = true;

  const REFRESH_MS = 5000;
  const MAX_DOTS = 12;
  const norm = (s) => String(s || "").trim().toLowerCase();
  const macNorm = (m) => String(m || "").replace(/[^0-9a-fA-F]/g, "").toUpperCase();

  // --- API base + site (local: /proxy/network/api ; cloud: proxied under the
  // console id, exact scheme varies). We DISCOVER the base from the app's own
  // network activity (it already polls stat/device), with URL candidates as a
  // fallback, then cache the winner.
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
    const prefix = i < 0 ? "" : p.slice(0, i); // "" local, "/consoles/<id>" cloud
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

  // name(lower) -> { online, clients:[{mac,hostname,band,signal,guest}] }
  let apByName = {};
  let lastErr = null;
  let apiBase = null;

  async function resolveBase(site) {
    if (apiBase) return apiBase;
    const perf = baseFromPerf();
    const tries = perf ? [perf, ...candidateBases()] : candidateBases();
    let lastE = "no reachable API";
    for (const b of [...new Set(tries)]) {
      try {
        const j = await getJson(`${b}/s/${site}/stat/device`);
        if (Array.isArray(j.data)) { apiBase = b; return b; }
      } catch (e) { lastE = e.message; }
    }
    throw new Error(lastE);
  }

  async function refreshData() {
    const site = siteId();
    try {
      const base = await resolveBase(site);
      const [dev, sta] = await Promise.all([
        getJson(`${base}/s/${site}/stat/device`),
        getJson(`${base}/s/${site}/stat/sta`),
      ]);
      const byMac = {}; // AP mac -> {name, online}
      for (const d of dev.data || []) {
        if (d.type && d.type !== "uap") continue; // APs only
        byMac[macNorm(d.mac)] = { name: d.name || d.mac, online: d.state === 1 };
      }
      const cliByMac = {};
      for (const c of sta.data || []) {
        if (c.is_wired) continue;
        const ap = macNorm(c.ap_mac);
        if (!ap) continue;
        (cliByMac[ap] ||= []).push({
          mac: macNorm(c.mac),
          hostname: c.hostname || c.name || null,
          band: c.radio === "na" ? "5" : c.radio === "ng" ? "2.4" : c.radio === "6e" ? "6" : null,
          signal: c.signal ?? null,
          guest: !!c.is_guest,
        });
      }
      const next = {};
      for (const [mac, ap] of Object.entries(byMac)) {
        next[norm(ap.name)] = { online: ap.online, clients: cliByMac[mac] || [] };
      }
      apByName = next;
      lastErr = null;
    } catch (e) {
      lastErr = e.message;
      apiBase = null; // re-discover next tick (route/proxy may have changed)
    }
    updateStatus();
  }

  // --- overlay ----------------------------------------------------------
  const NS = "unifi-live";
  let overlay, statusEl;
  const groups = new Map(); // apNameLower -> {el, sig}

  function ensureOverlay() {
    if (overlay && document.body.contains(overlay)) return;
    // Drop any nodes left behind by a previous (now-dead) extension context —
    // reloading an unpacked extension orphans its DOM but not its elements.
    document.querySelectorAll(`#${NS}-overlay, #${NS}-status`).forEach((el) => el.remove());
    overlay = document.createElement("div");
    overlay.id = NS + "-overlay";
    Object.assign(overlay.style, {
      position: "fixed", inset: "0", pointerEvents: "none", zIndex: "2147483000",
    });
    const style = document.createElement("style");
    style.textContent = `
      #${NS}-overlay .grp { position: fixed; transform: translate(-50%, -50%);
        will-change: left, top; }
      #${NS}-overlay .badge { position: absolute; left: 12px; top: -14px;
        min-width: 16px; height: 16px; padding: 0 4px; border-radius: 9px;
        background: #35c46b; color: #04140a; font: 700 11px/16px system-ui, sans-serif;
        text-align: center; box-shadow: 0 1px 3px rgba(0,0,0,.4); }
      #${NS}-overlay .dot { position: absolute; width: 9px; height: 9px;
        margin: -4.5px; border-radius: 50%; background: #2b6cff;
        border: 1.5px solid #fff; box-shadow: 0 1px 2px rgba(0,0,0,.4);
        transition: left .7s ease, top .7s ease; }
      #${NS}-overlay .dot.guest { background: #e0a83c; }
      #${NS}-status { position: fixed; left: 14px; bottom: 14px; z-index: 2147483001;
        background: #131722ee; color: #e6e9ef; font: 12px/1.4 system-ui, sans-serif;
        padding: 7px 11px; border-radius: 8px; border: 1px solid #2a3346;
        pointer-events: none; box-shadow: 0 6px 20px rgba(0,0,0,.4); }
      #${NS}-status b { color: #4c9aff; }`;
    overlay.appendChild(style);
    document.body.appendChild(overlay);
    statusEl = document.createElement("div");
    statusEl.id = NS + "-status";
    statusEl.textContent = "UniFi Live: starting…";
    document.body.appendChild(statusEl);
  }

  function ringPositions(n) {
    const out = [], per = 8;
    for (let i = 0; i < n; i++) {
      const ring = Math.floor(i / per), inRing = i - ring * per;
      const cnt = Math.min(per, n - ring * per);
      const ang = (inRing / cnt) * 2 * Math.PI - Math.PI / 2;
      const rad = 20 + ring * 14;
      out.push([Math.cos(ang) * rad, Math.sin(ang) * rad]);
    }
    return out;
  }

  function groupFor(name) {
    let g = groups.get(name);
    if (!g) {
      const el = document.createElement("div");
      el.className = "grp";
      overlay.appendChild(el);
      g = { el, sig: "" };
      groups.set(name, g);
    }
    return g;
  }

  function renderDots(g, clients) {
    const shown = clients.slice(0, MAX_DOTS);
    const extra = clients.length - shown.length;
    const sig = clients.length + ":" + shown.map((c) => c.mac + c.guest).join(",");
    if (g.sig === sig) return;
    g.sig = sig;
    const pos = ringPositions(shown.length);
    g.el.innerHTML =
      `<span class="badge">${clients.length}${extra > 0 ? "" : ""}</span>` +
      shown.map((c, i) => {
        const [dx, dy] = pos[i];
        const t = `${c.hostname || c.mac}${c.band ? " · " + c.band + "G" : ""}${c.signal != null ? " · " + c.signal + " dBm" : ""}${c.guest ? " · guest" : ""}`;
        return `<span class="dot${c.guest ? " guest" : ""}" style="left:${dx}px;top:${dy}px" title="${t.replace(/"/g, "&quot;")}"></span>`;
      }).join("");
  }

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
      if (!ap || !ap.clients.length) return; // only APs with clients
      const r = sec.getBoundingClientRect();
      // The label sits *below* the AP icon; anchor above it so the bubble ring
      // circles the icon instead of covering the name/model text.
      const x = r.left + r.width / 2, y = r.top - 22;
      if (x < clip.left || x > clip.right || y < clip.top || y > clip.bottom) return;
      seen.add(name);
      const g = groupFor(name);
      g.el.style.left = x + "px";
      g.el.style.top = y + "px";
      g.el.style.display = "block";
      renderDots(g, ap.clients);
    });
    for (const [name, g] of groups) if (!seen.has(name)) g.el.style.display = "none";
    schedule();
  }

  function schedule() {
    if (!raf) raf = requestAnimationFrame(tickPositions);
  }

  function updateStatus() {
    if (!statusEl) return;
    if (lastErr) {
      const tried = (baseFromPerf() ? "perf, " : "") + candidateBases().length + " path(s)";
      statusEl.innerHTML = `UniFi Live: <b>API error</b> — ${lastErr} (tried ${tried})`;
      return;
    }
    const aps = Object.values(apByName).filter((a) => a.clients.length);
    const total = aps.reduce((n, a) => n + a.clients.length, 0);
    statusEl.innerHTML = `UniFi Live: <b>${total}</b> client${total === 1 ? "" : "s"} on <b>${aps.length}</b> AP${aps.length === 1 ? "" : "s"}`;
  }

  // --- lifecycle: mount on InnerSpace, react to SPA route changes --------
  let mounted = false, dataTimer = 0;
  function onInnerspace() {
    return location.pathname.includes("/innerspace") &&
      document.querySelector('[data-testid="editor-canvas"]');
  }
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
    overlay?.remove(); statusEl?.remove();
    overlay = statusEl = null; groups.clear();
  }
  function check() {
    if (onInnerspace()) mount();
    else unmount();
  }
  new MutationObserver(check).observe(document.documentElement, { childList: true, subtree: true });
  setInterval(check, 1500);
  check();
})();
