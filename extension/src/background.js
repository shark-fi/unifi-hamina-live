/* Service worker: (de)register the content script for the user's console origin.
 *
 * The console lives on a LAN IP or unifi.ui.com, so we can't hardcode a match.
 * The popup asks for the origin, requests host permission (a user gesture), then
 * messages us to register src/content.js for <origin>/innerspace/*. Registration
 * persists across sessions; we also re-assert it on startup from stored config.
 */
const SCRIPT_ID = "unifi-innerspace";
const PROBE_ID = "unifi-innerspace-probe";

async function registerForOrigin(origin) {
  // The console is an SPA; InnerSpace lives at nested paths like
  // /network/<site>/innerspace/<plan> (local) or
  // /consoles/<id>/network/<site>/innerspace/<plan> (remote). Register for the
  // whole origin and let the content script guard on the path.
  const matches = [origin + "/*"];
  try {
    const existing = await chrome.scripting.getRegisteredContentScripts({
      ids: [SCRIPT_ID, PROBE_ID],
    });
    if (existing.length) {
      await chrome.scripting.unregisterContentScripts({ ids: [SCRIPT_ID, PROBE_ID] });
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
    {
      // page-context probe: must run before the app issues any request
      id: PROBE_ID,
      matches,
      js: ["src/probe.js"],
      runAt: "document_start",
      world: "MAIN",
      persistAcrossSessions: true,
    },
  ]);
  return matches;
}

async function unregister() {
  try {
    await chrome.scripting.unregisterContentScripts({ ids: [SCRIPT_ID, PROBE_ID] });
  } catch (_e) {
    /* already gone */
  }
}

/* Fetch on behalf of the content script. Remote consoles are reached over a
 * separate origin (<console-id>.id.ui.direct), and content-script fetches are
 * subject to the page's CORS; the worker's are not, given host permission. */
async function proxyGet(url) {
  const r = await fetch(url, { credentials: "include", headers: { Accept: "application/json" } });
  const text = await r.text();
  return { ok: r.ok, status: r.status, type: r.headers.get("content-type") || "", text };
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    if (msg?.type === "enable" && msg.origin) {
      const matches = await registerForOrigin(msg.origin);
      await chrome.storage.local.set({ origin: msg.origin, site: msg.site || "" });
      sendResponse({ ok: true, matches });

    } else if (msg?.type === "get" && msg.url) {
      try {
        sendResponse({ ok: true, res: await proxyGet(msg.url) });
      } catch (e) {
        sendResponse({ ok: false, error: String(e && e.message || e) });
      }
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
