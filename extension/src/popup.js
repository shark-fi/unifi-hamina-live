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

/* Guess the scheme from the shape of the address. An IP or localhost is a LAN
 * bridge and is plain HTTP; a hostname is reached through a tunnel or reverse
 * proxy and is HTTPS. Defaulting everything to http sent the worker at a
 * tunnel's http:// address, which redirects to https — a cross-origin redirect
 * the worker has no permission to follow, so it failed as a bare "Failed to
 * fetch" that reads like the bridge is down. An explicit scheme always wins. */
function normBridge(v) {
  v = (v || "").trim();
  if (!v) return "";
  if (!/^https?:\/\//i.test(v)) {
    const host = v.split("/")[0].split(":")[0];
    const lan = /^(\d{1,3}\.){3}\d{1,3}$/.test(host)
      || host === "localhost" || /\.local$/i.test(host);
    v = (lan ? "http://" : "https://") + v;
  }
  try {
    return new URL(v).origin;
  } catch (_e) {
    return "";
  }
}

/* One bridge instance polls one console, so a bridge URL belongs to a console
 * rather than to the extension. A cloud console is identified by its
 * /consoles/<id> segment — minus the :epoch suffix, which changes — and a LAN
 * console by its origin. Must stay identical to consoleKey() in content.js. */
function consoleKeyFromUrl(u) {
  try {
    const url = new URL(u);
    const m = url.pathname.match(/\/consoles\/([^/]+)/);
    return url.origin + (m ? "/consoles/" + m[1].split(":")[0] : "");
  } catch (_e) {
    return "";
  }
}

let bridgeKey = "";

async function initBridge(stored) {
  const tab = await activeTab();
  bridgeKey = consoleKeyFromUrl(tab && tab.url) || normOrigin($("origin").value) || "";
  const map = stored.bridges || {};
  // carry a pre-per-console value over, but only while nothing else is stored
  $("bridge").value = map[bridgeKey]
    || (Object.keys(map).length ? "" : (stored.bridge || ""));
  $("bridgeFor").textContent = bridgeKey
    ? "Applies to " + bridgeKey.replace(/^https?:\/\//, "")
    : "Open the console's tab first — this is stored per console.";
}

/* Which of the two things this tab is. The console field used to take the tab
 * origin unconditionally, so opening the popup on a Hamina tab offered
 * "https://us.hamina.com" as your console — a value that can only fail.
 *
 * Reading tab.url needs `activeTab`, granted for the current tab when the user
 * clicks the extension's action — which is exactly when this popup runs. Without
 * it chrome.tabs.query still answers, but strips url and title unless host
 * permission for that origin was already granted. So the prefill worked on
 * consoles you had already enabled and silently did nothing on a new one: the
 * case it exists for. */
const HAMINA_RE = /(^|\.)hamina\.com$/i;

function isHaminaUrl(u) {
  try { return HAMINA_RE.test(new URL(u).hostname); } catch (_e) { return false; }
}

/* A console is anything that is not Hamina and looks like a UniFi surface:
 * unifi.ui.com, a console tunnel host, or a page with /network/ or /innerspace/
 * in its path. Deliberately loose — a wrong guess costs one edit, and refusing
 * to guess costs a retype every time. */
function isConsoleUrl(u) {
  try {
    const url = new URL(u);
    if (HAMINA_RE.test(url.hostname)) return false;
    return /(^|\.)ui\.com$/i.test(url.hostname)
      || /\.id\.ui\.direct$/i.test(url.hostname)
      || /\/(network|innerspace|consoles)\//.test(url.pathname);
  } catch (_e) { return false; }
}

/* Consoles you have enabled before, most recent first. Retyping a gateway
 * address on every switch is the single most tedious part of using this. */
async function rememberConsole(origin) {
  if (!origin) return;
  const { consoles } = await chrome.storage.local.get("consoles");
  const next = [origin, ...(consoles || []).filter((c) => c !== origin)].slice(0, 8);
  await chrome.storage.local.set({ consoles: next });
}

async function fillConsoleList() {
  const { consoles } = await chrome.storage.local.get("consoles");
  const dl = $("known-consoles");
  dl.innerHTML = (consoles || [])
    .map((c) => `<option value="${c.replace(/"/g, "&quot;")}"></option>`).join("");
}

async function init() {
  const stored = await chrome.storage.local.get(["origin", "site", "bridge", "bridges"]);
  const tab = await activeTab();
  const tabUrl = (tab && tab.url) || "";
  const tabOrigin = (() => {
    try { return new URL(tabUrl).origin; } catch (_e) { return ""; }
  })();

  // Prefer the tab's own origin ONLY when the tab is actually a console: on a
  // console page that is exactly the answer, and it stops a stored (or
  // mistyped) value being re-offered. On a Hamina tab it is the wrong answer.
  $("origin").value =
    (isConsoleUrl(tabUrl) ? tabOrigin : "") || stored.origin || "";
  $("site").value = stored.site || "";

  // …and fill the Hamina field from the tab when that is where you are.
  const haminaStored = (await chrome.storage.local.get("haminaOrigin")).haminaOrigin;
  $("haminaorigin").value =
    (isHaminaUrl(tabUrl) ? tabOrigin : "") || haminaStored || "";

  await fillConsoleList();
  if (stored.origin) setStatus(`Enabled for ${stored.origin}`);
  await initBridge(stored);
}

function setStatus(msg) {
  $("status").textContent = msg;
}

$("enable").addEventListener("click", async () => {
  const origin = normOrigin($("origin").value);
  if (!origin) return setStatus("Enter a valid console URL.");
  /* Two URL fields sitting one above the other, and the bridge is the one you
   * were last asked to paste — so it lands here. Registering against it means
   * the overlay never runs on the console at all, and the symptom is simply
   * nothing happening, which reads like the extension being broken. */
  if (normBridge($("bridge").value) === origin) {
    return setStatus(
      "That's the bridge, not a console. Console URL is the page you want the "
      + "overlay drawn on — your console's own address.");
  }
  const tab = await activeTab();
  const tabOrigin = (() => {
    try { return new URL(tab && tab.url).origin; } catch (_e) { return ""; }
  })();
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
  if (tab && tab.url && tab.url.startsWith(origin)) {
    try {
      await chrome.scripting.executeScript({
        target: { tabId: tab.id }, files: ["src/probe.js"], world: "MAIN" });
      await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["src/content.js"] });
    } catch (_e) {
      /* not an injectable page; it'll load on next navigation */
    }
  }
  await rememberConsole(origin);
  await fillConsoleList();
  setStatus(tabOrigin && tabOrigin !== origin
    ? `Enabled for ${origin} — but this tab is ${tabOrigin}, so nothing will `
      + "appear here. Did you mean this tab's address?"
    : `Enabled for ${origin}. Open the InnerSpace map.`);
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
    if (!bridgeKey) {
      return setBridgeStatus(
        "Reachable, but there's no console to attach it to — open the console's "
        + "tab and press Test bridge again. A bridge is stored per console.", "bad");
    }
    const map = (await chrome.storage.local.get("bridges")).bridges || {};
    map[bridgeKey] = base;
    await chrome.storage.local.set({ bridges: map });
    // /api/health reports the collector's own state, so the same call tells us
    // whether the bridge is reachable AND whether it has data worth overlaying
    const j = r.json || {};
    const counts = `${j.access_points ?? "?"} AP(s) · ${j.clients ?? "?"} client(s)`;
    const age = j.age_seconds != null ? `, ${j.age_seconds}s old` : "";
    if (j.ok === false) {
      return setBridgeStatus(
        `The worker reached it (HTTP ${r.status} in ${r.ms} ms) — but the bridge `
        + `itself isn't collecting: ${j.error || "unknown error"}. `
        + "Fix its console credentials; the transport is fine.", "bad");
    }
    return setBridgeStatus(
      `Works — HTTP ${r.status} in ${r.ms} ms · ${counts}${age}. Saved for `
      + bridgeKey.replace(/^https?:\/\//, "") + ".", "ok");
  }
  if (r.stage === "access") {
    return setBridgeStatus(
      `Reached it, but Cloudflare Access is asking for a login (HTTP ${r.status}). `
      + "Open the bridge URL in a tab and sign in once — the worker will then "
      + "carry the session cookie.", "bad");
  }
  if (r.stage === "fetch") {
    const httpToTunnel = /^http:\/\//i.test(base) && !/^http:\/\/(\d|localhost)/i.test(base);
    return setBridgeStatus(
      `Fetch failed: ${r.error}. `
      + (httpToTunnel
        ? `${base} is plain HTTP on a hostname — if that's a tunnel it redirects `
          + "to https, and the worker can't follow a redirect to an origin it has "
          + "no permission for. Try the https:// address."
        : `Either the bridge isn't reachable at ${base} (wrong host/port, not `
          + "running, firewall), or Chrome blocked the plain-HTTP request from "
          + "the worker."), "bad");
  }
  if (r.stage === "http") {
    return setBridgeStatus(`Reached it, but HTTP ${r.status}: ${r.text.slice(0, 120)}`, "bad");
  }
  return setBridgeStatus(
    `Reached it (HTTP ${r.status}) but the reply isn't JSON — something else is `
    + `listening on that port? First bytes: ${r.text.slice(0, 80)}`, "bad");
});

/* --- Hamina overlay ---------------------------------------------------- */

function setHaminaStatus(msg, cls) {
  const el = $("haminastatus");
  el.textContent = msg;
  el.className = cls || "";
}

$("haminaenable").addEventListener("click", async () => {
  let base = ($("haminaorigin").value || "").trim().replace(/\/+$/, "");
  if (!base) return setHaminaStatus("Enter the Hamina URL you sign in to.", "bad");
  if (!/^https?:\/\//i.test(base)) base = "https://" + base;
  let origin;
  try {
    origin = new URL(base).origin;
  } catch (_e) {
    return setHaminaStatus("That is not a URL.", "bad");
  }

  // Carry the bridge over explicitly. The overlay can fall back to a lone
  // console bridge, but being explicit avoids surprises once a second console
  // is configured and "the only one" stops being unambiguous.
  const bridge = ($("bridge").value || "").trim().replace(/\/+$/, "");

  setHaminaStatus("Requesting permission…");
  let granted;
  try {
    granted = await chrome.permissions.request({ origins: [origin + "/*"] });
  } catch (e) {
    return setHaminaStatus("Permission error: " + e.message, "bad");
  }
  if (!granted) return setHaminaStatus("Permission denied for " + origin, "bad");

  const reply = await chrome.runtime.sendMessage(
    { type: "enableHamina", origin, bridge: bridge || undefined });
  if (!reply?.ok) return setHaminaStatus("Failed: " + (reply?.error || "?"), "bad");
  setHaminaStatus("Enabled for " + origin + " — reload the Hamina tab.", "ok");
});

$("haminadisable").addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ type: "disableHamina" });
  setHaminaStatus("Disabled. Reload the Hamina tab to remove the panel.");
});

$("disable").addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ type: "disable" });
  setStatus("Disabled. Reload the console tab to remove the panel.");
});

init();
