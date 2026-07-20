#!/usr/bin/env bash
#
# One-command installer for unifi-hamina-live.
#
#   Local (from a checkout):   ./install.sh
#   Remote (no checkout):      curl -fsSL https://raw.githubusercontent.com/shark-fi/unifi-hamina-live/main/install.sh | bash
#
# Options (env vars or flags):
#   --dir PATH        install location        (default: current repo dir, else /opt/unifi-hamina-live)
#   --branch NAME     git branch to clone     (default: main)
#   --systemd         install+enable a systemd service (needs root/sudo)
#   --user NAME       run the service as this user     (default: invoking user)
#   --start           start the service after installing (implies --systemd)
#   --no-openintent   skip fetching the OpenIntent exporter (live API only)
#   --exporter-dir P  where to place the exporter        (default: sibling of --dir)
#   -y | --non-interactive  never prompt; leave .env values as-is
#   --interactive     force prompts even when piped (reads from /dev/tty)
#   -h | --help       show this help
#
# By default the installer also fetches the companion OpenIntent exporter
# (unifi-hamina-export) and enables the scheduled refresh, wiring up the full
# Hamina integration: live Meraki-compatible API + near-live OpenIntent zip.
#
# When run on a terminal it prompts for any UniFi .env values that are still
# empty or at their example defaults (host / username / password), and it
# generates a random MERAKI_COMPAT_API_KEY. Use --non-interactive to skip.
#
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/shark-fi/unifi-hamina-live.git}"
BRANCH="${BRANCH:-main}"
INSTALL_DIR="${INSTALL_DIR:-}"
DO_SYSTEMD=0
DO_START=0
SERVICE_USER="${SERVICE_USER:-}"
SERVICE_NAME="unifi-hamina-live"
EXPORTER_URL="${EXPORTER_URL:-https://github.com/shark-fi/unifi-hamina-export.git}"
EXPORTER_BRANCH="${EXPORTER_BRANCH:-main}"
EXPORTER_DIR="${EXPORTER_DIR:-}"
WITH_OPENINTENT=1
INTERACTIVE=auto   # auto | yes | no

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# Clone a repo shallowly, or fast-forward it if already present.
clone_or_update() {
  local url="$1" dir="$2" branch="$3"
  if [ -d "$dir/.git" ]; then
    log "updating $(basename "$dir") in $dir"
    git -C "$dir" fetch --depth 1 origin "$branch"
    git -C "$dir" checkout -q -B "$branch" "origin/$branch"
    git -C "$dir" reset --hard -q "origin/$branch"
  else
    log "cloning $url ($branch) into $dir"
    mkdir -p "$(dirname "$dir")"
    git clone --depth 1 --branch "$branch" "$url" "$dir"
  fi
}

# set_env KEY VALUE FILE — replace an existing KEY= line or append it. Avoids
# sed so the value may contain any character (slashes, &, |, quotes in passwords).
set_env() {
  local key="$1" val="$2" file="$3" tmp
  tmp="$(mktemp)"
  grep -vE "^${key}=" "$file" > "$tmp" 2>/dev/null || true
  printf '%s=%s\n' "$key" "$val" >> "$tmp"
  cat "$tmp" > "$file"
  rm -f "$tmp"
}

# env_get KEY — current value of KEY in ./.env ("" if unset).
env_get() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2-; }

# is_placeholder KEY — true when the value is empty or the example default.
is_placeholder() {
  case "$1=$(env_get "$1")" in
    "$1=") return 0 ;;
    UNIFI_HOST=https://192.168.1.1) return 0 ;;
    UNIFI_USERNAME=local-admin) return 0 ;;
    UNIFI_PASSWORD=change-me) return 0 ;;
    MERAKI_COMPAT_API_KEY=replace-with-a-long-random-token) return 0 ;;
  esac
  return 1
}

# gen_token — a long random hex string for the Meraki facade key.
gen_token() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24
  else
    LC_ALL=C tr -dc 'a-f0-9' < /dev/urandom | head -c 48
  fi
}

# ask_env KEY LABEL SECRET — prompt on the tty; Enter keeps the current value.
ask_env() {
  local key="$1" label="$2" secret="${3:-0}" current ans
  current="$(env_get "$key")"
  if [ "$secret" = "1" ]; then
    printf '  %s [Enter=keep current]: ' "$label" > "$TTY"
    read -rs ans < "$TTY"; printf '\n' > "$TTY"
  else
    printf '  %s [%s]: ' "$label" "$current" > "$TTY"
    read -r ans < "$TTY"
  fi
  [ -n "$ans" ] && set_env "$key" "$ans" .env
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dir)            INSTALL_DIR="$2"; shift 2 ;;
    --branch)         BRANCH="$2"; shift 2 ;;
    --user)           SERVICE_USER="$2"; shift 2 ;;
    --systemd)        DO_SYSTEMD=1; shift ;;
    --start)          DO_SYSTEMD=1; DO_START=1; shift ;;
    --no-openintent)  WITH_OPENINTENT=0; shift ;;
    --openintent)     WITH_OPENINTENT=1; shift ;;
    --exporter-dir)   EXPORTER_DIR="$2"; shift 2 ;;
    --exporter-branch) EXPORTER_BRANCH="$2"; shift 2 ;;
    -y|--non-interactive) INTERACTIVE=no; shift ;;
    --interactive)    INTERACTIVE=yes; shift ;;
    -h|--help) awk 'NR>1 && /^#/{sub(/^# ?/,"");print;next} NR>1{exit}' "$0"; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

# --- prerequisites --------------------------------------------------------
command -v git >/dev/null 2>&1 || die "git is required but not installed."

PY=""
for c in python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
[ -n "$PY" ] || die "python3 (>=3.10) is required but not found."
PYVER="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
"$PY" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,10) else 1)' \
  || die "python $PYVER found, but >=3.10 is required."
log "using $PY ($PYVER)"

# --- locate / fetch the source -------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/pyproject.toml" ] \
   && grep -q 'name = "unifi-hamina-live"' "$SCRIPT_DIR/pyproject.toml" 2>/dev/null; then
  # Running from inside a checkout.
  INSTALL_DIR="${INSTALL_DIR:-$SCRIPT_DIR}"
  if [ "$INSTALL_DIR" != "$SCRIPT_DIR" ]; then
    log "copying source to $INSTALL_DIR"
    mkdir -p "$INSTALL_DIR"
    git -C "$SCRIPT_DIR" archive HEAD | tar -x -C "$INSTALL_DIR"
  fi
else
  # Piped / standalone: clone.
  INSTALL_DIR="${INSTALL_DIR:-/opt/unifi-hamina-live}"
  if [ -d "$INSTALL_DIR/.git" ]; then
    log "updating existing checkout in $INSTALL_DIR"
    git -C "$INSTALL_DIR" fetch --depth 1 origin "$BRANCH"
    git -C "$INSTALL_DIR" checkout -q "$BRANCH"
    git -C "$INSTALL_DIR" reset --hard -q "origin/$BRANCH"
  else
    log "cloning $REPO_URL ($BRANCH) into $INSTALL_DIR"
    mkdir -p "$(dirname "$INSTALL_DIR")"
    git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
  fi
fi
cd "$INSTALL_DIR"
INSTALL_DIR="$(pwd)"   # normalize to absolute
log "install dir: $INSTALL_DIR"

# --- virtualenv + package -------------------------------------------------
if [ ! -d .venv ]; then
  log "creating virtualenv"
  "$PY" -m venv .venv
fi
log "installing package + dependencies"
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -e .

# --- config ---------------------------------------------------------------
FRESH_ENV=0
if [ ! -f .env ]; then
  cp .env.example .env
  FRESH_ENV=1
  log "created .env from .env.example"
else
  log ".env already present — keeping your values"
fi

# Decide whether we can prompt: need a readable tty, unless forced off.
TTY=""
[ -r /dev/tty ] && TTY=/dev/tty
DO_PROMPT=0
case "$INTERACTIVE" in
  yes) DO_PROMPT=1; [ -n "$TTY" ] || die "--interactive given but no /dev/tty available" ;;
  no)  DO_PROMPT=0 ;;
  auto) [ -n "$TTY" ] && DO_PROMPT=1 ;;
esac

# Prompt only for the UniFi values that are still empty / at example defaults.
if [ "$DO_PROMPT" -eq 1 ]; then
  if is_placeholder UNIFI_HOST || is_placeholder UNIFI_USERNAME || is_placeholder UNIFI_PASSWORD; then
    log "let's fill in the UniFi connection ($INSTALL_DIR/.env):"
    is_placeholder UNIFI_HOST     && ask_env UNIFI_HOST     "UniFi console URL"       0
    is_placeholder UNIFI_USERNAME && ask_env UNIFI_USERNAME "UniFi local-admin user"  0
    is_placeholder UNIFI_PASSWORD && ask_env UNIFI_PASSWORD "UniFi password"          1
  fi
else
  if is_placeholder UNIFI_HOST || is_placeholder UNIFI_USERNAME || is_placeholder UNIFI_PASSWORD; then
    warn "UniFi values in $INSTALL_DIR/.env are unset — edit UNIFI_HOST / UNIFI_USERNAME / UNIFI_PASSWORD before starting"
  fi
fi

# Always ensure the Meraki facade has a real key (generate if still the default).
if is_placeholder MERAKI_COMPAT_API_KEY; then
  _key="$(gen_token)"
  set_env MERAKI_COMPAT_API_KEY "$_key" .env
  log "generated MERAKI_COMPAT_API_KEY: $_key"
fi

# --- OpenIntent exporter (full Hamina integration) ------------------------
# Fetch the companion exporter so the scheduled OpenIntent refresh works, and
# wire it into a freshly created .env. This is what turns the live API into a
# full integration (live Meraki-compatible feed + near-live OpenIntent zip).
if [ "$WITH_OPENINTENT" -eq 1 ]; then
  EXPORTER_DIR="${EXPORTER_DIR:-$(dirname "$INSTALL_DIR")/unifi-hamina-export}"
  if clone_or_update "$EXPORTER_URL" "$EXPORTER_DIR" "$EXPORTER_BRANCH"; then
    EXPORTER_SCRIPT="$EXPORTER_DIR/unifi_export.py"
    if [ -f "$EXPORTER_SCRIPT" ]; then
      log "OpenIntent exporter ready at $EXPORTER_SCRIPT (stdlib-only, no deps)"
      if [ "$FRESH_ENV" -eq 1 ]; then
        set_env OPENINTENT_EXPORTER_PATH "$EXPORTER_SCRIPT" .env
        set_env OPENINTENT_REFRESH_ENABLED true .env
        log "enabled OpenIntent refresh in .env"
      else
        warn "existing .env kept — to enable the refresh, set in $INSTALL_DIR/.env:"
        warn "  OPENINTENT_REFRESH_ENABLED=true"
        warn "  OPENINTENT_EXPORTER_PATH=$EXPORTER_SCRIPT"
      fi
    else
      warn "exporter fetched but $EXPORTER_SCRIPT missing — skipping OpenIntent wiring"
    fi
  else
    warn "could not fetch the OpenIntent exporter — continuing with live API only"
  fi
fi

# --- systemd (optional) ---------------------------------------------------
if [ "$DO_SYSTEMD" -eq 1 ]; then
  command -v systemctl >/dev/null 2>&1 || die "--systemd requested but systemctl not found."
  SUDO=""
  if [ "$(id -u)" -ne 0 ]; then
    command -v sudo >/dev/null 2>&1 || die "--systemd needs root; run as root or install sudo."
    SUDO="sudo"
  fi
  RUN_USER="${SERVICE_USER:-$(id -un)}"
  UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
  log "installing systemd unit at $UNIT_PATH (User=$RUN_USER)"
  # Render the template with real paths/user.
  sed -e "s#__INSTALL_DIR__#${INSTALL_DIR}#g" \
      -e "s#__USER__#${RUN_USER}#g" \
      deploy/unifi-hamina-live.service | $SUDO tee "$UNIT_PATH" >/dev/null
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable "$SERVICE_NAME"
  if [ "$DO_START" -eq 1 ]; then
    $SUDO systemctl restart "$SERVICE_NAME"
    log "service started — status: systemctl status $SERVICE_NAME"
  else
    log "service enabled — start it with: $SUDO systemctl start $SERVICE_NAME"
  fi
fi

# --- done -----------------------------------------------------------------
cat <<EOF

$(printf '\033[1;32m✓ installed\033[0m')  unifi-hamina-live in $INSTALL_DIR

Next steps:
  1. Review $INSTALL_DIR/.env  (UNIFI_HOST / UNIFI_USERNAME / UNIFI_PASSWORD)
EOF
if [ "$DO_SYSTEMD" -eq 1 ]; then
  cat <<EOF
  2. Restart the service:   ${SUDO:-sudo} systemctl restart $SERVICE_NAME
  3. Open the dashboard:    http://localhost:8080/
     Logs:                  ${SUDO:-sudo} journalctl -u $SERVICE_NAME -f
EOF
else
  cat <<EOF
  2. Run it:                cd $INSTALL_DIR && ./.venv/bin/python -m unifi_hamina_live
  3. Open the dashboard:    http://localhost:8080/
     (or re-run with --systemd to install it as a service)
EOF
fi
if [ "$WITH_OPENINTENT" -eq 1 ] && [ -f "${EXPORTER_SCRIPT:-/nonexistent}" ]; then
  cat <<EOF

OpenIntent refresh wired up (exporter: $EXPORTER_DIR)
  Fresh import zip:         http://localhost:8080/openintent/latest.zip
  Refresh status:           http://localhost:8080/openintent/status
  Re-import that zip into your Hamina Planner project for near-live data.
EOF
fi
