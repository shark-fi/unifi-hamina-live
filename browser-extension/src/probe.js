/* Main-world probe: report the console's API URLs to the extension.
 *
 * Runs in the PAGE's JS context (world: "MAIN") at document_start, so it wraps
 * the app's own request functions before it makes any call. This is the only
 * vantage point that works everywhere: performance entries get evicted under
 * load and omit websockets, and webRequest needs manifest host permissions
 * that an optional-permission extension doesn't have.
 *
 * It only reads request URLs — no bodies, no responses, no mutation. The
 * originals are always called through.
 */
(() => {
  if (window.__unifiLiveProbe) return;
  window.__unifiLiveProbe = true;

  // /proxy/network/... on the console origin, OR the direct tunnel host that
  // remote access uses (<console-id>.id.ui.direct), which is a different origin
  const RX = /\/proxy\/network\/|\.id\.ui\.direct/i;
  const send = (u) => {
    try {
      if (!u) return;
      const abs = new URL(String(u), location.href).href;
      if (!RX.test(abs)) return;
      window.dispatchEvent(new CustomEvent("unifi-live-api", { detail: abs }));
    } catch (_e) { /* never disturb the page */ }
  };

  const origFetch = window.fetch;
  if (typeof origFetch === "function") {
    window.fetch = function (input, init) {
      try { send(typeof input === "string" ? input : (input && input.url)); } catch (_e) {}
      return origFetch.apply(this, arguments);
    };
  }

  const origOpen = XMLHttpRequest.prototype.open;
  if (typeof origOpen === "function") {
    XMLHttpRequest.prototype.open = function (method, url) {
      try { send(url); } catch (_e) {}
      return origOpen.apply(this, arguments);
    };
  }

  const OrigWS = window.WebSocket;
  if (typeof OrigWS === "function") {
    const WS = function (url, protocols) {
      try { send(url); } catch (_e) {}
      return protocols === undefined ? new OrigWS(url) : new OrigWS(url, protocols);
    };
    WS.prototype = OrigWS.prototype;
    for (const k of ["CONNECTING", "OPEN", "CLOSING", "CLOSED"]) WS[k] = OrigWS[k];
    window.WebSocket = WS;
  }
})();
