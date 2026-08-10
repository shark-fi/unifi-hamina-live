#!/usr/bin/env bash
# Verify a Cloudflare-tunnelled bridge, one layer at a time.
#
# The failure that matters here is silent: a tunnel published without an Access
# policy answers everything, to anyone who learns the hostname, and looks
# perfectly healthy while doing it. The neutral /api routes are unauthenticated
# by design — they expect to sit on your LAN — so they carry your whole client
# inventory: MACs, hostnames, IPs, SSIDs, and which AP each device is on. This
# script's main job is to make that state loud rather than invisible.
#
#   ./scripts/check-tunnel.sh unifi-bridge.example.com [http://localhost:8080]
#
# Optional, to also test the machine path (Zero Trust -> Service Auth -> tokens):
#   export CF_ACCESS_CLIENT_ID=...  CF_ACCESS_CLIENT_SECRET=...
set -u

HOST="${1:-}"
LOCAL="${2:-http://localhost:8080}"
if [ -z "$HOST" ]; then
  echo "usage: $0 <tunnel-hostname> [local-url]" >&2
  exit 2
fi
HOST="${HOST#https://}"; HOST="${HOST#http://}"; HOST="${HOST%%/*}"
PUB="https://$HOST"

red()  { printf '\033[31m%s\033[0m\n' "$*"; }
grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
ylw()  { printf '\033[33m%s\033[0m\n' "$*"; }
hdr()  { printf '\n\033[1m%s\033[0m\n' "$*"; }

fail=0

# --- 1. the bridge itself, bypassing the tunnel entirely ------------------
hdr "1. Bridge on the LAN  ($LOCAL/api/health)"
body=$(curl -fsS --max-time 10 "$LOCAL/api/health" 2>&1)
if [ $? -ne 0 ]; then
  red "  unreachable: $body"
  red "  -> nothing below can pass. Is the container up? docker compose ps"
  exit 1
fi
echo "  $body"
case "$body" in
  *'"ok":true'*|*'"ok": true'*) grn "  collecting" ;;
  *) ylw "  reachable but not collecting — check its console credentials" ;;
esac

# --- 2. the tunnel, unauthenticated: this SHOULD be refused ---------------
hdr "2. Tunnel without credentials  ($PUB/api/health)"
out=$(curl -sS --max-time 15 -o /tmp/ct.$$ -w '%{http_code} %{redirect_url}' "$PUB/api/health" 2>&1)
code=${out%% *}; redir=${out#* }
peek=$(head -c 200 /tmp/ct.$$ 2>/dev/null); rm -f /tmp/ct.$$
case "$code" in
  200)
    # Decide on the SHAPE of the body, never on keywords: the bridge's own
    # health JSON contains "access_points", so a keyword match for "access"
    # reports a wide-open endpoint as protected — inverting the one check this
    # script exists to make. A JSON object means the bridge itself answered.
    if printf '%s' "$peek" | grep -qE '^[[:space:]]*[{[]'; then
      red "  HTTP 200 WITH DATA — this endpoint is OPEN TO THE INTERNET."
      red "  Anyone with the hostname can read your client inventory."
      red "  -> Zero Trust > Access > Applications: add a self-hosted app for"
      red "     $HOST covering /api and require your identity. Then re-run."
      echo "     first bytes: $peek"
      fail=1
    elif printf '%s' "$peek" | grep -qiE 'cloudflareaccess\.com|<!doctype|<html'; then
      grn "  HTTP 200 but served an Access login page — policy is ON"
    else
      ylw "  HTTP 200, body neither JSON nor a login page — inspect it yourself:"
      echo "     first bytes: $peek"
      fail=1
    fi ;;
  301|302|303|307|308)
    if printf '%s' "$redir" | grep -qi 'cloudflareaccess\.com'; then
      grn "  HTTP $code to the Access login — policy is ON"
    else
      ylw "  HTTP $code to $redir — redirected, but not to Access. Check the policy."
      fail=1
    fi ;;
  401|403) grn "  HTTP $code — refused. Policy is ON." ;;
  000)     red "  no response — hostname not resolving, or the tunnel is down"; fail=1 ;;
  *)       ylw "  HTTP $code (unexpected): $peek" ;;
esac

# --- 3. the machine path, if a service token is configured ----------------
hdr "3. Tunnel with a service token"
if [ -z "${CF_ACCESS_CLIENT_ID:-}" ] || [ -z "${CF_ACCESS_CLIENT_SECRET:-}" ]; then
  echo "  skipped (CF_ACCESS_CLIENT_ID / CF_ACCESS_CLIENT_SECRET not set)"
  echo "  The browser extension does not need this — it rides your Access"
  echo "  session cookie. Set it only for headless callers."
else
  body=$(curl -sS --max-time 15 \
    -H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" \
    -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET" \
    "$PUB/api/health" 2>&1)
  case "$body" in
    *'"ok"'*) grn "  token accepted"; echo "  $body" ;;
    *) red "  token rejected or blocked:"; echo "  $(printf '%s' "$body" | head -c 200)"
       red "  -> the Access application needs this token in its policy"; fail=1 ;;
  esac
fi

hdr "Summary"
if [ "$fail" -eq 0 ]; then
  grn "Looks right. In the extension's popup, set the Bridge URL to $PUB"
  echo "and press Test bridge. If Access asks for a login, open $PUB in a tab,"
  echo "sign in once, then test again — the worker reuses that session cookie."
else
  red "Fix the items above before pointing anything at $PUB."
fi
exit "$fail"
