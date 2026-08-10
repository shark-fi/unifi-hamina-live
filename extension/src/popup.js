/* Popup: capture the console origin, request host permission (user gesture),
 * ask the service worker to (un)register the content script, and inject
 * immediately into the current tab so it works without a reload. */
const $ = (id) => document.getElementById(id);

function normOrigin(v) {
  v = (v || "").trim();
  if (!v) return "";
  if (!/^https?:\/\//i.test(v)) v = "https://" + v;
  try {
    return new URL(v).origin;
  } catch (_e) {
    return "";
  }
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

// A LAN bridge is plain HTTP far more often than not, so default the scheme to
// http here (the console field defaults to https). Explicit schemes win.
function normBridge(v) {
  v = (v || "").trim();
  if (!v) return "";
  if (!/^https?:\/\//i.test(v)) v = "http://" + v;
  try {
    return new URL(v).origin;
  } catch (_e) {
    return "";
  }
}

async function init() {
  const stored = await chrome.storage.local.get(["origin", "site", "bridge"]);
  $("bridge").value = stored.bridge || "";
  const tab = await activeTab();
  const prefill =
    stored.origin ||
    (tab && tab.url ? (() => { try { return new URL(tab.url).origin; } catch { return ""; } })() : "");
  $("origin").value = prefill || "";
  $("site").value = stored.site || "";
  if (stored.origin) setStatus(`Enabled for ${stored.origin}`);
}

function setStatus(msg) {
  $("status").textContent = msg;
}

$("enable").addEventListener("click", async () => {
  const origin = normOrigin($("origin").value);
  if (!origin) return setStatus("Enter a valid console URL.");
  const site = $("site").value.trim();
  setStatus("Requesting permission…");
  let granted;
  try {
    // remote consoles are reached over https://<id>.id.ui.direct, a separate
    // origin, so we need permission for it as well as the console page itself
    granted = await chrome.permissions.request({
      origins: [origin + "/*", "https://*.id.ui.direct/*"] });
  } catch (e) {
    return setStatus("Permission error: " + e.message);
  }
  if (!granted) return setStatus("Permission denied.");
  const res = await chrome.runtime.sendMessage({ type: "enable", origin, site });
  if (!res?.ok) return setStatus("Register failed: " + (res?.error || "unknown"));

  // Inject now so the current tab lights up without a reload (if it's the console).
  const tab = await activeTab();
  if (tab && tab.url && tab.url.startsWith(origin)) {
    try {
      await chrome.scripting.executeScript({
        target: { tabId: tab.id }, files: ["src/probe.js"], world: "MAIN" });
      await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["src/content.js"] });
    } catch (_e) {
      /* not an injectable page; it'll load on next navigation */
    }
  }
  setStatus(`Enabled for ${origin}. Open the InnerSpace map.`);
});

/* Verify the load-bearing assumption for bridge support: that the service
 * worker can fetch a plain-HTTP LAN address, which is what makes a relayed
 * unifi.ui.com session (and hamina.com) coverable at all. Runs the fetch in the
 * worker — the same path the overlay would use — and reports which stage failed
 * rather than a bare "didn't work". */
function setBridgeStatus(msg, cls) {
  const el = $("bridgestatus");
  el.textContent = msg;
  el.className = cls || "";
}

$("testbridge").addEventListener("click", async () => {
  const base = normBridge($("bridge").value);
  if (!base) return setBridgeStatus("Enter a bridge URL, e.g. http://192.168.1.50:8000", "bad");
  setBridgeStatus("Requesting permission…");
  let granted;
  try {
    granted = await chrome.permissions.request({ origins: [base + "/*"] });
  } catch (e) {
    return setBridgeStatus("Permission error: " + e.message, "bad");
  }
  if (!granted) return setBridgeStatus("Permission denied for " + base, "bad");

  setBridgeStatus("Fetching " + base + "/api/health from the service worker…");
  const reply = await chrome.runtime.sendMessage({ type: "probeBridge", base });
  const r = reply?.res;
  if (!reply?.ok || !r) return setBridgeStatus("Worker error: " + (reply?.error || "no reply"), "bad");

  if (r.ok) {
    await chrome.storage.local.set({ bridge: base });
    // /api/health reports the collector's own state, so the same call tells us
    // whether the bridge is reachable AND whether it has data worth overlaying
    const j = r.json || {};
    const counts = `${j.access_points ?? "?"} AP(s) · ${j.clients ?? "?"} client(s)`;
    const age = j.age_seconds != null ? `, ${j.age_seconds}s old` : "";
    if (j.ok === false) {
      return setBridgeStatus(
        `Worker fetch to plain HTTP WORKS (HTTP ${r.status} in ${r.ms} ms) — but the `
        + `bridge itself isn't collecting: ${j.error || "unknown error"}. `
        + "Fix its console credentials; the transport is fine.", "bad");
    }
    return setBridgeStatus(
      `Works — HTTP ${r.status} in ${r.ms} ms · ${counts}${age}. `
      + "Worker fetch to plain HTTP is allowed; bridge saved.", "ok");
  }
  if (r.stage === "access") {
    return setBridgeStatus(
      `Reached it, but Cloudflare Access is asking for a login (HTTP ${r.status}). `
      + "Open the bridge URL in a tab and sign in once — the worker will then "
      + "carry the session cookie.", "bad");
  }
  if (r.stage === "fetch") {
    return setBridgeStatus(
      `Fetch failed: ${r.error}. Either the bridge isn't reachable at ${base} `
      + "(wrong host/port, not running, firewall), or Chrome blocked the "
      + "plain-HTTP request from the worker.", "bad");
  }
  if (r.stage === "http") {
    return setBridgeStatus(`Reached it, but HTTP ${r.status}: ${r.text.slice(0, 120)}`, "bad");
  }
  return setBridgeStatus(
    `Reached it (HTTP ${r.status}) but the reply isn't JSON — something else is `
    + `listening on that port? First bytes: ${r.text.slice(0, 80)}`, "bad");
});

$("disable").addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ type: "disable" });
  setStatus("Disabled. Reload the console tab to remove the panel.");
});

init();
