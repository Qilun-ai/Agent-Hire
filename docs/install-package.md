# Installing Agent-Hire

## One line

```bash
curl -fsSL https://raw.githubusercontent.com/Qilun-ai/Agent-Hire/main/install.sh | bash        # Linux
```

```powershell
irm https://raw.githubusercontent.com/Qilun-ai/Agent-Hire/main/install.ps1 | iex                # Windows
```

The command downloads the latest release for your system from GitHub, checks its SHA-256, and
runs its installer, which shows numbered steps and progress bars:

1. checks the computer (64-bit, glibc on Linux, optional Poppler and bubblewrap),
2. asks where to install (default `~/Agent-Hire`; an existing install is found and updated),
3. installs the app, keeping your data and settings on updates,
4. prepares the careers scraper (a private Python under the install folder),
5. downloads the two job classifier models from Hugging Face (about 450 MB),
6. adds the `agent-hire` command to your PATH and a menu entry, then starts the app.

Pass options after `bash -s --` (Linux) or set them when running the downloaded `install.ps1`
(Windows), for example `curl -fsSL …/install.sh | bash -s -- --yes --dir ~/Apps/Agent-Hire`.

The package carries everything it needs (the API server with its web UI, the agent with its own
Node.js, the browser extension, the careers scraper with its own uv, and the MiniLM model), so the
computer needs no developer tools.

## Building the packages

From a checkout with the developer toolchain (see [getting-started.md](getting-started.md)):

```bash
./scripts/package.sh linux      # dist/agent-hire-<version>-linux-<arch>.tar.gz
./scripts/package.sh windows    # dist/agent-hire-<version>-windows-x86_64.zip
```

`--no-build` reuses existing build output and `--no-archive` skips the archive. The first run
downloads the Node.js runtime, uv and MiniLM into `dist/.cache` and verifies their checksums.

The Windows package is cross-compiled on Linux with
[cargo-xwin](https://github.com/rust-cross/cargo-xwin) (`cargo install --locked cargo-xwin`,
`rustup target add x86_64-pc-windows-msvc`). It needs `clang-cl`, `llvm-lib` and `lld-link`
on `PATH` (`rust-lld` works as `lld-link`). cargo-xwin downloads Microsoft's CRT and Windows SDK,
so set `XWIN_ACCEPT_LICENSE=1` only after accepting Microsoft's licence terms for them.

The Linux binary needs the glibc of the machine that built it or newer. The package records that
minimum (`payload/GLIBC_MIN`), and the installer refuses older systems.

## Installing and updating

| | Linux | Windows |
|---|---|---|
| Install or update | `tar -xzf agent-hire-…tar.gz && agent-hire-…/install.sh` | unzip, double-click `Install Agent-Hire.cmd` |
| Default folder | `~/Agent-Hire` | `%USERPROFILE%\Agent-Hire` |
| Open | `<folder>/agent-hire open`, app menu, or `agent-hire` on `PATH` | Start menu → Agent-Hire, or `agent-hire.cmd open` |
| Uninstall | `<folder>/agent-hire uninstall` | `<folder>\agent-hire.cmd uninstall` |

The installer asks for a folder. If a previous install is recorded (Linux:
`~/.config/agent-hire/install-dir`, Windows: `%APPDATA%\Agent-Hire\install-dir`), it offers to
update that one. An update:

1. stops the running app,
2. copies the new app next to the old one, then swaps `app/` and `extension/` into place (the
   previous version stays in `.agent-hire/previous` until the next update),
3. keeps `data/`, `logs/` and `.env`, appending any settings newer versions added (commented out),
4. syncs the scraper's Python packages, and starts the app again if it was running.

Uninstalling asks whether to keep your data. Installing again into a folder with kept data reuses it.

Installer options (Linux / Windows): `--dir` / `-Dir`, `--port` / `-Port` (new installs, default
8000), `--models-from` / `-ModelsFrom`, `--yes` / `-Yes`, `--no-start` / `-NoStart`, and
`--no-desktop` / `-NoShortcut`.

## The install folder

```
Agent-Hire/
  agent-hire (.cmd/.ps1)   launcher: start, stop, restart, status, open, logs, uninstall
  .env                     your settings (ports, decision model, optional keys)
  app/                     the program (replaced on update)
  extension/               the unpacked browser extension (replaced in place on update)
  data/                    profile, documents, job index, chats, scraper environment
  logs/  run/  cache/      logs, pid files and temp, downloaded Python and package caches
```

The launcher points every path the app uses into this folder: the API's data and job-index
directories, the agent's chats and uploads, the scraper's Python and packages, and also `HOME`,
the XDG folders and `TMPDIR` (on Windows `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `TEMP`), so
libraries cannot write elsewhere.

On Linux the app also runs inside [bubblewrap](https://github.com/containers/bubblewrap) when it
is installed: the system is read-only, `/home`, `/media` and `/mnt` are empty except for the
install folder, and `/tmp` is private. Set `AGENT_HIRE_SANDBOX=required` in `.env` to refuse to
start without it, or `off` to disable it. Windows has no equivalent here; confinement there comes
from the redirected paths only. Subscription-CLI providers (Claude, Codex, Gemini CLIs) read logins
from your real home folder, so they do not work in a sandboxed install.

## AI keys and the decision model

Keys are looked up in this order: a key saved in the app's Settings, then environment variables.
For each provider, the conventional name and common short forms are accepted, for example
`OPENAI_API_KEY`, `OPENAI_API`, `OPENAI_KEY`, `OPENAI_API_KEY_1` and `OPENAI_API_1` (Gemini also
accepts `GOOGLE_API_KEY`; Hugging Face uses `HF_TOKEN`).

Desktop launchers do not read `~/.bashrc`, so on Linux the launcher asks your login shell for
exactly these variable names when they are not already set. On Windows it reads your stored user
and machine environment variables. Values stay in memory and are never written to disk. Set
`AGENT_HIRE_IMPORT_SHELL_KEYS=0` in `.env` to turn the shell lookup off.

On first start, if no provider has been chosen in the app, the first provider with a key is
selected. Settings → AI model shows which keys come from the environment.

OpenCode keys go to the OpenCode Go endpoint by default; for OpenCode Zen set
`OPENCODE_BASE_URL=https://opencode.ai/zen/v1`.

The decision model is set in the app (Settings → Job agent) or in `.env`:

```
AGENT_HIRE_DECISION_API_URL=https://…/v1
AGENT_HIRE_DECISION_API_KEY=…
AGENT_HIRE_DECISION_API_MODEL=…
```

The app's values win when both are set.

## Job classifier models

The job level and department classifiers are licensed separately and are not part of the package.
Without them, levels and departments come from built-in rules. The installer downloads them from
Hugging Face (`qilun-ai/job-level-classifier`, `qilun-ai/job-department-classifier`) with a progress
bar, copies them from `--models-from FOLDER` (FOLDER holds `level-model/` and `department-model/`), or
offers to copy them from an existing Agent-Hire data folder it finds. `--no-models` skips them.

## Browser extension

Load `<install folder>/extension` as an unpacked extension (Developer mode → Load unpacked). The
installer writes `extension/agent-hire.json` with the app's address, so the extension also finds
installs on a port other than 8000. After an update, press reload on the extension card.
