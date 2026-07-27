/* UniFi Live for InnerSpace — content script (same-origin, read-only).
 *
 * Runs on the UniFi console. Reads the InnerSpace project (floor image + AP
 * positions) and the Network API (live APs + clients), joins them by AP MAC, and
 * renders a live client map in an injected shadow-DOM card. Everything is a GET
 * against the console's own origin, using the logged-in session cookies — no
 * bridge, no CORS, no CSRF.
 *
 * SCAFFOLD: the card floats bottom-right (anchor TBD once we have the InnerSpace
 * DOM). Verified logic: API paths, MAC join, scene->pixel transform, rendering.
 * Not yet run against a live console — expect small field-name tweaks.
 */
(() => {
  if (window.__unifiLiveInnerspace) return; // guard against double injection
  window.__unifiLiveInnerspace = true;

  const IS = "/proxy/innerspace/api";
  const NET = "/proxy/network/api";
  const SVGNS = "http://www.w3.org/2000/svg";
  const REFRESH_MS = 5000;
  const state = { site: "", selPlan: null, selAp: null, aps: [], imgFor: undefined };

  const normMac = (m) => String(m || "").replace(/[^0-9a-fA-F]/g, "").toUpperCase();

  async function getJson(path) {
    const r = await fetch(path, { credentials: "include", headers: { Accept: "application/json" } });
    if (!r.ok) throw new Error(`${path} -> HTTP ${r.status}`);
    return r.json();
  }

  async function resolveSite() {
    if (state.site) return state.site;
    const stored = await chrome.storage.local.get("site");
    if (stored.site) return (state.site = stored.site);
    const sites = (await getJson(`${NET}/self/sites`)).data || [];
    return (state.site = (sites[0] && sites[0].name) || "default");
  }

  function sceneToPx(pt, map, W, H) {
    const mp = (map.position && map.position[0]) || { x: 0, y: 0 };
    const sx = (map.scale && map.scale.x) || 1;
    const sy = (map.scale && map.scale.y) || 1;
    return { x: (pt.x - mp.x) / sx + W / 2, y: H / 2 - (pt.y - mp.y) / sy };
  }

  function imageSize(url) {
    return new Promise((resolve) => {
      const im = new Image();
      im.onload = () => resolve({ w: im.naturalWidth, h: im.naturalHeight });
      im.onerror = () => resolve(null);
      im.src = url;
    });
  }

  // ---- data ------------------------------------------------------------
  async function loadModel() {
    const site = await resolveSite();
    const [proj, devWrap, staWrap] = await Promise.all([
      getJson(`${IS}/project?mode=2D`),
      getJson(`${NET}/s/${site}/stat/device`),
      getJson(`${NET}/s/${site}/stat/sta`),
    ]);
    const data = proj.data || proj;
    const shapes = data.shapes || [];
    const plans = (data.plans || []).map((p) => ({ id: p.id, name: p.title || p.id }));
    const mapByPlan = {}, devByPlan = {};
    for (const s of shapes) {
      if (s.type === "map" && s.planId) mapByPlan[s.planId] = s;
      else if (s.type === "device" && s.planId) (devByPlan[s.planId] ||= []).push(s);
    }
    // live joins
    const devLive = {};
    for (const d of devWrap.data || []) devLive[normMac(d.mac)] = d;
    const cliByAp = {};
    for (const c of staWrap.data || []) {
      if (c.is_wired) continue;
      const ap = normMac(c.ap_mac);
      if (!ap) continue;
      (cliByAp[ap] ||= []).push({
        mac: normMac(c.mac),
        hostname: c.hostname || c.name || null,
        band: c.radio === "na" ? "5" : c.radio === "ng" ? "2.4" : c.radio === "6e" ? "6" : null,
        signal_dbm: c.signal ?? null,
        is_guest: !!c.is_guest,
      });
    }
    return { plans, mapByPlan, devByPlan, devLive, cliByAp };
  }

  async function buildPlan(model, planId) {
    const map = model.mapByPlan[planId];
    const url = map && map.urlImage ? map.urlImage : null;
    const dim = url ? await imageSize(url) : null;
    const W = dim ? dim.w : 1000, H = dim ? dim.h : 600;
    const aps = (model.devByPlan[planId] || []).map((s) => {
      const mac = normMac(s.meta && s.meta.mac);
      const px = sceneToPx((s.position && s.position[0]) || { x: 0, y: 0 }, map || {}, W, H);
      const live = model.devLive[mac];
      const clients = model.cliByAp[mac] || [];
      return {
        mac, name: s.title || (live && live.name) || mac,
        online: live ? live.state === 1 : false,
        x: px.x, y: px.y, clients,
      };
    });
    return { id: planId, name: (model.plans.find((p) => p.id === planId) || {}).name, url, W, H, aps };
  }

  // ---- render ----------------------------------------------------------
  function clientPositions(ap, R) {
    const k = ap.clients.length, per = 8, out = [];
    for (let i = 0; i < k; i++) {
      const ring = Math.floor(i / per), inRing = i - ring * per;
      const cnt = Math.min(per, k - ring * per);
      const ang = (inRing / cnt) * 2 * Math.PI - Math.PI / 2;
      const rad = R * 2.1 + ring * R * 1.3;
      out.push({ ...ap.clients[i], apName: ap.name, cx: ap.x + rad * Math.cos(ang), cy: ap.y + rad * Math.sin(ang) });
    }
    return out;
  }

  function renderPlan(root, plan) {
    const svg = root.getElementById("map"), img = root.getElementById("mapImg");
    svg.setAttribute("viewBox", `0 0 ${plan.W} ${plan.H}`);
    img.setAttribute("width", plan.W); img.setAttribute("height", plan.H);
    if (state.imgFor !== plan.id) { img.setAttribute("href", plan.url || ""); state.imgFor = plan.id; }
    state.aps = plan.aps;
    const total = plan.aps.reduce((n, a) => n + a.clients.length, 0);
    root.getElementById("hint").textContent =
      `${plan.aps.length} AP${plan.aps.length === 1 ? "" : "s"} · ${total} client${total === 1 ? "" : "s"}`;
    const R = Math.min(Math.max(Math.max(plan.W, plan.H) * 0.012, 9), 26);

    root.getElementById("apLayer").innerHTML = plan.aps.map((a) => {
      const badge = a.clients.length
        ? `<circle class="badge" cx="${a.x + R}" cy="${a.y - R}" r="${R * 0.72}"></circle>
           <text class="badge-t" x="${a.x + R}" y="${a.y - R + R * 0.3}" font-size="${R * 0.9}">${a.clients.length}</text>` : "";
      return `<g class="ap" data-mac="${a.mac}">
        <circle class="core ${a.online ? "on" : "off"}${a.mac === state.selAp ? " sel" : ""}" cx="${a.x}" cy="${a.y}" r="${R}"></circle>
        <text class="lbl" x="${a.x}" y="${a.y + R * 2.1}" font-size="${R * 1.05}" text-anchor="middle">${esc(a.name)}</text>
        ${badge}</g>`;
    }).join("");
    root.querySelectorAll("#apLayer .ap").forEach((g) =>
      g.addEventListener("click", () => selectAp(root, g.dataset.mac)));

    const targets = {};
    plan.aps.forEach((a) => clientPositions(a, R).forEach((c) => { targets[c.mac] = c; }));
    const layer = root.getElementById("cliLayer"), have = {};
    layer.querySelectorAll("circle").forEach((el) => { have[el.dataset.mac] = el; });
    Object.keys(have).forEach((m) => { if (!targets[m]) have[m].remove(); });
    Object.values(targets).forEach((c) => {
      let el = have[c.mac];
      if (!el) { el = document.createElementNS(SVGNS, "circle"); el.setAttribute("class", "cli" + (c.is_guest ? " guest" : "")); el.dataset.mac = c.mac; layer.appendChild(el); }
      el.setAttribute("r", R * 0.42); el.setAttribute("cx", c.cx); el.setAttribute("cy", c.cy);
    });
    if (state.selAp) selectAp(root, state.selAp, true);
  }

  function selectAp(root, mac) {
    state.selAp = mac;
    const a = state.aps.find((x) => x.mac === mac);
    root.querySelectorAll("#apLayer .core").forEach((el) =>
      el.classList.toggle("sel", el.closest(".ap").dataset.mac === mac));
    root.getElementById("sideT").textContent = a ? a.name : "Access point";
    root.getElementById("side").innerHTML = a && a.clients.length
      ? a.clients.map((c) => `<div class="cli-row"><b>${esc(c.hostname || c.mac)}</b>
          <span>${esc(c.mac)}${c.band ? " · " + c.band + "G" : ""}${c.signal_dbm != null ? " · " + c.signal_dbm + " dBm" : ""}${c.is_guest ? " · guest" : ""}</span></div>`).join("")
      : `<div class="muted">${a ? "No clients on this AP." : "Click an AP to list its clients."}</div>`;
  }

  function esc(s) {
    return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  // ---- shell + loop ----------------------------------------------------
  async function refresh(root) {
    try {
      const model = await loadModel();
      const sel = root.getElementById("planSel");
      const opts = model.plans.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("");
      if (sel.dataset.sig !== opts) { sel.innerHTML = opts; sel.dataset.sig = opts; }
      if (!state.selPlan && model.plans[0]) state.selPlan = model.plans[0].id;
      if (state.selPlan) sel.value = state.selPlan;
      root.getElementById("conn").textContent = "live";
      if (state.selPlan) renderPlan(root, await buildPlan(model, state.selPlan));
    } catch (e) {
      root.getElementById("conn").textContent = "error: " + e.message;
    }
  }

  const CSS = `
    :host { all: initial; }
    .card { position: fixed; right: 16px; bottom: 16px; width: 420px; z-index: 2147483647;
      background: #131722; color: #e6e9ef; border: 1px solid #232a3a; border-radius: 12px;
      font: 12px/1.4 system-ui, sans-serif; box-shadow: 0 12px 40px rgba(0,0,0,.5); overflow: hidden; }
    .hd { display: flex; align-items: center; gap: 8px; padding: 9px 12px; border-bottom: 1px solid #232a3a; }
    .hd b { font-size: 13px; } .hd .sp { margin-left: auto; }
    select { background: #1b2130; color: #e6e9ef; border: 1px solid #232a3a; border-radius: 6px; padding: 3px 6px; font: inherit; }
    .conn { color: #8b93a7; } .muted { color: #8b93a7; }
    .stage { padding: 8px; } svg#map { width: 100%; height: auto; display: block; background: #0b0e14; border-radius: 6px; }
    .core.on { fill: #3b82f6; stroke: #fff; stroke-width: 2; } .core.off { fill: #e5533c; stroke: #fff; stroke-width: 2; }
    .core.sel { stroke: #35c46b; stroke-width: 3; } .ap { cursor: pointer; }
    .lbl { fill: #e6e9ef; paint-order: stroke; stroke: #131722; stroke-width: 3px; stroke-linejoin: round; font-weight: 600; }
    .badge { fill: #35c46b; } .badge-t { fill: #04140a; font-weight: 700; text-anchor: middle; }
    .cli { fill: #4c9aff; stroke: #131722; stroke-width: 1; transition: cx .8s ease, cy .8s ease; }
    .cli.guest { fill: #e0a83c; }
    .side { max-height: 130px; overflow: auto; padding: 6px 12px 10px; border-top: 1px solid #232a3a; }
    .cli-row { padding: 4px 0; border-bottom: 1px solid #1b2130; display: flex; flex-direction: column; }
    .cli-row span { color: #8b93a7; } .foot { padding: 6px 12px; color: #8b93a7; border-top: 1px solid #232a3a; }`;

  const HTML = `
    <div class="card">
      <div class="hd"><b>UniFi Live</b><span class="conn" id="conn">connecting…</span>
        <span class="sp"></span><select id="planSel"></select></div>
      <div class="stage">
        <svg id="map" viewBox="0 0 1000 600" preserveAspectRatio="xMidYMid meet">
          <image id="mapImg" x="0" y="0" width="1000" height="600" href="" />
          <g id="apLayer"></g><g id="cliLayer"></g>
        </svg>
      </div>
      <div class="side"><div id="sideT" style="font-weight:600;margin-bottom:4px">Access point</div>
        <div id="side"><div class="muted">Click an AP to list its clients.</div></div></div>
      <div class="foot"><span id="hint">—</span> · live from this console</div>
    </div>`;

  function mount() {
    const host = document.createElement("div");
    host.id = "unifi-live-innerspace-host";
    const root = host.attachShadow({ mode: "open" });
    const style = document.createElement("style"); style.textContent = CSS; root.appendChild(style);
    const wrap = document.createElement("div"); wrap.innerHTML = HTML; root.appendChild(wrap);
    document.body.appendChild(host);
    root.getElementById("planSel").addEventListener("change", (e) => {
      state.selPlan = e.target.value; state.selAp = null; refresh(root);
    });
    refresh(root);
    setInterval(() => refresh(root), REFRESH_MS);
  }

  if (document.body) mount();
  else window.addEventListener("DOMContentLoaded", mount);
})();
