#!/usr/bin/env bash
#
# Build an Agent-Hire install package:
#
#   ./scripts/package.sh linux      dist/agent-hire-<version>-linux-<arch>.tar.gz   (./install.sh)
#   ./scripts/package.sh windows    dist/agent-hire-<version>-windows-x86_64.zip    (Install Agent-Hire.cmd)
#
# Options: --no-build packages the existing build output; --no-archive leaves only the unpacked
# package in dist/.
#
# A package is self-contained: the API binary (which also renders the web UI), the agent with a
# private Node.js runtime, the browser extension, the careers scraper with a private uv, and the MiniLM model. Its installer
# installs or updates (packaging/linux/install.sh, packaging/windows/install.ps1).
#
# Needs the developer toolchain (Rust, Node.js, npm) and an internet connection on the first run
# for the Node.js runtime, uv and MiniLM (later runs reuse dist/.cache). The Windows package is
# cross-compiled with cargo-xwin, which downloads Microsoft's CRT and Windows SDK; set
# XWIN_ACCEPT_LICENSE=1 once you accept Microsoft's licence terms for them.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DIST="$ROOT/dist"
CACHE="$DIST/.cache"
NODE_MAJOR=22
UV_VERSION=0.11.25
PLATFORM=""
BUILD=1
ARCHIVE=1

while [ "$#" -gt 0 ]; do
  case "$1" in
    linux | windows) PLATFORM="$1"; shift ;;
    --no-build) BUILD=0; shift ;;
    --no-archive) ARCHIVE=0; shift ;;
    -h | --help) sed -n '3,19p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done
[ -n "$PLATFORM" ] || { echo "usage: $0 linux|windows [--no-build] [--no-archive]" >&2; exit 2; }

info() { printf '\033[36m▸\033[0m %s\n' "$*"; }
ok() { printf '\033[32m✓\033[0m %s\n' "$*"; }
die() { printf '\033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

if [ "$PLATFORM" = linux ]; then
  case "$(uname -m)" in
    x86_64) ARCH=x86_64 NODE_ARCH=x64 UV_TARGET=x86_64-unknown-linux-gnu ;;
    aarch64) ARCH=aarch64 NODE_ARCH=arm64 UV_TARGET=aarch64-unknown-linux-gnu ;;
    *) die "unsupported architecture $(uname -m)" ;;
  esac
  RUST_TARGET=""
  API_BINARY="$ROOT/apps/api-rs/target/release/agent-hire-api"
else
  ARCH=x86_64 NODE_ARCH=x64 UV_TARGET=x86_64-pc-windows-msvc
  RUST_TARGET=x86_64-pc-windows-msvc
  API_BINARY="$ROOT/apps/api-rs/target/$RUST_TARGET/release/agent-hire-api.exe"
fi

BASE_VERSION=$(tr -d '[:space:]' <"$ROOT/packaging/VERSION")
REVISION=$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo local)
VERSION="$BASE_VERSION-$(date -u +%Y%m%d).$REVISION"
NAME="agent-hire-$VERSION-$PLATFORM-$ARCH"
STAGE="$DIST/$NAME"
PAYLOAD="$STAGE/payload"

fetch() { # url, destination
  [ -s "$2" ] && return 0
  mkdir -p "$(dirname "$2")"
  curl -fsSL --retry 3 -o "$2.part" "$1" || die "download failed: $1"
  mv "$2.part" "$2"
}

build() {
  info "building the API (release, $PLATFORM)"
  if [ "$PLATFORM" = linux ]; then
    (cd "$ROOT/apps/api-rs" && cargo build --release -p agent-hire-server) || die "API build failed"
  else
    command -v cargo-xwin >/dev/null 2>&1 || die "install cargo-xwin first: cargo install --locked cargo-xwin"
    (cd "$ROOT/apps/api-rs" && cargo xwin build --release --target "$RUST_TARGET" -p agent-hire-server) ||
      die "Windows API build failed"
  fi
  for app in agent extension; do
    info "building apps/$app"
    if [ ! -d "$ROOT/apps/$app/node_modules" ]; then
      (cd "$ROOT/apps/$app" && npm ci --no-audit --no-fund) || die "npm ci failed in apps/$app"
    fi
    (cd "$ROOT/apps/$app" && npm run build) || die "build failed in apps/$app"
  done
}

latest_node() {
  curl -fsSL https://nodejs.org/dist/index.json |
    python3 -c "import json,sys; print(next(r['version'] for r in json.load(sys.stdin) if r['version'].startswith('v$NODE_MAJOR.')))" ||
    die "cannot look up the latest Node.js $NODE_MAJOR"
}

verify_node() { # version, file
  fetch "https://nodejs.org/dist/$1/SHASUMS256.txt" "$CACHE/node-$1-SHASUMS256.txt"
  (cd "$CACHE" && grep " $2\$" "node-$1-SHASUMS256.txt" | sha256sum -c --status) || die "checksum mismatch for $2"
}

node_runtime() {
  local version folder file
  version=$(latest_node)
  if [ "$PLATFORM" = linux ]; then
    folder="node-$version-linux-$NODE_ARCH" file="$folder.tar.xz"
    fetch "https://nodejs.org/dist/$version/$file" "$CACHE/$file"
    verify_node "$version" "$file"
    tar -xJf "$CACHE/$file" -C "$CACHE" "$folder/bin/node" "$folder/LICENSE"
    mkdir -p "$PAYLOAD/app/runtime/node/bin"
    cp "$CACHE/$folder/bin/node" "$PAYLOAD/app/runtime/node/bin/node"
  else
    folder="node-$version-win-$NODE_ARCH" file="$folder.zip"
    fetch "https://nodejs.org/dist/$version/$file" "$CACHE/$file"
    verify_node "$version" "$file"
    rm -rf "${CACHE:?}/$folder"
    unzip -q -o "$CACHE/$file" "$folder/node.exe" "$folder/LICENSE" -d "$CACHE"
    mkdir -p "$PAYLOAD/app/runtime/node"
    cp "$CACHE/$folder/node.exe" "$PAYLOAD/app/runtime/node/node.exe"
  fi
  cp "$CACHE/$folder/LICENSE" "$PAYLOAD/app/runtime/node/LICENSE"
  ok "Node.js $version"
}

uv_runtime() {
  local base="https://github.com/astral-sh/uv/releases/download/$UV_VERSION" file
  local cached="$CACHE/uv-$UV_VERSION"
  if [ "$PLATFORM" = linux ]; then file="uv-$UV_TARGET.tar.gz"; else file="uv-$UV_TARGET.zip"; fi
  fetch "$base/$file" "$cached/$file"
  fetch "$base/$file.sha256" "$cached/$file.sha256"
  (cd "$cached" && sha256sum -c --status "$file.sha256") || die "checksum mismatch for $file"
  mkdir -p "$PAYLOAD/app/runtime/bin"
  if [ "$PLATFORM" = linux ]; then
    tar -xzf "$cached/$file" -C "$PAYLOAD/app/runtime/bin" --strip-components=1 "uv-$UV_TARGET/uv"
  else
    unzip -q -o "$cached/$file" uv.exe -d "$PAYLOAD/app/runtime/bin"
  fi
  ok "uv $UV_VERSION"
}

minilm_model() {
  local target="$PAYLOAD/app/models/all-MiniLM-L6-v2" cached="$CACHE/all-MiniLM-L6-v2" file
  local base="https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main"
  local local_copy="$ROOT/apps/api-rs/target/contract-cache/all-MiniLM-L6-v2"
  mkdir -p "$cached"
  for file in model.onnx tokenizer.json config.json tokenizer_config.json; do
    if [ ! -s "$cached/$file" ] && [ -s "$local_copy/$file" ]; then
      cp "$local_copy/$file" "$cached/$file"
    fi
  done
  fetch "$base/onnx/model.onnx" "$cached/model.onnx"
  for file in tokenizer.json config.json tokenizer_config.json; do
    fetch "$base/$file" "$cached/$file"
  done
  mkdir -p "$target"
  cp "$cached"/{model.onnx,tokenizer.json,config.json,tokenizer_config.json} "$target/"
  ok "MiniLM model"
}

glibc_floor() { # binaries... -> highest GLIBC_x.y any of them needs
  local binary
  for binary in "$@"; do
    objdump -T "$binary" 2>/dev/null | grep -o 'GLIBC_[0-9.]*' | sed 's/GLIBC_//'
  done | sort -uV | tail -1
}

stage_common() {
  info "assembling $NAME"
  rm -rf "$STAGE"
  [ -f "$API_BINARY" ] || die "missing $API_BINARY (build it, or drop --no-build)"
  mkdir -p "$PAYLOAD/app/bin" "$PAYLOAD/app/share" "$PAYLOAD/app/agent" "$PAYLOAD/app/scraper"
  install -m 755 "$API_BINARY" "$PAYLOAD/app/bin/"
  cp -a "$ROOT/apps/extension/dist" "$PAYLOAD/extension"
  cp -a "$ROOT/apps/agent/dist" "$PAYLOAD/app/agent/dist"
  cp "$ROOT/apps/agent/package.json" "$ROOT/apps/agent/package-lock.json" "$PAYLOAD/app/agent/"
  # Every native add-on the agent uses ships prebuilt binaries for all platforms, so the packages
  # installed here also work on Windows.
  (cd "$PAYLOAD/app/agent" && npm ci --omit=dev --ignore-scripts --no-audit --no-fund >/dev/null) ||
    die "installing the agent's runtime packages failed"
  (cd "$ROOT/services/careers-scraper" &&
    tar --exclude=.venv --exclude=__pycache__ --exclude='*.pyc' --exclude=.pytest_cache --exclude=tests \
      -cf - pyproject.toml uv.lock companies.json companies src) | tar -xf - -C "$PAYLOAD/app/scraper"
  node_runtime
  uv_runtime
  minilm_model
  printf '%s\n' "$VERSION" >"$PAYLOAD/app/VERSION"
  install -m 644 "$ROOT/LICENSE" "$ROOT/NOTICE" "$ROOT/THIRD_PARTY_NOTICES.md" "$PAYLOAD/"
}

stage_linux() {
  local packaging="$ROOT/packaging/linux"
  cp "$ROOT/packaging/icons/agent-hire.png" "$PAYLOAD/app/share/agent-hire.png"
  printf '%s\n' "$ARCH" >"$PAYLOAD/ARCH"
  glibc_floor "$PAYLOAD/app/bin/agent-hire-api" "$PAYLOAD/app/runtime/node/bin/node" >"$PAYLOAD/GLIBC_MIN"
  install -m 755 "$packaging/agent-hire" "$PAYLOAD/agent-hire"
  install -m 644 "$packaging/env.template" "$PAYLOAD/env.template"
  install -m 755 "$packaging/install.sh" "$STAGE/install.sh"
  install -m 644 "$packaging/README.txt" "$STAGE/README.txt"
  install -m 644 "$packaging/README.txt" "$PAYLOAD/README.txt"
  ok "staged $(du -sh "$STAGE" | cut -f1) (glibc $(cat "$PAYLOAD/GLIBC_MIN")+)"
}

# ONNX Runtime's Windows build links DirectML. Windows ships an older DirectML.dll, so the
# redistributable version it was built against goes next to the API (Microsoft's licence allows
# this inside applications built with ML frameworks; the licence is copied alongside).
directml_runtime() {
  local version=1.15.4 file="$CACHE/microsoft.ai.directml.1.15.4.nupkg"
  fetch "https://api.nuget.org/v3-flatcontainer/microsoft.ai.directml/$version/microsoft.ai.directml.$version.nupkg" "$file"
  unzip -q -o -j "$file" bin/x64-win/DirectML.dll -d "$PAYLOAD/app/bin"
  unzip -q -p "$file" LICENSE.txt >"$PAYLOAD/app/bin/DirectML-LICENSE.txt"
  ok "DirectML $version"
}

stage_windows() {
  local packaging="$ROOT/packaging/windows"
  directml_runtime
  cp "$ROOT/packaging/icons/agent-hire.ico" "$PAYLOAD/app/share/agent-hire.ico"
  cp "$packaging/agent-hire.ps1" "$packaging/agent-hire.cmd" "$packaging/env.template" "$PAYLOAD/"
  cp "$packaging/install.ps1" "$packaging/Install Agent-Hire.cmd" "$STAGE/"
  cp "$packaging/README.txt" "$STAGE/README.txt"
  cp "$packaging/README.txt" "$PAYLOAD/README.txt"
  ok "staged $(du -sh "$STAGE" | cut -f1)"
}

archive() {
  local file
  info "compressing"
  if [ "$PLATFORM" = linux ]; then
    file="$NAME.tar.gz"
    tar -C "$DIST" -czf "$DIST/$file" "$NAME"
  else
    file="$NAME.zip"
    rm -f "$DIST/$file"
    (cd "$DIST" && zip -qr -9 "$file" "$NAME")
  fi
  (cd "$DIST" && sha256sum "$file" >"$file.sha256")
  ok "$DIST/$file ($(du -h "$DIST/$file" | cut -f1))"
}

main() {
  mkdir -p "$DIST" "$CACHE"
  [ "$BUILD" = 0 ] || build
  stage_common
  "stage_$PLATFORM"
  [ "$ARCHIVE" = 0 ] || archive
  echo
  if [ "$PLATFORM" = linux ]; then
    echo "Install or update:  tar -xzf $NAME.tar.gz && $NAME/install.sh"
  else
    echo "Install or update on Windows:  unzip $NAME.zip, then double-click 'Install Agent-Hire.cmd'"
  fi
}

main
