# Developing Agent-Hire

Most people install the packaged app with the one-line installer in the [README](../README.md).
This page is for working on the code.

## Layout

| Path | What it is |
|---|---|
| `apps/api-rs` | The Rust API server (profile, jobs, tailoring, email, extension data) |
| `apps/web` | The web UI (Preact + TypeScript, built with Vite into `apps/web/dist`) |
| `apps/agent` | The agent (Node.js, Pi agent SDK): chat, tools, scheduler; the front door on port 8000, which also serves the web UI |
| `apps/extension` | The fill-only browser extension (MV3) |
| `services/careers-scraper` | The careers-site scraper (Python, run through uv) |
| `packaging/`, `scripts/package.sh` | The Linux and Windows install packages |

## Run from a checkout

Requirements: a stable [Rust](https://rustup.rs/) toolchain, Python 3.13 with
[uv](https://docs.astral.sh/uv/) (only for the scraper), and Node.js 22.19 or newer.

```bash
./scripts/install.sh --start      # installs missing toolchains and dependencies, builds, starts
# or by hand:
make install
make up                           # API on :8010, then the agent (with the web UI) on :8000
```

Open `http://127.0.0.1:8000`. The agent serves the web UI and `/agent/*`, and forwards `/api`, `/ws`
and `/bootstrap` to the API. For UI work, `make web-dev` serves the UI with hot reload on
`http://127.0.0.1:5173` against the app running on :8000. On Windows use `scripts\install.ps1 -Start` and `scripts\start.ps1`.

Nothing checked out yet? This downloads the repository to `~/Agent-Hire` and installs it from source:

```bash
curl -fsSL https://raw.githubusercontent.com/Qilun-ai/Agent-Hire/main/scripts/install.sh | bash -s -- --start
```

Update a checkout install with `./scripts/update.sh`; remove it with `./scripts/uninstall.sh`
(`--purge-data` and `--remove-files` also remove your data and the checkout). Details:
[getting-started.md](getting-started.md).

## Useful commands

```bash
make build           # release API (with the web UI), agent and extension builds
make test            # API (incl. web UI and HTTP contracts), agent and extension tests
make lint            # Clippy for the API
make ext-build       # build the extension into apps/extension/dist
make vendor-careers  # refresh the vendored scraper from a company-careers-scraper checkout
python3 scripts/readme_companies.py   # refresh the company list in the README
```

To try the extension from source, build it with `make ext-build` and load `apps/extension/dist` as
an unpacked extension (chrome://extensions → Developer mode → Load unpacked).

## Packages and releases

```bash
./scripts/package.sh linux      # dist/agent-hire-<version>-linux-x86_64.tar.gz
./scripts/package.sh windows    # dist/agent-hire-<version>-windows-x86_64.zip (cross-compiled)
```

Attach both packages and their `.sha256` files to a GitHub release; the one-line installers
(`install.sh`, `install.ps1` at the repository root) download the latest release. See
[install-package.md](install-package.md) for what the installers do and how to build the Windows
package on Linux.

## Safety defaults

The API listens only on 127.0.0.1, and application actions stay reviewable: AI output is grounded
in your profile and the job data, generated documents are yours to inspect, and the extension fills
fields but never presses Submit.
