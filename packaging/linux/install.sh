#!/usr/bin/env bash
#
# Agent-Hire installer and updater for Linux.
#
# Run it from an unpacked Agent-Hire package. It asks where to install (default ~/Agent-Hire);
# everything the app stores (profile, documents, job index, chats, logs, caches) lives in that
# folder, and the app runs sandboxed to it. Run the same script from a newer package to update:
# it finds the existing install, keeps your data and settings, and replaces only the app.
#
#   ./install.sh                      install, or update the install it finds
#   ./install.sh --dir PATH           install to (or update) PATH
#   ./install.sh --port 8000          the browser port for a new install (default 8000)
#   ./install.sh --models-from DIR    copy the job classifier models from DIR (holds level-model/
#                                     and department-model/) instead of downloading them
#   ./install.sh --no-models          skip the job classifier models (built-in rules are used)
#   ./install.sh --yes                accept every default without asking
#   ./install.sh --no-start           do not start the app at the end
#   ./install.sh --no-desktop         no menu entry and no ~/.local/bin/agent-hire link
set -euo pipefail

PACKAGE_DIR=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
PAYLOAD="$PACKAGE_DIR/payload"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
POINTER="$CONFIG_HOME/agent-hire/install-dir"

TARGET=""
PORT=8000
MODELS_FROM=""
ASSUME_YES=0
DOWNLOAD_MODELS=1
STEP=0
STEPS=6
START=1
DESKTOP=1

info() { printf '  \033[36m▸\033[0m %s\n' "$*"; }
ok() { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*" >&2; }
die() { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

usage() { sed -n '3,18p' "$0" | sed 's/^# \{0,1\}//'; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dir) TARGET="${2:?--dir needs a path}"; shift 2 ;;
    --port) PORT="${2:?--port needs a number}"; shift 2 ;;
    --models-from) MODELS_FROM="${2:?--models-from needs a folder}"; shift 2 ;;
    --no-models) DOWNLOAD_MODELS=0; shift ;;
    --yes | -y) ASSUME_YES=1; shift ;;
    --no-start) START=0; shift ;;
    --no-desktop) DESKTOP=0; shift ;;
    -h | --help) usage; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

# Prompts read from the terminal, so they also work when this script arrives through a pipe
# (`curl ... | bash`). Without a terminal, or with --yes, the default is taken.
ask() { # question, default -> answer on stdout
  local answer=""
  if [ "$ASSUME_YES" = 1 ] || ! { : </dev/tty; } 2>/dev/null; then
    printf '%s\n' "$2"
    return
  fi
  read -r -p "  $1 [$2] " answer </dev/tty || true
  printf '%s\n' "${answer:-$2}"
}

step() { # title
  STEP=$((STEP + 1))
  printf '\n  \033[1m[%s/%s] %s\033[0m\n' "$STEP" "$STEPS" "$1"
}

confirm() { # question, default y|n
  local answer
  answer=$(ask "$1 (y/n)" "$2")
  [[ "$answer" =~ ^[Yy] ]]
}

expand_path() { # ~ and relative paths -> absolute
  local path="$1"
  case "$path" in
    "~") path="$HOME" ;;
    "~/"*) path="$HOME/${path#\~/}" ;;
  esac
  case "$path" in
    /*) ;;
    *) path="$PWD/$path" ;;
  esac
  realpath -m -- "$path"
}

installed_version() { # dir
  cat "$1/app/VERSION" 2>/dev/null || echo "unknown"
}

is_install() { # dir
  [ -f "$1/.agent-hire/install.json" ]
}

# Data an uninstall kept (it removes the app but leaves data/ and .env); installing there reuses it.
has_kept_data() { # dir
  [ -d "$1/data/app" ] && [ -f "$1/.env" ]
}

# ---------------------------------------------------------------------------------------------

preflight() {
  [ -d "$PAYLOAD/app" ] || die "run this from an unpacked Agent-Hire package (payload/ is missing)"
  [ "$(uname -s)" = Linux ] || die "this package is for Linux"
  local arch need have
  arch=$(uname -m)
  [ "$arch" = "$(cat "$PAYLOAD/ARCH")" ] || die "this package is for $(cat "$PAYLOAD/ARCH"), this machine is $arch"
  need=$(cat "$PAYLOAD/GLIBC_MIN")
  have=$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}')
  if [ -n "$have" ] && [ "$(printf '%s\n%s\n' "$need" "$have" | sort -V | head -1)" != "$need" ]; then
    die "this package needs glibc $need or newer; this system has $have (Ubuntu 24.04, Debian 13, Fedora 40 or newer)"
  fi
  [[ "$PORT" =~ ^[1-9][0-9]{0,4}$ ]] && ((PORT < 65535)) || die "--port must be a number between 1 and 65534"
  local missing=()
  for tool in curl tar setsid timeout; do
    command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
  done
  [ "${#missing[@]}" = 0 ] || die "missing tools: ${missing[*]} (install them with your package manager)"
  if ! command -v pdftotext >/dev/null 2>&1; then
    warn "poppler-utils is not installed: reading PDF résumés will be limited."
    warn "  install it with: sudo apt install poppler-utils   (Fedora: sudo dnf install poppler-utils)"
  fi
  if ! command -v bwrap >/dev/null 2>&1; then
    warn "bubblewrap is not installed: the app will keep to its folder by configuration only."
    warn "  for a real sandbox: sudo apt install bubblewrap   (Fedora: sudo dnf install bubblewrap)"
  fi
}

choose_target() {
  if [ -n "$TARGET" ]; then
    TARGET=$(expand_path "$TARGET")
    return
  fi
  local remembered
  remembered=$(cat "$POINTER" 2>/dev/null || true)
  if [ -n "$remembered" ] && is_install "$remembered"; then
    echo "  Agent-Hire $(installed_version "$remembered") is installed in $remembered."
    if confirm "Update it to $(cat "$PAYLOAD/app/VERSION")?" y; then
      TARGET="$remembered"
      return
    fi
  fi
  echo "  Agent-Hire keeps the app and all of its data (profile, documents, job index, chats)"
  echo "  in one folder, and runs sandboxed to it."
  TARGET=$(expand_path "$(ask "Install folder:" "$HOME/Agent-Hire")")
}

check_target() {
  case "$TARGET" in
    / | /usr | /usr/* | /etc | /etc/* | /bin | /sbin | /lib* | /boot | /proc | /sys | /dev | "$HOME")
      die "choose a dedicated folder, not $TARGET" ;;
  esac
  if [ -e "$TARGET" ] && ! is_install "$TARGET" && ! has_kept_data "$TARGET"; then
    [ -d "$TARGET" ] || die "$TARGET exists and is not a folder"
    if [ -n "$(ls -A "$TARGET" 2>/dev/null)" ]; then
      die "$TARGET is not empty and is not an Agent-Hire install; choose an empty or new folder"
    fi
  fi
  mkdir -p "$TARGET"
  [ -w "$TARGET" ] || die "cannot write to $TARGET"
  local free_kb
  free_kb=$(df -Pk "$TARGET" | awk 'NR==2 {print $4}')
  if [ -n "$free_kb" ] && [ "$free_kb" -lt $((4 * 1024 * 1024)) ]; then
    warn "less than 4 GB free in $TARGET; the job index grows to a few GB"
  fi
}

stop_running() {
  if [ -x "$TARGET/agent-hire" ] && "$TARGET/agent-hire" status 2>/dev/null | grep -q running; then
    WAS_RUNNING=1
    info "stopping the running app"
    "$TARGET/agent-hire" stop >/dev/null
  fi
}

# Replace app/ and extension/ by moving a complete copy into place, so a failed copy never leaves
# a half-updated install. The previous app is kept in .agent-hire/previous until the next update.
install_files() {
  local state="$TARGET/.agent-hire"
  mkdir -p "$state"
  rm -rf "${state:?}/staging" "${state:?}/previous"
  mkdir -p "$state/staging" "$state/previous"
  info "copying the app"
  cp -a "$PAYLOAD/app" "$state/staging/app"
  cp -a "$PAYLOAD/extension" "$state/staging/extension"
  for part in app extension; do
    [ ! -e "$TARGET/$part" ] || mv "$TARGET/$part" "$state/previous/$part"
    mv "$state/staging/$part" "$TARGET/$part"
  done
  install -m 755 "$PAYLOAD/agent-hire" "$TARGET/agent-hire"
  for doc in LICENSE NOTICE THIRD_PARTY_NOTICES.md README.txt; do
    [ ! -f "$PAYLOAD/$doc" ] || install -m 644 "$PAYLOAD/$doc" "$TARGET/$doc"
  done
  rmdir "$state/staging"
  mkdir -p "$TARGET"/{data,logs,run,cache}
  chmod 700 "$TARGET/data" "$TARGET/run"
  ok "app files in place"
}

# A new install gets the template; an update keeps the user's file and appends settings that newer
# versions added (commented out, so behaviour does not change).
write_settings() {
  local env_file="$TARGET/.env" template="$PAYLOAD/env.template"
  if [ ! -f "$env_file" ]; then
    local api_port=$((PORT + 10))
    [ "$api_port" -le 65535 ] || api_port=$((PORT - 10))
    sed -e "s/__APP_PORT__/$PORT/" -e "s/__API_PORT__/$api_port/" "$template" >"$env_file"
    chmod 600 "$env_file"
    ok "settings written to $env_file"
    return
  fi
  local added=() name
  while IFS= read -r name; do
    grep -Eq "^#?${name}=" "$env_file" || added+=("$name")
  done < <(grep -Eo '^#?[A-Z_]+=' "$template" | tr -d '#=' | sort -u)
  if [ "${#added[@]}" -gt 0 ]; then
    {
      printf '\n# Added by the update to %s (uncomment to use):\n' "$(cat "$PAYLOAD/app/VERSION")"
      for name in "${added[@]}"; do
        grep -E "^#?${name}=" "$template" | head -1 | sed -e 's/^#*/#/' -e "s/__APP_PORT__/$PORT/" -e "s/__API_PORT__/$((PORT + 10))/"
      done
    } >>"$env_file"
    ok "kept your settings; added ${#added[@]} new option(s) to $env_file"
  else
    ok "kept your settings in $env_file"
  fi
  chmod 600 "$env_file"
}

# Tell the browser extension where this install listens (it otherwise only tries :8000 and :8080).
configure_extension() {
  local port
  port=$(sed -n 's/^APP_PORT=\([0-9]*\).*/\1/p' "$TARGET/.env" | tail -1)
  printf '{"url": "http://127.0.0.1:%s"}\n' "${port:-8000}" >"$TARGET/extension/agent-hire.json"
}

# The careers scraper is Python; uv installs a private Python and its packages under cache/ and data/.
prepare_scraper() {
  info "preparing the careers scraper (downloads Python and its packages once)"
  local log="$TARGET/logs/install-scraper.log"
  if (
    cd "$TARGET/app/scraper"
    export UV_CACHE_DIR="$TARGET/cache/uv" UV_PYTHON_INSTALL_DIR="$TARGET/cache/python"
    export UV_PROJECT_ENVIRONMENT="$TARGET/data/scraper-venv" UV_PYTHON_PREFERENCE=only-managed UV_NO_CONFIG=1
    "$TARGET/app/runtime/bin/uv" sync --frozen --no-dev
  ) >"$log" 2>&1; then
    ok "careers scraper ready"
  else
    warn "the careers scraper could not be prepared (see $log); job scraping will not work until"
    warn "  you run this installer again with a working internet connection"
  fi
}

valid_model() { # dir
  [ -f "$1/labels.json" ] && [ -f "$1/tokenizer.json" ] && compgen -G "$1/*.onnx" >/dev/null
}

# The job level and department classifiers are not part of the package: they are downloaded from
# Hugging Face (or copied from --models-from / an existing install). Without them the app uses its
# built-in rules for job levels and departments.
MODELS_BASE_URL="${AGENT_HIRE_MODELS_BASE_URL:-https://huggingface.co}"
MODEL_REPOS=("level-model qilun-ai/job-level-classifier Experience-level"
  "department-model qilun-ai/job-department-classifier Department")

fetch_model_file() { # url destination
  local auth=()
  [ -z "${HF_TOKEN:-}" ] || auth=(-H "Authorization: Bearer ${HF_TOKEN}")
  curl -fL --retry 3 --progress-bar "${auth[@]}" -o "$2" "$1"
}

download_model() { # folder repo label
  local folder=$1 repo=$2 label=$3 jobs="$TARGET/data/jobs"
  local work="$jobs/$folder.download" base="$MODELS_BASE_URL/$repo/resolve/main/onnx" model_file
  rm -rf "${work:?}"
  mkdir -p "$work"
  fetch_model_file "$base/labels.json" "$work/labels.json" 2>/dev/null || return 1
  model_file=$(sed -n 's/.*"model_file"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$work/labels.json" | head -1)
  model_file=$(basename -- "${model_file:-model.onnx}")
  printf '    %s model\n' "$label"
  fetch_model_file "$base/$model_file" "$work/$model_file" || return 1
  printf '    %s tokenizer\n' "$label"
  fetch_model_file "$base/tokenizer.json" "$work/tokenizer.json" || return 1
  valid_model "$work" || return 1
  rm -rf "${jobs:?}/$folder"
  mv "$work" "$jobs/$folder"
}

copy_models() { # source folder
  local source=$1 jobs="$TARGET/data/jobs" name
  for name in level-model department-model; do
    valid_model "$source/$name" || die "$source/$name is not a classifier model folder"
    rm -rf "${jobs:?}/$name.new"
    cp -rL "$source/$name" "$jobs/$name.new"
    rm -rf "${jobs:?}/$name"
    mv "$jobs/$name.new" "$jobs/$name"
  done
  ok "job classifier models copied from $source"
}

install_models() {
  local jobs="$TARGET/data/jobs" entry folder repo label existing
  mkdir -p "$jobs"
  if valid_model "$jobs/level-model" && valid_model "$jobs/department-model"; then
    ok "job classifier models already installed"
    return
  fi
  if [ -n "$MODELS_FROM" ]; then
    copy_models "$(expand_path "$MODELS_FROM")"
    return
  fi
  existing="${XDG_DATA_HOME:-$HOME/.local/share}/Agent-Hire/jobs"
  if [ "$existing" != "$jobs" ] && valid_model "$existing/level-model" && valid_model "$existing/department-model" \
    && confirm "Copy the job classifier models already on this computer ($existing)?" y; then
    copy_models "$existing"
    return
  fi
  if [ "$DOWNLOAD_MODELS" = 0 ]; then
    info "skipped: job levels and departments use built-in rules (add the models later by re-running the installer)"
    return
  fi
  info "downloading the job classifier models from Hugging Face (about 450 MB)"
  for entry in "${MODEL_REPOS[@]}"; do
    read -r folder repo label <<<"$entry"
    valid_model "$jobs/$folder" && continue
    if ! download_model "$folder" "$repo" "$label"; then
      rm -rf "${jobs:?}/$folder.download"
      warn "could not download the $label model from $MODELS_BASE_URL/$repo"
      warn "  job levels and departments use built-in rules until you re-run the installer"
      return
    fi
  done
  ok "job classifier models installed"
}

# Puts ~/.local/bin (where the agent-hire command is linked) on PATH for new terminals.
ensure_path() {
  case ":$PATH:" in *":$HOME/.local/bin:"*) return 0 ;; esac
  local line='export PATH="$HOME/.local/bin:$PATH"' rc
  for rc in "$HOME/.profile" "$HOME/.bashrc" "$HOME/.zshrc"; do
    [ "$rc" = "$HOME/.profile" ] || [ -f "$rc" ] || continue
    grep -qF '# Added by Agent-Hire' "$rc" 2>/dev/null && continue
    printf '\n# Added by Agent-Hire\n%s\n' "$line" >>"$rc"
  done
  PATH_UPDATED=1
}

desktop_integration() {
  [ "$DESKTOP" = 1 ] || return 0
  local apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
  mkdir -p "$apps" "$HOME/.local/bin"
  cat >"$apps/agent-hire.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Agent-Hire
Comment=Your local job-search agent
Exec="$TARGET/agent-hire" open
Icon=$TARGET/app/share/agent-hire.png
Terminal=false
Categories=Office;Network;
EOF
  local link="$HOME/.local/bin/agent-hire"
  if [ ! -e "$link" ] || [ -L "$link" ]; then
    ln -sfn "$TARGET/agent-hire" "$link"
  fi
  ensure_path
  ok "menu entry and the agent-hire command added"
}

record_install() {
  local state="$TARGET/.agent-hire" now first
  now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  first=$(sed -n 's/.*"installed_at": "\([^"]*\)".*/\1/p' "$state/install.json" 2>/dev/null || true)
  printf '{"version": "%s", "installed_at": "%s", "updated_at": "%s"}\n' \
    "$(cat "$TARGET/app/VERSION")" "${first:-$now}" "$now" >"$state/install.json"
  mkdir -p "$(dirname "$POINTER")"
  printf '%s\n' "$TARGET" >"$POINTER"
}

summary() {
  local port
  port=$(sed -n 's/^APP_PORT=\([0-9]*\).*/\1/p' "$TARGET/.env" | tail -1)
  local command="$TARGET/agent-hire"
  [ "$DESKTOP" = 0 ] || command="agent-hire"
  echo
  ok "Agent-Hire $(cat "$TARGET/app/VERSION") is $MODE_DONE in $TARGET"
  echo
  echo "    open it            $command open$([ "$DESKTOP" = 0 ] || echo '   (or Agent-Hire in your app menu)')"
  echo "    address            http://127.0.0.1:${port:-8000}"
  echo "    browser extension  chrome://extensions → Developer mode → Load unpacked → $TARGET/extension"
  echo "    settings           $TARGET/.env"
  echo "    update             run the same install command again"
  echo "    uninstall          $command uninstall"
  if [ "${PATH_UPDATED:-0}" = 1 ]; then
    echo
    echo "    The agent-hire command works in new terminals (open one, or run: source ~/.profile)."
  fi
  echo
}

main() {
  echo
  echo "  Agent-Hire $(cat "$PAYLOAD/app/VERSION" 2>/dev/null || echo '') installer"
  step "Checking this computer"
  preflight
  ok "ready"
  step "Choosing where to install"
  choose_target
  check_target
  WAS_RUNNING=0
  if is_install "$TARGET"; then
    MODE_DONE="updated"
    info "updating $(installed_version "$TARGET") → $(cat "$PAYLOAD/app/VERSION") in $TARGET (your data stays)"
    stop_running
  elif has_kept_data "$TARGET"; then
    MODE_DONE="installed"
    info "installing to $TARGET and reusing the data kept there"
  else
    MODE_DONE="installed"
    info "installing to $TARGET"
  fi
  step "Installing the app"
  install_files
  write_settings
  configure_extension
  step "Preparing the job scraper"
  prepare_scraper
  step "Job classifier models"
  install_models
  step "Finishing"
  desktop_integration
  record_install
  summary
  if [ "$START" = 1 ] || [ "$WAS_RUNNING" = 1 ]; then
    "$TARGET/agent-hire" start
  fi
}

main
