# Getting started

This guide is for running Agent-Hire locally from a checkout.

## Requirements

The installer installs these for you when they are missing; they are listed for manual setups:

- A stable [Rust](https://rustup.rs/) toolchain (builds the API server)
- Python 3.13 and [uv](https://docs.astral.sh/uv/) (only for the careers scraper)
- Node.js 22.19 or newer (the agent and the browser extension build)
- A Chromium-based browser if you want to use the fill-only extension

The web UI is part of the Rust API (`apps/api-rs/crates/ui`), so it needs no separate frontend toolchain.

## One-command install

From the repository root:

```bash
./scripts/install.sh          # install everything; start later with ./scripts/start.sh
./scripts/install.sh --start  # install, then start the API (with its web UI) and the agent in the background
```

Nothing checked out yet? The script also works as a one-liner. It downloads the repository to `~/Agent-Hire` (no git required) and then installs and starts:

```bash
curl -fsSL https://raw.githubusercontent.com/Qilun-ai/Agent-Hire/main/scripts/install.sh | bash -s -- --start
```

Set `AGENT_HIRE_DIR` to choose the checkout location and `AGENT_HIRE_REF` to test a branch:

```bash
AGENT_HIRE_DIR=~/code/Agent-Hire AGENT_HIRE_REF=main \
  curl -fsSL https://raw.githubusercontent.com/Qilun-ai/Agent-Hire/main/scripts/install.sh | bash -s -- --start
```

Re-running the one-liner refreshes a bootstrap checkout to the requested ref; existing `.env` files are kept. Set `AGENT_HIRE_NO_UPDATE=1` to leave the checkout untouched, or manage the checkout yourself with git (that is skipped automatically).

The script detects your distribution, checks what is already installed, and only installs what is missing (Rust, uv, Node.js 22 LTS). It then installs every workspace's dependencies, creates local configuration from the committed examples, and builds the web UI and extension. There is no database server or container runtime to install: the scraper service keeps its working state in one SQLite file. It is idempotent: re-running skips what is already installed and never replaces an existing `.env`.

Useful options:

- `--dry-run` — print every action without changing the system
- `--deps-only` — install dependencies only (no builds)
- `--service` — install and start a systemd user service (Linux)
- `--skip-docker` — obsolete and ignored (Agent-Hire no longer uses Docker)
- `--data-dir PATH` — store app data at an absolute PATH instead of the OS default
- `-y` — assume yes for all prompts

Full output is written to `.run/install-*.log`.

In the bash installer, `--start` and `--service` are Linux-only. On macOS the installer still prepares the dependencies and the builds; start the services with `make api` and `make agent` in two terminals.

## Windows (native PowerShell)

Native Windows installs use the PowerShell installer. With no checkout yet, this one-liner downloads the repository to `%USERPROFILE%\Agent-Hire` and then installs and starts:

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/Qilun-ai/Agent-Hire/main/scripts/install.ps1))) -Start
```

From an existing checkout:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1          # install; start later
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Start   # install and start
```

Set `AGENT_HIRE_DIR` to choose the checkout location and `AGENT_HIRE_REF` to test a branch before running the one-liner. Re-running refreshes a bootstrap checkout (existing `.env` files are kept); set `$env:AGENT_HIRE_NO_UPDATE = '1'` to skip the refresh.

It checks what is already installed and only installs what is missing: Git, Node.js LTS, Rust, and uv. It then installs the workspace dependencies, creates the local configuration, and builds the API (with its web UI) and the extension. The same options exist as on Linux/macOS: `-DryRun`, `-DepsOnly`, `-Yes`, and `-Help` (`-SkipDocker` is accepted and ignored). Start and stop the stack later with:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
powershell -ExecutionPolicy Bypass -File scripts\start.ps1 -Stop
```

`start.ps1` writes logs and pid files to `.run\` and serves the app at `http://127.0.0.1:8000` (open the URL in your browser). `-Stop` terminates the Agent-Hire processes.

The `make` commands below are the manual alternative. They require a POSIX shell (WSL qualifies), so native Windows users should prefer the PowerShell scripts.

## Where data lives

The app keeps its local data — profile database, knowledge graph, generated documents, résumés, and the jobs you have saved — in the OS data dir (`~/.local/share/Agent-Hire` on Linux, `~/Library/Application Support/Agent-Hire` on macOS). The scraper service keeps its own working state in one SQLite file, `<data dir>/scraper/scraper.sqlite3` (override with `SCRAPER_DATABASE_PATH`): the runs, every raw job a search collected, and the decision-model cache. Finished runs are pruned after `SCRAPER_RETENTION_DAYS` (default 14). Nothing is stored as vectors.

When the root partition is small, move the app data to a bigger disk either at install time or any time later:

```bash
./scripts/install.sh --data-dir /media/you/big-disk/Agent-Hire
```

or by editing the checkout's `.env` (created from `.env.example`, sourced by `scripts/start.sh` before anything launches):

```bash
AGENT_HIRE_DATA_DIR=/media/you/big-disk/Agent-Hire
```

`AGENT_HIRE_DATA_BASE_DIR` is the variant that moves only the parent and still appends `Agent-Hire/`. The directory is created on first use; existing data is not migrated — copy it yourself if you want to keep it. `--purge-data` on uninstall reads the same `.env` and deletes whichever directory was configured.

## Update

Update an existing installation to the latest source and restart it in the mode it was running. The app is stopped first, the checkout is refreshed, dependencies are re-applied, the UI/extension are rebuilt, and the stack is started again — so it comes back on the new code. Your `.env` configuration and local data are preserved.

```bash
./scripts/update.sh                 # update; restart only if it was already running
./scripts/update.sh --start         # update, then start even if it was stopped
./scripts/update.sh --ref v1.5.0    # update to a specific branch or tag
./scripts/update.sh --dry-run       # print every action without changing anything
```

One-liner (no checkout needed):

```bash
curl -fsSL https://raw.githubusercontent.com/Qilun-ai/Agent-Hire/main/scripts/update.sh | bash
```

`AGENT_HIRE_DIR` targets another installation; `--ref`/`AGENT_HIRE_REF` selects a branch or tag; `AGENT_HIRE_REPO` and `AGENT_HIRE_ARCHIVE_URL` point at a fork or mirror. A git checkout is updated with `git pull` and must have no uncommitted changes; a bootstrap checkout is refreshed from the source archive. Full output is written to `.run/update-*.log`.

On Windows, re-run the PowerShell installer one-liner; it refreshes the checkout, and `-Start` starts the stack again afterwards. Or stop the stack with `scripts\start.ps1 -Stop` before and after updating a git checkout manually.

## Uninstall

Stop Agent-Hire and remove its systemd user service (if installed) and the shell PATH lines the installer added. Postgres/Redis containers and the database volume left by an earlier version are removed too if they are still present. Your profile/settings data and the checkout are kept unless you ask for them:

```bash
./scripts/uninstall.sh                                     # from the checkout
AGENT_HIRE_DIR=~/Agent-Hire ./scripts/uninstall.sh -y      # target a bootstrap checkout
```

Add `--purge-data` to also delete your local profile, settings, and leads, and `--remove-files` to also delete the checkout (git checkouts are refused). Use `--dry-run` to preview. One-liner:

```bash
curl -fsSL https://raw.githubusercontent.com/Qilun-ai/Agent-Hire/main/scripts/uninstall.sh | bash -s -- --remove-files
```

On Windows:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1 [-PurgeData] [-RemoveFiles]
```

Toolchains installed by the installer (Rust, uv, Node.js) are left alone.

## Install and start

From the repository root:

```bash
make install
make up
```

`make up` starts the API (which renders the web UI) and the agent through the repository startup
script. Open `http://127.0.0.1:8000` when it reports that the agent is ready.

### Custom ports and separate instances

Set `APP_PORT` (the agent, which you open in the browser) and `API_INTERNAL_PORT` (the API behind
it) in `.env`. `API_PORT` is an alias for `APP_PORT`; do not give them different values.

Use a separate environment file to run an isolated instance:

```bash
./scripts/start.sh --env-file .env.user-stack --check
./scripts/start.sh --env-file .env.user-stack
./scripts/start.sh --env-file .env.user-stack --stop
```

Ports must be distinct integers in 1–65535. For multiple instances, also choose unique
`AGENT_HIRE_RUN_DIR` and `AGENT_HIRE_DATA_DIR`. Stop terminates only processes recorded in the selected
run directory. Startup never clears collected data.

On Windows the equivalent is `scripts\start.ps1 -EnvFile .env.user-stack` and `-Stop` with the same
file. Credentials remain in your private environment file; native server launches suppress the
desktop token announcement in logs.

Alternatively, run the two services individually (with the same `AGENT_HIRE_AGENT_SECRET` in both):

```bash
make api      # the API and its web UI on API_INTERNAL_PORT (8010)
make agent    # the agent on APP_PORT (8000), forwarding pages and /api to the API
```

Use `make down` to stop services started by `make up`.

## First launch

1. Open Settings and choose an AI provider. See [AI provider configuration](ai-providers.md).
2. Add a preferred role and, optionally, country/city filters on Home.
3. Open **Add experience** and upload a résumé, or enter profile information manually.
4. Assign uploaded documents and profile points to tags such as `SDE` or `ML` when you use multiple career tracks.
5. Configure the decision model and, optionally, an extraction model for the scraper service — see Job discovery.
6. On Home, describe the role and choose **Find new jobs**. Then open **Find jobs** and narrow the results by experience level, skills, location and site.

A search is never narrowed by experience level or skills: every job that matches the role is classified once, and Find jobs filters that stored result — see Job discovery.

## Build and load the extension

```bash
make ext-build
```

Open your browser's extension developer page, enable developer mode, choose **Load unpacked**, and select `apps/extension/dist`. Keep Agent-Hire running while you use the extension.

The extension fills fields on the active career-site page. It does not submit the application.

## Verify a checkout

```bash
make test
make lint
```

`make test` runs the workspace test suites, typechecks, the production build, and the vendor check. All of it runs offline; nothing needs a database server.
