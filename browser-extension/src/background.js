/* Service worker: (de)register the content script for the user's console origin.
 *
 * The console lives on a LAN IP or unifi.ui.com, so we can't hardcode a match.
 * The popup asks for the origin, requests host permission (a user gesture), then
 * messages us to register src/content.js for <origin>/innerspace/*. Registration
 * persists across sessions; we also re-assert it on startup from stored config.
 */
const SCRIPT_ID = "unifi-innerspace";

/* Discover the console's API prefix from real network traffic.
 * The content script can only inspect performance entries, which a busy
 * console evicts (and which never include websockets) — a large console
 * reported seeing none at all. The worker sees the requests themselves. */
const PROXY_RX = /^(https?:\/\/[^/]+(?:\/[^?#]*?)?\/proxy\/network)\//;
const basesByTab = new Map();          // tabId -> [base, ...] newest first

function noteRequest(tabId, url) {
  const m = String(url || "").match(PROXY_RX);
  if (!m || tabId == null || tabId < 0) return;
  const base = m[1] + "/api";
  const list = basesByTab.get(tabId) || [];
  const i = list.indexOf(base);
  if (i === 0) return;
  if (i > 0) list.splice(i, 1);
  list.unshift(base);
  basesByTab.set(tabId, list.slice(0, 6));
}

let watching = false;
function watchTraffic() {
  try {
    if (watching || !chrome.webRequest?.onBeforeRequest) return;
    watching = true;
    chrome.webRequest.onBeforeRequest.addListener(
      (d) => noteRequest(d.tabId, d.url),
      { urls: ["*://*/*"] });        // events arrive only for granted hosts
  } catch (_e) { watching = false; /* no permission yet; retried on enable */ }
}
watchTraffic();
chrome.tabs?.onRemoved?.addListener((tabId) => basesByTab.delete(tabId));

async function registerForOrigin(origin) {
  // The console is an SPA; InnerSpace lives at nested paths like
  // /network/<site>/innerspace/<plan> (local) or
  // /consoles/<id>/network/<site>/innerspace/<plan> (remote). Register for the
  // whole origin and let the content script guard on the path.
  const matches = [origin + "/*"];
  try {
    const existing = await chrome.scripting.getRegisteredContentScripts({
      ids: [SCRIPT_ID],
    });
    if (existing.length) {
      await chrome.scripting.unregisterContentScripts({ ids: [SCRIPT_ID] });
    }
  } catch (_e) {
    /* nothing registered yet */
  }
  await chrome.scripting.registerContentScripts([
    {
      id: SCRIPT_ID,
      matches,
      js: ["src/content.js"],
      runAt: "document_idle",
      persistAcrossSessions: true,
    },
  ]);
  return matches;
}

async function unregister() {
  try {
    await chrome.scripting.unregisterContentScripts({ ids: [SCRIPT_ID] });
  } catch (_e) {
    /* already gone */
  }
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    if (msg?.type === "enable" && msg.origin) {
      const matches = await registerForOrigin(msg.origin);
      watchTraffic();          // re-arm now that the host permission exists
      await chrome.storage.local.set({ origin: msg.origin, site: msg.site || "" });
      sendResponse({ ok: true, matches });
    } else if (msg?.type === "bases") {
      sendResponse({ ok: true, bases: basesByTab.get(_sender?.tab?.id) || [] });
    } else if (msg?.type === "disable") {
      await unregister();
      await chrome.storage.local.remove(["origin"]);
      sendResponse({ ok: true });
    } else {
      sendResponse({ ok: false, error: "unknown message" });
    }
  })();
  return true; // async sendResponse
});

// Re-assert registration on browser startup (in case it was cleared).
chrome.runtime.onStartup.addListener(async () => {
  const { origin } = await chrome.storage.local.get("origin");
  if (origin) {
    const granted = await chrome.permissions.contains({ origins: [origin + "/*"] });
    if (granted) await registerForOrigin(origin);
  }
});
