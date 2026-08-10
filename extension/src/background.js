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

/* Probe a unifi-hamina-live bridge from the WORKER, not from a page.
 *
 * The whole point of the bridge is to supply data on pages where the console's
 * own API is unreachable — a relayed unifi.ui.com session, or hamina.com. Those
 * are HTTPS pages and a LAN bridge is plain HTTP, so a content-script fetch
 * would be blocked as mixed content. The worker is not a document and is not
 * subject to that, given host permission — which is the assumption this probe
 * exists to verify before anything is built on it.
 *
 * Reports the outcome in enough detail to tell the failure modes apart: a
 * throw (blocked / refused / DNS), an HTTP status, or a body that isn't the
 * bridge's JSON (wrong port, something else listening). */
async function probeBridge(base) {
  const url = base.replace(/\/+$/, "") + "/api/health";
  const t0 = Date.now();
  let r;
  try {
    r = await fetch(url, { headers: { Accept: "application/json" } });
  } catch (e) {
    return { ok: false, stage: "fetch", url, error: String((e && e.message) || e) };
  }
  const ms = Date.now() - t0;
  const text = (await r.text()).slice(0, 400);
  if (!r.ok) return { ok: false, stage: "http", url, status: r.status, ms, text };
  try {
    return { ok: true, url, status: r.status, ms, json: JSON.parse(text) };
  } catch (_e) {
    return { ok: false, stage: "body", url, status: r.status, ms, text };
  }
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    if (msg?.type === "enable" && msg.origin) {
      const matches = await registerForOrigin(msg.origin);
      await chrome.storage.local.set({ origin: msg.origin, site: msg.site || "" });
      sendResponse({ ok: true, matches });

    } else if (msg?.type === "probeBridge" && msg.base) {
      sendResponse({ ok: true, res: await probeBridge(msg.base) });

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
