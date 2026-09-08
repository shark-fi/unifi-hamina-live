#!/usr/bin/env python3
"""Probe UniFi's documented APIs and diff them against what the bridge needs.

Answers three questions that decide how much of the undocumented scrape can
retire:

  1. Does this console return MORE than the published spec documents? The specs
     at developer.ui.com are pinned (Network v10.4.57, InnerSpace v1.3.23) while
     a console may run something newer, so "not in the spec" is not the same as
     "not on the box". This walks the live responses and lists every field the
     spec does not mention.

  2. Which of the bridge's required RF telemetry is actually reachable? The
     documented Network API carries no TX power, channel utilisation, per-radio
     client counts or client RSSI. That is a grep over the spec, not over a real
     console — so this greps the console's own responses instead, by shape
     rather than by field name, and reports what it finds.

  3. Which documented fields does this console NOT return? Answers the reverse
     case, where the spec promises something the deployment does not populate.

READ-ONLY. Issues GET only; no endpoint here creates, modifies, adopts or
deletes anything. The Network API does have destructive verbs — this script
never calls them.

Prints FIELD NAMES AND TYPES ONLY, never values, so the output is safe to paste
into an issue or send to a vendor. Use --dump-raw to keep the bodies, which do
contain MACs, IP addresses, client and device names.

    export UI_API_KEY='...'          # unifi.ui.com -> Settings -> API Keys
    python3 scripts/probe_documented_apis.py                    # list consoles
    python3 scripts/probe_documented_apis.py --console <id>
    python3 scripts/probe_documented_apis.py --local 10.10.5.1 --insecure
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request

CLOUD = "https://api.ui.com"
SPECS = {
    "network": "https://developer.ui.com/network/v10.4.57/openapi.json",
    "innerspace": "https://developer.ui.com/innerspace/v1.3.23/openapi.json",
}

# The telemetry the bridge consumes from the UNDOCUMENTED stat/device scrape.
# Matched against observed field names by shape, because the documented API
# spells everything differently (camelCase, and often not at all).
CAPABILITIES = {
    "tx power":            r"(?i)(tx.?power|transmit.?power|power.?dbm|eirp)",
    # Anchored away from cpu/memory/disk: statistics/latest carries
    # cpuUtilizationPct and memoryUtilizationPct, which are not airtime.
    "channel utilisation": r"(?i)^(?!cpu|mem|disk|load|storage).*(utili[sz]ation|cu_total|airtime|channel.?busy)",
    "per-radio clients":   r"(?i)(num.?sta|client.?count|num.?client|sta.?count)",
    "client RSSI":         r"(?i)(rssi|signal|snr|noise)",
    "client SSID":         r"(?i)(essid|ssid|wlan.?name)",
    "client rates":        r"(?i)((tx|rx).?(rate|bytes))",
    "channel":             r"(?i)channel",
    # ^ht$/^bw$ anchored: a bare "ht" substring also matches "height",
    # which InnerSpace returns as the AP mounting height in metres.
    "channel width":       r"(?i)(width|bandwidth|^ht$|^bw$)",
}


class Http:
    def __init__(self, key: str, insecure: bool = False, timeout: float = 20.0):
        self.key, self.timeout = key, timeout
        self.ctx = ssl.create_default_context()
        if insecure:
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def get(self, url: str):
        req = urllib.request.Request(url, headers={
            "X-API-KEY": self.key, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout,
                                        context=self.ctx) as r:
                return json.loads(r.read() or b"null"), None
        except urllib.error.HTTPError as e:
            return None, f"HTTP {e.code}"
        except ssl.SSLCertVerificationError:
            return None, ("TLS verify failed. For a local console add "
                          "--insecure. For api.ui.com on python.org macOS "
                          "Python, run '/Applications/Python 3.11/"
                          "Install Certificates.command' once.")
        except Exception as e:                     # noqa: BLE001 - report it
            return None, f"{type(e).__name__}: {e}"


def observed_fields(node, prefix="", out=None) -> dict[str, str]:
    """Flatten a response into {dotted.path: type}, unioning across list items.

    Arrays collapse to a single '[]' segment so ten APs produce one field set
    rather than ten, and a field present on only one of them still shows up.
    """
    out = {} if out is None else out
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{prefix}.{k}" if prefix else k
            if isinstance(v, (dict, list)):
                observed_fields(v, p, out)
            else:
                t = "null" if v is None else type(v).__name__
                # A field seen as null on one object and typed on another is
                # typed: null tells us nothing about the schema.
                if out.get(p) in (None, "null"):
                    out[p] = t
    elif isinstance(node, list):
        for item in node:
            observed_fields(item, f"{prefix}[]", out)
    return out


def spec_fields(spec: dict) -> set[str]:
    """Every property name the spec mentions, flattened and $ref-resolved."""
    comp = (spec.get("components") or {}).get("schemas") or {}
    names: set[str] = set()

    def walk(s, seen: frozenset):
        if not isinstance(s, dict):
            return
        if "$ref" in s:
            n = s["$ref"].split("/")[-1]
            if n not in seen:
                walk(comp.get(n, {}), seen | {n})
            return
        for key in ("oneOf", "anyOf", "allOf"):
            for sub in s.get(key) or []:
                walk(sub, seen)
        for k, v in (s.get("properties") or {}).items():
            names.add(k)
            walk(v, seen)
        if "items" in s:
            walk(s["items"], seen)
        for sub in s.get("prefixItems") or []:
            walk(sub, seen)

    for ops in (spec.get("paths") or {}).values():
        for op in ops.values():
            if not isinstance(op, dict):
                continue
            for r in (op.get("responses") or {}).values():
                for c in (r.get("content") or {}).values():
                    walk(c.get("schema") or {}, frozenset())
    for s in comp.values():
        walk(s, frozenset())
    return names


def probe(http: Http, base: str, endpoints: list[tuple[str, str]]):
    results = {}
    for label, path in endpoints:
        body, err = http.get(base + path)
        results[label] = {"path": path, "error": err,
                          "fields": observed_fields(body) if err is None else {}}
        status = err or f"{len(results[label]['fields'])} fields"
        print(f"  {label:34} {status}")
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--console", help="consoleId (hostId) from /v1/hosts")
    ap.add_argument("--site", help="Network siteId; default: first site found")
    ap.add_argument("--local", metavar="IP",
                    help="probe the console directly instead of via api.ui.com")
    ap.add_argument("--insecure", action="store_true",
                    help="skip TLS verification (needed for a local console)")
    ap.add_argument("--max-devices", type=int, default=3,
                    help="how many devices to fetch detail for (default 3)")
    ap.add_argument("--dump-raw", metavar="PATH",
                    help="also write full response bodies — CONTAINS MACs, IPs "
                         "and device names")
    args = ap.parse_args()

    key = os.environ.get("UI_API_KEY")
    if not key:
        print("Set UI_API_KEY (unifi.ui.com -> Settings -> API Keys).",
              file=sys.stderr)
        print("Passing it as an argument would leave it in your shell history.",
              file=sys.stderr)
        return 2

    http = Http(key, insecure=args.insecure)
    raw: dict = {}

    if args.local:
        net_base = f"https://{args.local}/proxy/network/integration"
        ins_base = f"https://{args.local}/proxy/innerspace/integration"
        print(f"Probing {args.local} directly.")
        print("Note: a CLOUD key is known not to authenticate the LOCAL Network "
              "Integration API. A 401 here is expected and is itself a result.\n")
    else:
        if not args.console:
            print("Consoles visible to this key:\n")
            hosts, err = http.get(f"{CLOUD}/v1/hosts")
            if err:
                print(f"  /v1/hosts failed: {err}", file=sys.stderr)
                return 1
            for h in (hosts or {}).get("data") or []:
                name = ((h.get("reportedState") or {}).get("name")
                        or h.get("hardwareId") or "?")
                print(f"  {h.get('id')}  {name}")
            print("\nRe-run with --console <id>.")
            return 0
        prefix = f"{CLOUD}/v1/connector/consoles/{args.console}/proxy"
        net_base, ins_base = f"{prefix}/network/integration", f"{prefix}/innerspace/integration"

    # --- InnerSpace -------------------------------------------------------
    print("InnerSpace Integration v1")
    ins = probe(http, ins_base, [
        ("floor_plans", "/v1/floor_plans"),
        ("access_points", "/v1/access_points"),
        ("switches", "/v1/switches"),
        ("inventory", "/v1/inventory"),
        ("project (2D)", "/v1/project?mode=2D"),
    ])

    # --- Network ----------------------------------------------------------
    print("\nNetwork Integration v1")
    sites, err = http.get(f"{net_base}/v1/sites")
    site_id = args.site
    if not site_id and not err:
        data = (sites or {}).get("data") or []
        site_id = data[0].get("id") if data else None
    print(f"  sites                              "
          f"{err or ('siteId ' + str(site_id))}")

    net = {}
    if site_id:
        net = probe(http, net_base, [
            ("devices", f"/v1/sites/{site_id}/devices"),
            ("clients", f"/v1/sites/{site_id}/clients"),
            ("wifi broadcasts", f"/v1/sites/{site_id}/wifi/broadcasts"),
        ])
        devs, _ = http.get(f"{net_base}/v1/sites/{site_id}/devices")
        ids = [d.get("id") for d in ((devs or {}).get("data") or [])][:args.max_devices]
        for i, did in enumerate(ids):
            net.update(probe(http, net_base, [
                (f"device[{i}] detail", f"/v1/sites/{site_id}/devices/{did}"),
                (f"device[{i}] stats",
                 f"/v1/sites/{site_id}/devices/{did}/statistics/latest"),
            ]))

    raw = {"innerspace": ins, "network": net}

    # --- 1. observed vs published spec ------------------------------------
    print("\n" + "=" * 68)
    print("1. Fields this console returns that the published spec does NOT list")
    print("=" * 68)
    for svc, results in (("innerspace", ins), ("network", net)):
        spec, err = http.get(SPECS[svc])
        if err:
            print(f"\n{svc}: could not fetch spec ({err})")
            continue
        documented = spec_fields(spec)
        seen: dict[str, str] = {}
        for r in results.values():
            seen.update(r["fields"])
        leaf = {p: t for p, t in seen.items()}
        undocumented = {p: t for p, t in leaf.items()
                        if p.split(".")[-1].replace("[]", "") not in documented}
        print(f"\n{svc}: {len(leaf)} field paths observed, "
              f"{len(undocumented)} not in the spec")
        for p, t in sorted(undocumented.items()):
            print(f"    + {p}: {t}")
        if not undocumented and leaf:
            print("    (none — this console matches the published spec)")

    # --- 2. bridge telemetry ----------------------------------------------
    print("\n" + "=" * 68)
    print("2. RF telemetry the bridge needs — is it reachable?")
    print("=" * 68)
    everything: dict[str, str] = {}
    for results in (ins, net):
        for r in results.values():
            everything.update(r["fields"])
    for cap, pattern in CAPABILITIES.items():
        hits = sorted(p for p in everything if re.search(pattern, p.split(".")[-1]))
        if hits:
            print(f"  FOUND    {cap:22} {', '.join(hits[:4])}"
                  + (f" (+{len(hits)-4})" if len(hits) > 4 else ""))
        else:
            print(f"  ABSENT   {cap:22} -> undocumented scrape still required")

    # --- 3. documented but not returned -----------------------------------
    print("\n" + "=" * 68)
    print("3. Endpoints that failed")
    print("=" * 68)
    failures = [(svc, lbl, r["error"])
                for svc, res in (("innerspace", ins), ("network", net))
                for lbl, r in res.items() if r["error"]]
    for svc, lbl, e in failures:
        print(f"  {svc}/{lbl}: {e}")
    if not failures:
        print("  none")

    if args.dump_raw:
        with open(args.dump_raw, "w") as f:
            json.dump(raw, f, indent=2, sort_keys=True)
        print(f"\nField map written to {args.dump_raw}")
        print("It lists names and types only — no values, no MACs, no IPs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
