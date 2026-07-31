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

async function init() {
  const stored = await chrome.storage.local.get(["origin", "site"]);
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

$("disable").addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ type: "disable" });
  setStatus("Disabled. Reload the console tab to remove the panel.");
});

init();
