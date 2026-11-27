#!/usr/bin/env bash
#
# Agent-Hire one-command installer.
#
# Installs and deploys everything a local Agent-Hire needs on Linux or macOS:
#
#   1. system packages (curl, git, make, lsof, build tools)
#   2. toolchains: Rust, uv (scraper), Node.js 22 LTS
#   3. workspace dependencies (careers scraper via uv, API via Cargo, extension/agent via npm)
#   4. local configuration (.env files, generated from the committed examples)
#   5. production builds (the API, which also renders the web UI, the agent and the extension)
#   6. optionally starts the stack or installs a systemd user service
#
# There is no database server, cache server or container runtime to install: the scraper service
# keeps its working state in one SQLite file.
#
# Design rules this script follows:
#   • Idempotent — safe to re-run; existing config files are never overwritten.
#   • Non-destructive — it never deletes databases, configs, or build output.
#   • Honest — it fails with the exact command and log line instead of continuing half-installed.
#   • No secrets — nothing is written to the repository; .env files are gitignored and chmod 600.
#
# Usage (from the repository root):
#   ./scripts/install.sh                 install everything, don't start the app
#   ./scripts/install.sh --start         install, then start the agent + API in the background
#   ./scripts/install.sh --service       install a systemd user service running the stack (Linux)
#   ./scripts/install.sh --deps-only     only install dependencies (no builds)
#   ./scripts/install.sh --dry-run       print every action without changing the system
#
# One-liners (no checkout required; the script downloads the repository itself):
#   curl -fsSL https://raw.githubusercontent.com/Qilun-ai/Agent-Hire/main/scripts/install.sh | bash -s -- --start
#   AGENT_HIRE_DIR=~/code/Agent-Hire AGENT_HIRE_REF=main bash scripts/install.sh --start
#
# Run ./scripts/install.sh --help for all options.
set -Eeuo pipefail

# ── paths ─────────────────────────────────────────────────────────────────────
# BASH_SOURCE is unset when this file is piped (curl | bash); an empty REPO_ROOT makes the
# bootstrap below fetch a checkout before anything else runs.
SCRIPT_PATH="${BASH_SOURCE[0]:-}"
SCRIPT_DIR=''
if [ -n "${SCRIPT_PATH}" ] && [ -f "${SCRIPT_PATH}" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
fi
REPO_ROOT=''
if [ -n "${SCRIPT_DIR}" ]; then
  REPO_ROOT="$(cd "${SCRIPT_DIR}/.." 2>/dev/null && pwd || true)"
fi
CAREERS_DIR="${REPO_ROOT}/services/careers-scraper"
AGENT_DIR="${REPO_ROOT}/apps/agent"
API_DIR="${REPO_ROOT}/apps/api-rs"
EXT_DIR="${REPO_ROOT}/apps/extension"
RUN_DIR="${REPO_ROOT}/.run"
LOG_FILE="${RUN_DIR}/install-$(date +%Y%m%d-%H%M%S).log"

# Vite 7 (extension) requires Node ^20.19 || >=22.12.
NODE_MIN_MAJOR_LOW=22
NODE_MIN_MINOR_LOW=19
NODE_MIN_MAJOR_HIGH=22
NODE_MIN_MINOR_HIGH=19
NODE_INSTALL_MAJOR=22

TOTAL_STEPS=5
STEP=0

# ── options ───────────────────────────────────────────────────────────────────
ASSUME_YES=0
START_APP=0
INSTALL_SERVICE=0
DEPS_ONLY=0
DRY_RUN=0
ALLOW_ROOT=0
SHELL_PATH_UPDATED=0
DATA_DIR=''

# shellcheck disable=SC2034
NO_COLOR="${NO_COLOR:-}"

if [ -t 1 ] && [ -z "${NO_COLOR}" ]; then
  C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'
  C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_CYAN=$'\033[36m'
else
  C_RESET=''; C_BOLD=''; C_DIM=''; C_RED=''; C_GREEN=''; C_YELLOW=''; C_CYAN=''
fi

usage() {
  cat <<'EOF'
Agent-Hire installer

Usage: ./scripts/install.sh [options]

Options:
  -y, --yes          assume "yes" for all prompts (non-interactive installs)
      --start        start Agent-Hire in the background after installing (Linux)
      --service      install and start a systemd user service (Linux only)
      --deps-only    only install dependencies; skip the builds
      --data-dir PATH  store app data at PATH instead of the OS default data dir
                      (written to .env as AGENT_HIRE_DATA_DIR; PATH must be absolute)
      --dry-run      print what would happen without changing anything
      --allow-root   permit running as root (not recommended)
  -h, --help         show this help

Environment (used by the curl one-liner):
  AGENT_HIRE_DIR     where to download the checkout (default: ~/Agent-Hire)
  AGENT_HIRE_REF     git ref to download, e.g. a branch (default: main)

Exit status: 0 on success, non-zero on the first failed step.
Full output is mirrored to .run/install-*.log.
EOF
}

parse_args() {
  while (($#)); do
    case "$1" in
      -y|--yes) ASSUME_YES=1 ;;
      --start) START_APP=1 ;;
      --service) INSTALL_SERVICE=1; START_APP=1 ;;
      --deps-only) DEPS_ONLY=1 ;;
      --skip-docker) warn "--skip-docker is obsolete: Agent-Hire no longer uses Docker" ;;
      --data-dir)
        shift
        if (($# == 0)); then
          printf '%s\n' '--data-dir needs a path' >&2
          exit 2
        fi
        DATA_DIR="$1"
        ;;
      --dry-run) DRY_RUN=1 ;;
      --allow-root) ALLOW_ROOT=1 ;;
      -h|--help) usage; exit 0 ;;
      *) printf 'Unknown option: %s (try --help)\n' "$1" >&2; exit 2 ;;
    esac
    shift
  done
  if (( DEPS_ONLY )) && { (( START_APP )) || (( INSTALL_SERVICE )); }; then
    printf '%s\n' '--deps-only cannot be combined with --start/--service' >&2
    exit 2
  fi
  # A relative path would resolve against whatever cwd the launcher happened to have, so only
  # absolute paths are accepted.
  if [ -n "${DATA_DIR}" ] && [ "${DATA_DIR#/}" = "${DATA_DIR}" ]; then
    printf '%s\n' '--data-dir must be an absolute path' >&2
    exit 2
  fi
}

# ── logging ───────────────────────────────────────────────────────────────────
_log() { { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*" >>"${LOG_FILE}"; } 2>/dev/null || true; }
info() { printf '%s\n' "  ${C_CYAN}▸${C_RESET} $*"; _log "INFO  $*"; }
ok()   { printf '%s\n' "  ${C_GREEN}✓${C_RESET} $*"; _log "OK    $*"; }
warn() { printf '%s\n' "  ${C_YELLOW}!${C_RESET} $*" >&2; _log "WARN  $*"; }
die()  { printf '%s\n' "  ${C_RED}✗${C_RESET} $*" >&2; _log "ERROR $*"; exit 1; }

section() {
  STEP=$((STEP + 1))
  printf '\n%s\n' "${C_BOLD}==> [${STEP}/${TOTAL_STEPS}] $*${C_RESET}"
  _log "== $*"
}

on_error() {
  local code=$?
  printf '\n%s\n' "${C_RED}${C_BOLD}Installation failed (exit ${code}).${C_RESET}" >&2
  local i
  for ((i = 1; i < ${#FUNCNAME[@]}; i++)); do
    printf '  at %s:%s (%s)\n' "${BASH_SOURCE[$i]##*/}" "${BASH_LINENO[$i - 1]}" "${FUNCNAME[$i]}" >&2
  done
  printf '%s\n' "  Review the log and re-run; completed steps are skipped automatically." >&2
  printf '%s\n' "  Log: ${LOG_FILE}" >&2
  exit "$code"
}
trap on_error ERR

# Run a command, honoring --dry-run, and tee its output into the log.
run() {
  if (( DRY_RUN )); then
    printf '      %s[dry-run]%s %s\n' "${C_DIM}" "${C_RESET}" "$*"
    _log "DRY   $*"
    return 0
  fi
  _log "RUN   $*"
  "$@" 2>&1 | tee -a "${LOG_FILE}"
}

have() { command -v "$1" >/dev/null 2>&1; }

confirm() {
  local prompt="$1"
  if (( ASSUME_YES )); then return 0; fi
  if [ ! -t 0 ] && [ ! -r /dev/tty ]; then
    die "Cannot prompt for confirmation without a terminal. Re-run with --yes. (${prompt})"
  fi
  local reply=''
  printf '  %s? %s [Y/n] ' "${C_BOLD}" "${prompt}"
  read -r reply < /dev/tty || reply=''
  case "${reply:-y}" in
    [Yy]*) return 0 ;;
    *) return 1 ;;
  esac
}

# ── host detection ────────────────────────────────────────────────────────────
SUDO=()
PKG_MANAGER=''
OS_NAME=''

preflight() {
  if [ "$(id -u)" -eq 0 ] && (( ! ALLOW_ROOT )); then
    die "Run this script as a normal user, not root (it uses sudo internally). Override with --allow-root."
  fi
  if [ ! -f "${REPO_ROOT}/Makefile" ] || [ ! -d "${CAREERS_DIR}" ]; then
    die "This does not look like the Agent-Hire repository (expected Makefile + services/careers-scraper)."
  fi
  OS_NAME="$(uname -s)"
  case "${OS_NAME}" in
    Linux|Darwin) ;;
    *) die "Unsupported operating system: ${OS_NAME}. Linux or macOS is required." ;;
  esac
  if [ "$(id -u)" -ne 0 ]; then
    have sudo || die "sudo is required to install system packages."
    SUDO=(sudo)
  fi

  if (( ! DRY_RUN )); then
    mkdir -p "${RUN_DIR}"
    : >"${LOG_FILE}"
  fi
  _log "Agent-Hire installer starting (dry-run=${DRY_RUN}, os=${OS_NAME})"

  local free_kb
  free_kb="$(df -Pk "${REPO_ROOT}" | awk 'NR==2 {print $4}')"
  if [ -n "${free_kb}" ] && [ "${free_kb}" -lt 12582912 ]; then
    warn "Less than 12 GB free on this filesystem (${free_kb} KB). Dependencies, models, and the corpus may not fit."
  fi
}

detect_pkg_manager() {
  if have apt-get; then PKG_MANAGER=apt
  elif have dnf; then PKG_MANAGER=dnf
  elif have pacman; then PKG_MANAGER=pacman
  elif have zypper; then PKG_MANAGER=zypper
  elif have brew; then PKG_MANAGER=brew
  else PKG_MANAGER=unknown
  fi
  _log "package manager: ${PKG_MANAGER}"
}

# ── 1. system packages ────────────────────────────────────────────────────────
install_base_packages() {
  section "System packages"
  local need=0 cmd
  for cmd in curl git make lsof unzip xz tar; do
    have "${cmd}" || need=1
  done
  have cc || have gcc || have clang || need=1

  if [ "${PKG_MANAGER}" = brew ]; then
    brew_missing=()
    for cmd in curl git make; do have "${cmd}" || brew_missing+=("${cmd}"); done
    if [ "${#brew_missing[@]}" -eq 0 ]; then
      ok "base tools already present"
      return 0
    fi
    warn "missing: ${brew_missing[*]}"
    run brew install "${brew_missing[@]}"
    ok "base tools installed"
    return 0
  fi

  if (( ! need )); then
    ok "base tools already present"
    return 0
  fi
  warn "Some base tools are missing."
  confirm "Install the missing system packages with ${PKG_MANAGER}?" || die "Required tools are missing."
  case "${PKG_MANAGER}" in
    apt)
      run "${SUDO[@]}" apt-get update
      run "${SUDO[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        curl git make lsof unzip xz-utils ca-certificates build-essential
      ;;
    dnf)
      run "${SUDO[@]}" dnf install -y curl git make lsof unzip xz ca-certificates gcc gcc-c++
      ;;
    pacman)
      run "${SUDO[@]}" pacman -Sy --noconfirm curl git make lsof unzip xz ca-certificates base-devel
      ;;
    zypper)
      run "${SUDO[@]}" zypper --non-interactive install curl git make lsof unzip xz ca-certificates gcc gcc-c++
      ;;
    *)
      die "No supported package manager found; install curl, git, make, lsof, unzip, and a C compiler manually."
      ;;
  esac
  ok "base tools installed"
}

# ── 2. toolchains ─────────────────────────────────────────────────────────────
ensure_shell_path() {
  # Persist the toolchain locations for future shells. Only touches files that already exist.
  # shellcheck disable=SC2016  # $HOME must stay literal: this line is written into rc files.
  local line='export PATH="$HOME/.local/bin:$HOME/.bun/bin:$HOME/.cargo/bin:$PATH"'
  local rc
  for rc in "${HOME}/.bashrc" "${HOME}/.zshrc" "${HOME}/.profile"; do
    [ -f "${rc}" ] || continue
    grep -qF '.bun/bin' "${rc}" 2>/dev/null &&
      grep -qF '.cargo/bin' "${rc}" 2>/dev/null && continue
    if (( DRY_RUN )); then
      info "[dry-run] would add the toolchain PATH to ${rc}"
    else
      printf '\n# Agent-Hire toolchain (added by scripts/install.sh)\n%s\n' "${line}" >>"${rc}"
      SHELL_PATH_UPDATED=1
    fi
  done
}

ensure_uv() {
  export PATH="${HOME}/.local/bin:${PATH}"
  if have uv; then
    ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"
    return 0
  fi
  info "Installing uv for the careers scraper..."
  if (( DRY_RUN )); then
    info "[dry-run] would install uv from https://astral.sh/uv"
    ok "uv would be installed"
    return 0
  fi
  run bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh' \
    || die "uv installation failed. Install it manually from https://docs.astral.sh/uv/"
  export PATH="${HOME}/.local/bin:${PATH}"
  have uv || die "uv was installed but is not on PATH (expected ${HOME}/.local/bin/uv)."
  ok "uv installed"
}

node_version_ok() {
  have node || return 1
  local v major minor
  v="$(node -v 2>/dev/null)" || return 1
  v="${v#v}"
  major="${v%%.*}"
  minor="${v#*.}"; minor="${minor%%.*}"
  # Unquoted on purpose: bash 3.2 (macOS default) treats a quoted left side as a literal.
  [[ ${major} =~ ^[0-9]+$ ]] || return 1
  [[ ${minor} =~ ^[0-9]+$ ]] || minor=0
  if (( major > NODE_MIN_MAJOR_HIGH )); then return 0; fi
  if (( major == NODE_MIN_MAJOR_LOW )) && (( minor >= NODE_MIN_MINOR_LOW )); then return 0; fi
  if (( major == NODE_MIN_MAJOR_HIGH )) && (( minor >= NODE_MIN_MINOR_HIGH )); then return 0; fi
  return 1
}

ensure_node() {
  export PATH="${HOME}/.local/bin:${PATH}"
  if node_version_ok; then
    ok "Node.js $(node -v)"
    return 0
  fi
  have node && warn "Node.js $(node -v) is older than required (need 22.19+, required by the Pi agent); installing Node ${NODE_INSTALL_MAJOR} LTS."
  if (( DRY_RUN )); then
    info "[dry-run] would install Node.js ${NODE_INSTALL_MAJOR} LTS (via brew on macOS, official tarball elsewhere)"
    ok "Node.js would be installed"
    return 0
  fi

  if [ "${PKG_MANAGER}" = brew ] && have brew; then
    run brew install "node@${NODE_INSTALL_MAJOR}" || die "brew install node@${NODE_INSTALL_MAJOR} failed."
    run brew link --overwrite --force "node@${NODE_INSTALL_MAJOR}" || true
    node_version_ok || die "Node installation did not produce a usable 'node'."
    ok "Node.js $(node -v)"
    return 0
  fi

  local os arch name ver url tmp dest
  case "${OS_NAME}" in
    Linux) os=linux ;;
    Darwin) os=darwin ;;
  esac
  case "$(uname -m)" in
    x86_64|amd64) arch=x64 ;;
    aarch64|arm64) arch=arm64 ;;
    *) die "Unsupported architecture for the Node.js tarball: $(uname -m)" ;;
  esac
  info "Resolving the latest Node ${NODE_INSTALL_MAJOR}.x release..."
  name="$(curl -fsSL "https://nodejs.org/dist/latest-v${NODE_INSTALL_MAJOR}.x/" 2>/dev/null \
    | grep -oE "node-v${NODE_INSTALL_MAJOR}[0-9.]+-${os}-${arch}\.tar\.xz" | head -n1 || true)"
  [ -n "${name}" ] || die "Could not resolve a Node.js ${NODE_INSTALL_MAJOR}.x release for ${os}-${arch} (is nodejs.org reachable?)."
  ver="${name#node-}"; ver="${ver%-"${os}"-"${arch}".tar.xz}"
  dest="${HOME}/.local/opt/node-${ver}-${os}-${arch}"
  url="https://nodejs.org/dist/latest-v${NODE_INSTALL_MAJOR}.x/${name}"

  tmp="$(mktemp -d "${TMPDIR:-/tmp}/agent-hire-node.XXXXXX")"
  info "Downloading ${name}..."
  curl -fsSL "${url}" -o "${tmp}/${name}" || die "Node.js download failed."
  mkdir -p "${HOME}/.local/opt" "${HOME}/.local/bin"
  rm -rf "${dest}"
  tar -xJf "${tmp}/${name}" -C "${HOME}/.local/opt" || die "Could not extract the Node.js archive."
  rm -rf "${tmp}"
  ln -sf "${dest}/bin/node" "${HOME}/.local/bin/node"
  ln -sf "${dest}/bin/npm" "${HOME}/.local/bin/npm"
  ln -sf "${dest}/bin/npx" "${HOME}/.local/bin/npx"
  export PATH="${HOME}/.local/bin:${PATH}"
  node_version_ok || die "Node.js installation failed; expected ${HOME}/.local/bin/node."
  ok "Node.js $(node -v)"
}

# ── 3b. toolchain bundle ──────────────────────────────────────────────────────
ensure_rust() {
  export PATH="${HOME}/.cargo/bin:${PATH}"
  if ! have cargo || ! have rustc; then
    info "Installing Rust with rustup..."
    if (( DRY_RUN )); then
      info "[dry-run] would install Rust from https://sh.rustup.rs"
      return
    fi
    curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal
  fi
  have cargo && have rustc || die "Rust installation failed."
  ok "$(rustc --version)"
}

ensure_toolchains() {
  section "Toolchains (Rust, uv, Node.js)"
  ensure_rust
  ensure_uv
  ensure_node
  ensure_shell_path
  ok "toolchains ready"
}

# ── 3. workspace dependencies ─────────────────────────────────────────────────
run_uv_sync() {
  local dir=$1; shift
  ( cd "${dir}" && run uv sync "$@" )
}

run_node_install() {
  local dir=$1
  # Lockfiles are committed: the installer must not silently rewrite them with unpinned versions.
  # A failure here means package.json and its lockfile disagree, which the user must resolve.
  if [ -f "${dir}/package-lock.json" ]; then
    ( cd "${dir}" && run npm ci ) \
      || die "npm ci failed in ${dir}. Run 'npm install' there to refresh the lockfile, then re-run."
  else
    die "No lockfile found in ${dir}; refusing an unpinned install."
  fi
}

install_workspace_deps() {
  section "Workspace dependencies"
  info "careers scraper Python environment (services/careers-scraper)..."
  run_uv_sync "${CAREERS_DIR}"
  info "Rust API dependencies (apps/api-rs)..."
  ( cd "${API_DIR}" && run cargo fetch --locked )
  info "browser extension dependencies (apps/extension)..."
  run_node_install "${EXT_DIR}"
  info "agent dependencies (apps/agent)..."
  run_node_install "${AGENT_DIR}"
  ok "all workspace dependencies installed"
}

# ── 4. configuration ──────────────────────────────────────────────────────────
prepare_env() {
  section "Configuration"
  run mkdir -p "${RUN_DIR}"

  # The checkout-root .env is the stack-wide config file: start.sh sources it before spawning
  # the services, which is what makes AGENT_HIRE_DATA_DIR reach the API.
  local root_env="${REPO_ROOT}/.env"
  if [ -f "${root_env}" ]; then
    info "Keeping the existing .env"
  else
    run cp "${REPO_ROOT}/.env.example" "${root_env}"
    (( DRY_RUN )) || ok "Created .env from the committed example"
  fi
  if [ -f "${root_env}" ] && (( ! DRY_RUN )); then chmod 600 "${root_env}" 2>/dev/null || true; fi

  if [ -n "${DATA_DIR}" ]; then
    if (( DRY_RUN )); then
      info "[dry-run] would set AGENT_HIRE_DATA_DIR=${DATA_DIR} in .env and create it"
    else
      if grep -qE '^#?AGENT_HIRE_DATA_DIR=' "${root_env}" 2>/dev/null; then
        sed -i -E "s|^#?AGENT_HIRE_DATA_DIR=.*|AGENT_HIRE_DATA_DIR=\"${DATA_DIR}\"|" "${root_env}"
      else
        printf 'AGENT_HIRE_DATA_DIR="%s"\n' "${DATA_DIR}" >>"${root_env}"
      fi
      mkdir -p "${DATA_DIR}"
      ok "App data directory set to ${DATA_DIR} (AGENT_HIRE_DATA_DIR in .env)"
    fi
  fi

  ok "configuration ready (no secrets are written to the repository)"
}

# ── 5. builds ─────────────────────────────────────────────────────────────────
build_artifacts() {
  section "Production builds"
  info "Building the Rust API (with the web UI), the agent and the browser extension..."
  ( cd "${REPO_ROOT}" && run make build )
  ok "builds complete"
}

# ── 6. run / service ──────────────────────────────────────────────────────────
wait_http() { # url label tries
  local url=$1 label=$2 tries=${3:-120}
  (( DRY_RUN )) && return 0
  for _ in $(seq 1 "${tries}"); do
    if curl -fsS "${url}" >/dev/null 2>&1; then
      ok "${label} is answering on ${url}"
      return 0
    fi
    sleep 1
  done
  die "${label} did not become healthy; see ${RUN_DIR}/*.log"
}

# Detach so the supervisor survives this shell. setsid exists on Linux but not macOS.
launch_detached() {
  if have setsid; then
    setsid nohup "$@" >"${RUN_DIR}/start.log" 2>&1 </dev/null &
  else
    nohup "$@" >"${RUN_DIR}/start.log" 2>&1 </dev/null &
  fi
}

detach_start() {
  # Run through bash explicitly: a checkout from an archive may not carry the executable bit.
  launch_detached bash "${REPO_ROOT}/scripts/start.sh" "$@"
}

start_stack() {
  section "Starting Agent-Hire"
  if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then
    ok "Agent-Hire is already running on http://127.0.0.1:8000"
    return 0
  fi
  if [ "${OS_NAME}" = Darwin ]; then
    # scripts/start.sh uses setsid and GNU `env -C`, which stock macOS does not provide. The
    # installer can prepare everything, but the two services must be started manually there.
    warn "Automatic start is supported on Linux only."
    printf '%s\n' "  Start the services in two terminals instead:"
    printf '%s\n' "    APP_PORT=8010 make api            # internal API on :8010"
    printf '%s\n' "    cd apps/agent && npm run build && AGENT_HIRE_AGENT_SECRET=<same-random-value-for-both> node dist/main.js   # app on :8000"
    return 0
  fi
  info "Launching scripts/start.sh in the background (logs in ${RUN_DIR}/)..."
  if (( DRY_RUN )); then
    info "[dry-run] would run scripts/start.sh"
    return 0
  fi
  detach_start
  sleep 1
  wait_http http://127.0.0.1:8000/health "Agent-Hire API"
}

install_systemd_service() {
  section "systemd user service"
  [ "${OS_NAME}" = Linux ] || die "--service is only supported on Linux; use --start on macOS."
  have systemctl || die "--service requires systemd; use --start instead."
  if ! systemctl --user show-environment >/dev/null 2>&1; then
    die "The systemd user instance is not available; use --start instead."
  fi

  local unit_dir="${HOME}/.config/systemd/user"
  local unit="${unit_dir}/agent-hire.service"
  local node_bin bun_bin uv_bin path_line
  node_bin="$(dirname "$(command -v npm 2>/dev/null || command -v node 2>/dev/null || echo "${HOME}/.local/bin/node")")"
  bun_bin="$(dirname "$(command -v bun 2>/dev/null || echo "${HOME}/.bun/bin/bun")")"
  uv_bin="$(dirname "$(command -v uv 2>/dev/null || echo "${HOME}/.local/bin/uv")")"
  path_line="${node_bin}:${bun_bin}:${uv_bin}:$(dirname "$(command -v cargo)"):${HOME}/.local/bin:${HOME}/.bun/bin:/usr/local/bin:/usr/bin:/bin"

  if [ -f "${RUN_DIR}/supervisor" ] || compgen -G "${RUN_DIR}/*.pid" >/dev/null; then
    info "Stopping the manually started instance before handing over to systemd..."
    run bash "${REPO_ROOT}/scripts/start.sh" --stop || true
    sleep 2
  fi

  run mkdir -p "${unit_dir}"
  if (( DRY_RUN )); then
    info "[dry-run] would write ${unit} and enable it"
    return 0
  fi
  if [ -f "${unit}" ]; then
    cp "${unit}" "${unit}.bak-$(date +%Y%m%d-%H%M%S)"
  fi
  cat >"${unit}" <<EOF
[Unit]
Description=Agent-Hire local application (API with its web UI, and the agent)
Documentation=file://${REPO_ROOT}/docs/getting-started.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory="${REPO_ROOT}"
Environment=PATH=${path_line}
Environment=HOME=${HOME}
ExecStart=/bin/bash "${REPO_ROOT}/scripts/start.sh"
Restart=on-failure
RestartSec=10
TimeoutStopSec=30
KillMode=mixed

[Install]
WantedBy=default.target
EOF
  chmod 644 "${unit}"
  run systemctl --user daemon-reload
  run systemctl --user enable agent-hire.service
  run systemctl --user restart agent-hire.service

  if ((${#SUDO[@]})); then
    run "${SUDO[@]}" loginctl enable-linger "$(id -un)" \
      || warn "Could not enable lingering; the service will stop when you log out."
  fi
  wait_http http://127.0.0.1:8000/health "Agent-Hire API"
  ok "systemd user service installed: systemctl --user status agent-hire"
}

print_summary() {
  if (( DRY_RUN )); then
    printf '\n%s\n' "${C_BOLD}Dry run complete — no changes were made.${C_RESET}"
    printf '%s\n' "  Re-run without --dry-run to perform the install (add --start to launch afterwards)."
    return 0
  fi
  printf '\n%s\n' "${C_GREEN}${C_BOLD}Agent-Hire is installed.${C_RESET}"
  printf '%s\n' "  Web + API   http://127.0.0.1:8000"
  printf '%s\n' "  Logs        ${RUN_DIR}/*.log"
  printf '\n%s\n' "  Start       ./scripts/start.sh"
  printf '%s\n' "  Update      ./scripts/update.sh"
  printf '%s\n' "  Stop        ./scripts/start.sh --stop"
  printf '%s\n' "  Docs        docs/getting-started.md"
  if (( INSTALL_SERVICE )); then
    printf '%s\n' "  Service     systemctl --user status agent-hire   (journalctl --user -u agent-hire -f)"
  fi
  if (( START_APP )) && (( ! INSTALL_SERVICE )); then
    printf '%s\n' "  Started in the background; stop with ./scripts/start.sh --stop"
  fi
  if (( ! START_APP )); then
    printf '\n%s\n' "  Start it when ready:  ./scripts/start.sh"
  fi
  printf '\n%s\n' "  Next: open the app, go to Settings, and configure an AI provider (docs/ai-providers.md)."
  if (( ! INSTALL_SERVICE )) && (( ! START_APP )) && (( SHELL_PATH_UPDATED )); then
    printf '\n%s\n' "  Open a new terminal (or source your shell profile) before running './scripts/start.sh',"
    printf '%s\n' "  so the just-installed toolchains are on PATH."
  fi
  printf '\n%s\n' "  Full install log: ${LOG_FILE}"
}

set_total_steps() {
  TOTAL_STEPS=5
  if (( INSTALL_SERVICE || START_APP )); then
    TOTAL_STEPS=6
  fi
  if (( DEPS_ONLY )); then
    TOTAL_STEPS=4
  fi
}

# ── bootstrap (curl | bash) ───────────────────────────────────────────────────
# Running from a pipe or from a copy of this file outside a checkout has no repository to install
# from. Download one first (no git required), then hand over to the real installer inside it.
download_checkout() { # archive_url, target
  local archive_url=$1 target=$2
  have curl || have wget || die "curl or wget is required to download Agent-Hire."
  local tmp base
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/agent-hire-bootstrap.XXXXXX")"
  if have curl; then
    curl -fsSL "${archive_url}" | tar -xz -C "${tmp}" || die "Could not download ${archive_url}"
  else
    wget -qO- "${archive_url}" | tar -xz -C "${tmp}" || die "Could not download ${archive_url}"
  fi
  base="$(find "${tmp}" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
  [ -n "${base}" ] || die "The downloaded archive did not contain a repository directory."
  mkdir -p "${target}"
  # Overlay the code: .env files and .run state are not part of the archive, so they survive.
  cp -Rp "${base}/." "${target}/"
  # Archive downloads can lose the executable bit; make the entry points runnable regardless.
  chmod +x "${target}"/scripts/*.sh 2>/dev/null || true
  rm -rf "${tmp}"
}

bootstrap_checkout() {
  local ref="${AGENT_HIRE_REF:-main}"
  local target="${AGENT_HIRE_DIR:-${HOME}/Agent-Hire}"
  local archive_url="https://codeload.github.com/Qilun-ai/Agent-Hire/tar.gz/refs/heads/${ref}"

  if [ -f "${target}/Makefile" ] && [ -d "${target}/services/careers-scraper" ]; then
    if [ -d "${target}/.git" ]; then
      info "Using the existing git checkout at ${target} (update it with git pull)"
    elif [ -n "${AGENT_HIRE_NO_UPDATE:-}" ]; then
      info "Using the existing checkout at ${target}"
    else
      info "Refreshing the existing checkout at ${target} (${ref})..."
      download_checkout "${archive_url}" "${target}"
      info "Checkout updated at ${target}"
    fi
  else
    if [ -e "${target}" ] && [ -n "$(find "${target}" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
      die "${target} exists but is not a Agent-Hire checkout. Set AGENT_HIRE_DIR to another path."
    fi
    info "Downloading Agent-Hire (${ref}) to ${target}..."
    download_checkout "${archive_url}" "${target}"
    info "Checkout ready at ${target}"
  fi

  # Re-run the real installer from the checkout with the original arguments.
  exec bash "${target}/scripts/install.sh" "$@"
}

main() {
  parse_args "$@"
  set_total_steps
  preflight
  detect_pkg_manager

  install_base_packages
  ensure_toolchains
  install_workspace_deps
  prepare_env

  if (( DEPS_ONLY )); then
    printf '\n%s\n' "${C_BOLD}==> Dependencies only — finished${C_RESET}"
    ok "Dependencies are installed. Re-run without --deps-only to run the builds."
    printf '%s\n' "  Log: ${LOG_FILE}"
    exit 0
  fi

  build_artifacts

  if (( INSTALL_SERVICE )); then
    install_systemd_service
  elif (( START_APP )); then
    start_stack
  fi

  print_summary
}

# A normal checkout skips the bootstrap and runs the installer directly.
if [ -z "${REPO_ROOT}" ] || [ ! -f "${REPO_ROOT}/Makefile" ] || [ ! -d "${REPO_ROOT}/services/careers-scraper" ]; then
  bootstrap_checkout "$@"
fi

main "$@"

