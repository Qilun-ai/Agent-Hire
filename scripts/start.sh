#!/usr/bin/env bash
#
# Start everything Agent-Hire needs, in the right order, and wait until each part is actually
# ready before starting the next.
#
# Each service starts after its dependency passes a health check.
# There is no database server to start: the API, job index, and agent each keep SQLite/JSON files.
# The agent is the front door on APP_PORT (UI + chat); the API listens on an internal port behind it.
#
#   ./scripts/start.sh          start the API (which renders the web UI) and the agent on :8000
#   ./scripts/start.sh --stop   stop everything this script started
#
# Logs go to .run/*.log. Ctrl-C stops everything; so does --stop from another terminal.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
ENV_FILE="${AGENT_HIRE_ENV_FILE:-.env}"
MODE=production
CHECK_ONLY=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --env-file) [ "$#" -ge 2 ] || { echo 'missing --env-file path' >&2; exit 2; }; ENV_FILE="$2"; shift 2 ;;
    --stop) MODE=stop; shift ;;
    --check) CHECK_ONLY=1; shift ;;
    *) echo "unknown startup option: $1" >&2; exit 2 ;;
  esac
done
# An explicit alternative never also sources the checkout .env.
if [ -f "$ENV_FILE" ]; then
  set -a
  . "$(realpath "$ENV_FILE")"
  set +a
elif [ "$ENV_FILE" != .env ]; then
  echo 'selected environment file is missing' >&2
  exit 2
fi
RUN_DIR="${AGENT_HIRE_RUN_DIR:-.run}"
# The installer can launch this script detached. Recording the supervisor's own pid lets a later
# `--stop` (or `make down`) end that background process too, instead of leaving it sleeping after
# its services are gone.
SUPERVISOR_FILE="$RUN_DIR/supervisor"

APP_PORT="${APP_PORT:-8000}"
API_INTERNAL_PORT="${API_INTERNAL_PORT:-8010}"
declare -A PORTS_SEEN=()
for name in APP_PORT API_INTERNAL_PORT; do
  value="${!name}"
  if [[ ! "$value" =~ ^[1-9][0-9]{0,4}$ ]] || (( value > 65535 )); then
    echo "$name must be a decimal port in 1..65535" >&2; exit 2
  fi
  if [ -n "${PORTS_SEEN[$value]:-}" ]; then
    echo "$name conflicts with ${PORTS_SEEN[$value]}" >&2; exit 2
  fi
  PORTS_SEEN[$value]="$name"
done
export APP_PORT API_INTERNAL_PORT
export AGENT_HIRE_API_URL="http://127.0.0.1:$API_INTERNAL_PORT"
export API_ORIGIN="${API_ORIGIN:-http://127.0.0.1:$APP_PORT}"
if [ "$CHECK_ONLY" = 1 ]; then
  printf 'APP_PORT=%s\nAPI_INTERNAL_PORT=%s\n' "$APP_PORT" "$API_INTERNAL_PORT"
  exit 0
fi
mkdir -p "$RUN_DIR"
chmod 700 "$RUN_DIR"
umask 077

# The agent reads the AI provider settings from the API through an internal route guarded by a
# shared secret (the browser's token must not unlock stored API keys). Generate it once into the
# private run directory; a value you export yourself wins.
AGENT_ENV_FILE="$RUN_DIR/agent.env"
if [ "$MODE" != stop ] && [ -z "${AGENT_HIRE_AGENT_SECRET:-}" ]; then
  if [ ! -s "$AGENT_ENV_FILE" ]; then
    node -p '"AGENT_HIRE_AGENT_SECRET=" + require("node:crypto").randomBytes(32).toString("base64url")' > "$AGENT_ENV_FILE"
  fi
  set -a
  . "$AGENT_ENV_FILE"
  set +a
fi
[ "$MODE" = stop ] || export AGENT_HIRE_AGENT_SECRET

info() { printf '\033[36m▸\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m✓\033[0m %s\n' "$*"; }
die()  { printf '\033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

stop_all() {
  # Stop a detached supervisor first: it owns the services below, and killing it triggers its own
  # EXIT trap, which is idempotent. `$$` guards the supervisor stopping itself on Ctrl-C.
  if [ -f "$SUPERVISOR_FILE" ]; then
    supervisor_pid=$(cat "$SUPERVISOR_FILE" 2>/dev/null || true)
    rm -f "$SUPERVISOR_FILE"
    if [ -n "$supervisor_pid" ] && [ "$supervisor_pid" != "$$" ] && kill -0 "$supervisor_pid" 2>/dev/null; then
      kill "$supervisor_pid" 2>/dev/null || true
      # The supervisor runs a 1s sleep loop, so its TERM trap fires quickly. Wait for it before
      # falling back to KILL: a half-run trap must not leave the services (or a stale pid) behind.
      for _ in $(seq 1 20); do
        kill -0 "$supervisor_pid" 2>/dev/null || break
        sleep 0.25
      done
      kill -9 "$supervisor_pid" 2>/dev/null || true
    fi
  fi
  for f in "$RUN_DIR"/*.pid; do
    [ -e "$f" ] || continue
    pid=$(cat "$f" 2>/dev/null || true)
    # Negative pid signals the whole group: the API and JavaScript tooling can spawn children, and killing only
    # the parent leaves those holding the port, so the next start fails with "address in use".
    [ -n "$pid" ] && kill -- "-$pid" 2>/dev/null || true
    [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
    rm -f "$f"
  done
  ok "stopped"
}

if [ "$MODE" = stop ]; then stop_all; exit 0; fi

# Refuse to start on top of a running instance rather than producing a confusing port clash.
# Checked BEFORE recording this supervisor, so a refused second start cannot overwrite the pid of
# the instance it is about to complain about.
CHECK_PORTS=("$API_INTERNAL_PORT" "$APP_PORT")
for port in "${CHECK_PORTS[@]}"; do
  if lsof -tiTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    die "port $port is already in use; choose unused ports or stop the matching instance"
  fi
done
if [ -f "$SUPERVISOR_FILE" ] && kill -0 "$(cat "$SUPERVISOR_FILE")" 2>/dev/null; then
  die 'this run directory already has a supervisor; stop that instance or choose another run directory'
fi
trap stop_all EXIT
trap 'exit 0' INT TERM

# Detached runs get their supervisor recorded (see stop_all); foreground runs overwrite it with
# their own pid, which a Ctrl-C trap clears again.
echo $$ > "$SUPERVISOR_FILE"

wait_for() { # url, label, attempts
  local url=$1 label=$2 tries=${3:-90}
  for _ in $(seq 1 "$tries"); do
    curl -sf "$url" >/dev/null 2>&1 && { ok "$label ready"; return 0; }
    sleep 1
  done
  die "$label did not become ready — see $RUN_DIR/"
}

spawn() { # name, command...
  local name=$1; shift
  setsid "$@" > "$RUN_DIR/$name.log" 2>&1 &
  echo $! > "$RUN_DIR/$name.pid"
}

API_BIN=apps/api-rs/target/release/agent-hire-api
command -v cargo >/dev/null 2>&1 || die "Cargo is required. Run scripts/install.sh first."
info "building the API"
(cd apps/api-rs && cargo build --release --target-dir target -p agent-hire-server) >"$RUN_DIR/api-build.log" 2>&1 \
  || die "API build failed — see $RUN_DIR/api-build.log"
ok "API built"
info "starting API on :$API_INTERNAL_PORT (internal)"
spawn api "$API_BIN" --port "$API_INTERNAL_PORT" --no-token-stdout
wait_for "http://127.0.0.1:$API_INTERNAL_PORT/health" "API" 240  # cold starts (e.g. after a power cut) can exceed 90 s

info "building the agent"
(cd apps/agent && npm run build) >"$RUN_DIR/agent-build.log" 2>&1 || die "agent build failed — see $RUN_DIR/agent-build.log"
ok "agent built"

# The API renders the web UI itself; the agent forwards page requests to it.
export AGENT_HIRE_WEB_DIST=""

info "starting Agent-Hire on :$APP_PORT"
spawn agent env -C apps/agent node dist/main.js
wait_for "http://127.0.0.1:$APP_PORT/agent/health" "agent"

URL="http://127.0.0.1:$APP_PORT"

echo
ok "Agent-Hire is up  →  $URL"
echo "   logs: $RUN_DIR/*.log     stop: ./scripts/start.sh --env-file $ENV_FILE --stop  (or Ctrl-C)"
echo
# Hold the terminal so Ctrl-C reaches the trap and stops the services. Without this the script
# exits, the trap fires immediately, and everything shuts down the moment it finished starting.
# The short interval matters for detached runs: bash runs a trapped signal's handler only after
# the foreground command returns, so `sleep 3600` would delay `--stop` by up to an hour.
while true; do sleep 1; done
