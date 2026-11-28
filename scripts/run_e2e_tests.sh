#!/usr/bin/env bash
# Run the native HTTP contracts (the web UI's own tests run with `cargo test` in apps/api-rs).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

(
  cd "$ROOT/apps/api-rs"
  cargo build -p agent-hire-server
  CONTRACT_BACKEND=rust CONTRACT_MAX_BACKENDS="${CONTRACT_MAX_BACKENDS:-2}" \
    cargo test -p agent-hire-contract --test contract
)
