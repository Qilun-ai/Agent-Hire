#!/usr/bin/env bash
#
# Agent-Hire one-line installer for Linux:
#
#   curl -fsSL https://raw.githubusercontent.com/Qilun-ai/Agent-Hire/main/install.sh | bash
#
# Downloads the latest Agent-Hire release for this machine, checks its SHA-256, and runs its
# installer, which asks where to install (default ~/Agent-Hire), downloads the job models, puts the
# `agent-hire` command on PATH and starts the app. Running it again updates an existing install.
#
# Options are passed through to the installer, e.g.:
#   curl -fsSL .../install.sh | bash -s -- --yes --dir ~/Apps/Agent-Hire
#
# AGENT_HIRE_PACKAGE_URL=<url of a .tar.gz> installs that package instead (its .sha256 must sit next
# to it); AGENT_HIRE_REPO=<owner/name> reads releases from another repository.
set -euo pipefail

REPO="${AGENT_HIRE_REPO:-Qilun-ai/Agent-Hire}"

say() { printf '  \033[36m▸\033[0m %s\n' "$*"; }
die() { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = Linux ] || die "this installer is for Linux; on Windows run: irm https://raw.githubusercontent.com/$REPO/main/install.ps1 | iex"
for tool in curl tar sha256sum; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is required (install it with your package manager)"
done

case "$(uname -m)" in
  x86_64 | amd64) ARCH=x86_64 ;;
  aarch64 | arm64) ARCH=aarch64 ;;
  *) die "unsupported processor: $(uname -m)" ;;
esac

package_url() {
  if [ -n "${AGENT_HIRE_PACKAGE_URL:-}" ]; then
    printf '%s\n' "$AGENT_HIRE_PACKAGE_URL"
    return
  fi
  local release
  release=$(curl -fsSL -H 'Accept: application/vnd.github+json' "https://api.github.com/repos/$REPO/releases/latest") ||
    die "could not reach GitHub to find the latest release of $REPO"
  printf '%s\n' "$release" | grep -o '"browser_download_url": *"[^"]*"' | sed 's/.*"\(https[^"]*\)"$/\1/' |
    grep -E -- "-linux-$ARCH\.tar\.gz$" | head -1
}

URL=$(package_url)
[ -n "$URL" ] || die "the latest release has no Linux package for $ARCH"
NAME=$(basename "$URL")

WORK=$(mktemp -d "${TMPDIR:-/tmp}/agent-hire-install.XXXXXX")
trap 'rm -rf "${WORK:?}"' EXIT

say "downloading $NAME"
curl -fL --retry 3 --progress-bar -o "$WORK/$NAME" "$URL" || die "download failed: $URL"
curl -fsSL --retry 3 -o "$WORK/$NAME.sha256" "$URL.sha256" || die "could not download the checksum for $NAME"
(cd "$WORK" && sha256sum -c --status "$NAME.sha256") || die "checksum mismatch: the download is damaged, try again"
say "checksum verified"

say "unpacking"
tar -xzf "$WORK/$NAME" -C "$WORK"
INSTALLER=$(find "$WORK" -mindepth 2 -maxdepth 2 -name install.sh -type f | head -1)
[ -n "$INSTALLER" ] || die "the package has no installer"
bash "$INSTALLER" "$@"
