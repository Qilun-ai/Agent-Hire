#Requires -Version 5.1
<#
.SYNOPSIS
    Agent-Hire one-command installer for Windows.

.DESCRIPTION
    Installs and deploys everything a native Windows Agent-Hire needs:

      1. winget packages (Git, Node.js LTS)
      2. toolchains: Rust, uv (scraper), Node.js
      3. workspace dependencies (careers scraper via uv, API via Cargo, extension/agent via npm)
      4. local configuration (.env files, copied from the committed examples)
      5. production builds (the API, which also renders the web UI, and the browser extension)
      6. optionally starts the stack in the background

    There is no database server or container runtime to install: the scraper service keeps its
    working state in one SQLite file.

    Design rules:
      - Idempotent: safe to re-run; existing config files are never overwritten.
      - Non-destructive: it never deletes databases, configs, or build output.
      - Honest: it fails with the failing command and a stack trace instead of continuing half-installed.
      - No secrets: nothing is written to the repository; .env files are gitignored.

.PARAMETER Yes
    Answer yes to every prompt (unattended installs).

.PARAMETER Start
    Start Agent-Hire in the background after installing.

.PARAMETER Dev
    Start with the hot-reload dev server (implies -Start).

.PARAMETER DepsOnly
    Only install dependencies; skip the builds.

.PARAMETER SkipDocker
    Obsolete and ignored: Agent-Hire no longer uses Docker. Accepted so existing automation keeps working.

.PARAMETER DryRun
    Print every action without changing the system.

.PARAMETER Help
    Show this help.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Start

.EXAMPLE
    # One-liner with no checkout (downloads the repository first)
    & ([scriptblock]::Create((irm https://raw.githubusercontent.com/Qilun-ai/Agent-Hire/main/scripts/install.ps1))) -Start

.NOTES
    Environment overrides for the one-liner:
      AGENT_HIRE_DIR  where to download the checkout (default: %USERPROFILE%\Agent-Hire)
      AGENT_HIRE_REF  git ref to download, e.g. a branch (default: main)
#>
[CmdletBinding()]
param(
    [switch]$Yes,
    [switch]$Start,
    [switch]$Dev,
    [switch]$DepsOnly,
    [switch]$SkipDocker,
    [switch]$DryRun,
    [switch]$Help
)

$ErrorActionPreference = 'Stop'

# ---- bootstrap (irm | iex) --------------------------------------------------
# When this file is piped (irm ... | iex) or run outside a checkout there is no repository to
# install from. Download one first (no git required), then hand over to the real installer inside
# it. A normal checkout skips this entirely.
function Get-BootstrapCheckout {
    param([string]$Ref, [string]$Target)
    $archive = "https://codeload.github.com/Qilun-ai/Agent-Hire/zip/refs/heads/$Ref"
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("agent-hire-" + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    $zip = Join-Path $tmp 'agent-hire.zip'
    Invoke-WebRequest -Uri $archive -OutFile $zip -UseBasicParsing
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    $inner = Get-ChildItem $tmp -Directory |
        Where-Object { Test-Path (Join-Path $_.FullName 'Makefile') } |
        Select-Object -First 1
    if (-not $inner) { throw 'The downloaded archive did not contain a repository directory.' }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Target) | Out-Null
    New-Item -ItemType Directory -Force -Path $Target | Out-Null
    # Overlay the code: .env files and .run state are not part of the archive, so they survive.
    Copy-Item -Path (Join-Path $inner.FullName '*') -Destination $Target -Recurse -Force
    Remove-Item -Recurse -Force $tmp
}

function Invoke-Bootstrap {
    param([hashtable]$BoundParameters)
    if ($env:OS -ne 'Windows_NT') {
        throw 'This installer is for Windows. Use ./scripts/install.sh on Linux or macOS.'
    }
    $ref = if ($env:AGENT_HIRE_REF) { $env:AGENT_HIRE_REF } else { 'main' }
    $target = if ($env:AGENT_HIRE_DIR) { $env:AGENT_HIRE_DIR } else { Join-Path $HOME 'Agent-Hire' }

    if (Test-Path (Join-Path $target 'Makefile')) {
        if (Test-Path (Join-Path $target '.git')) {
            Write-Host ("  > Using the existing git checkout at {0} (update it with git pull)" -f $target) -ForegroundColor Cyan
        }
        elseif ($env:AGENT_HIRE_NO_UPDATE) {
            Write-Host ("  > Using the existing checkout at {0}" -f $target) -ForegroundColor Cyan
        }
        else {
            Write-Host ("  > Refreshing the existing checkout at {0} ({1})..." -f $target, $ref) -ForegroundColor Cyan
            Get-BootstrapCheckout -Ref $ref -Target $target
            Write-Host ("  > Checkout updated at {0}" -f $target) -ForegroundColor Green
        }
    }
    else {
        if (Test-Path $target) {
            if ((Get-ChildItem -Force $target | Measure-Object).Count -eq 0) {
                Remove-Item -Force $target
            }
            else {
                throw ("{0} exists but is not a Agent-Hire checkout. Set AGENT_HIRE_DIR to another path." -f $target)
            }
        }
        Write-Host ("  > Downloading Agent-Hire ({0}) to {1}..." -f $ref, $target) -ForegroundColor Cyan
        Get-BootstrapCheckout -Ref $ref -Target $target
        Write-Host ("  > Checkout ready at {0}" -f $target) -ForegroundColor Green
    }

    # Re-run the real installer from the checkout with the original arguments.
    $installer = Join-Path $target 'scripts\install.ps1'
    & $installer @BoundParameters
    exit $LASTEXITCODE
}

# ---- paths ------------------------------------------------------------------
$ScriptDir = $PSScriptRoot
$RepoRoot = ''
if ($ScriptDir) {
    $candidate = Join-Path $ScriptDir '..'
    if (Test-Path $candidate) { $RepoRoot = (Resolve-Path $candidate).Path }
}
if (-not $RepoRoot -or
    -not (Test-Path (Join-Path $RepoRoot 'Makefile')) -or
    -not (Test-Path (Join-Path $RepoRoot 'services\careers-scraper'))) {
    Invoke-Bootstrap -BoundParameters $PSBoundParameters
}
$CareersDir = Join-Path $RepoRoot 'services\careers-scraper'
$ApiDir     = Join-Path $RepoRoot 'apps\api-rs'
$ExtDir     = Join-Path $RepoRoot 'apps\extension'
$AgentDir   = Join-Path $RepoRoot 'apps\agent'
$RunDir     = Join-Path $RepoRoot '.run'
$LogFile    = Join-Path $RunDir ("install-{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))

# Vite 7 (web + extension) requires Node ^20.19 || >=22.12.
$NodeMinMajorLow = 20; $NodeMinMinorLow = 19
$NodeMinMajorHigh = 22; $NodeMinMinorHigh = 12

$TotalSteps = 5
$Step = 0
$ShellPathUpdated = $false

if ($Dev) { $Start = $true }

# ---- logging ----------------------------------------------------------------
function Write-LogLine {
    param([string]$Message)
    if ($script:DryRun) { return }
    try { Add-Content -Path $script:LogFile -Value ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $Message) -ErrorAction SilentlyContinue } catch { $null = $_ }
}

function Write-Info {
    param([string]$Message)
    Write-Host ("  > {0}" -f $Message) -ForegroundColor Cyan
    Write-LogLine "INFO  $Message"
}

function Write-Ok {
    param([string]$Message)
    Write-Host ("  v {0}" -f $Message) -ForegroundColor Green
    Write-LogLine "OK    $Message"
}

function Write-WarnLine {
    param([string]$Message)
    Write-Host ("  ! {0}" -f $Message) -ForegroundColor Yellow
    Write-LogLine "WARN  $Message"
}

function Write-Section {
    param([string]$Message)
    $script:Step++
    Write-Host ""
    Write-Host ("==> [{0}/{1}] {2}" -f $script:Step, $script:TotalSteps, $Message) -ForegroundColor White
    Write-LogLine "== $Message"
}

# ---- helpers ----------------------------------------------------------------
function Test-Command {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Update-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $parts = @()
    if ($machine) { $parts += $machine }
    if ($user) { $parts += $user }
    $parts += (Join-Path $HOME '.local\bin')
    $parts += (Join-Path $HOME '.bun\bin')
    $parts += (Join-Path $HOME '.cargo\bin')
    $env:Path = ($parts -join ';')
}

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Confirm {
    param([string]$Prompt)
    if ($script:Yes) { return $true }
    $reply = Read-Host ("  ? {0} [Y/n]" -f $Prompt)
    if ([string]::IsNullOrWhiteSpace($reply)) { return $true }
    return $reply -match '^[Yy]'
}

# Resolve a command name to a concrete executable. PowerShell resolves a bare `npm` to npm.ps1 on
# Windows when both npm.cmd and npm.ps1 exist. Node's npm.ps1 shim reconstructs its arguments from
# the caller's SOURCE TEXT, so `& $FilePath @Arguments` (invoked from a function) makes it treat the
# literal parameter name as the command: `Unknown command: "FilePath"`. The .cmd/.exe launchers
# forward arguments correctly, so always prefer an Application (cmd/bat/exe) over an ExternalScript.
function Resolve-NativeCommand {
    param([string]$Name)
    $found = @(Get-Command $Name -All -ErrorAction SilentlyContinue)
    $apps = @($found | Where-Object { $_.CommandType -eq 'Application' })
    if ($apps.Count -gt 0) {
        $preferred = $apps | Where-Object { $_.Source -match '\.(cmd|bat|exe)$' } | Select-Object -First 1
        if ($preferred) { return $preferred.Source }
        return $apps[0].Source
    }
    if ($found.Count -gt 0) { return $found[0].Source }
    return $Name
}

# Run a native command. Honors -DryRun, logs it, and throws on a non-zero exit code
# unless -AllowFailure is set (callers then read $script:NativeExit).
$script:NativeExit = 0
function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [string]$WorkingDirectory = $RepoRoot,
        [switch]$AllowFailure
    )
    $display = "{0} {1}" -f $FilePath, ($Arguments -join ' ')
    if ($script:DryRun) {
        Write-Host ("      [dry-run] {0}" -f $display) -ForegroundColor DarkGray
        Write-LogLine "DRY   $display"
        $script:NativeExit = 0
        return
    }
    Write-LogLine "RUN   $display"
    # Never invoke a PowerShell shim (npm.ps1); see Resolve-NativeCommand.
    $resolved = Resolve-NativeCommand -Name $FilePath
    if ($resolved -ne $FilePath) { Write-LogLine "      -> $resolved" }
    Push-Location -LiteralPath $WorkingDirectory
    try {
        & $resolved @Arguments
        $script:NativeExit = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($script:NativeExit -ne 0 -and -not $AllowFailure) {
        throw "Command failed with exit code $($script:NativeExit): $display"
    }
}

function Install-WingetPackage {
    param([string]$Id, [string]$Label = $Id)
    if (-not (Test-Command winget)) {
        throw "winget is required to install $Label automatically. Install 'App Installer' from the Microsoft Store, then re-run."
    }
    Write-Info "Installing $Label via winget ($Id)..."
    Invoke-Native -FilePath 'winget' -Arguments @(
        'install', '--id', $Id, '-e',
        '--accept-source-agreements', '--accept-package-agreements', '--silent'
    )
    Update-SessionPath
}

function Test-NodeVersion {
    if (-not (Test-Command node)) { return $false }
    try { $raw = (& node -v 2>$null) } catch { return $false }
    if (-not $raw) { return $false }
    $v = $raw.Trim().TrimStart('v')
    $parts = $v.Split('.')
    if ($parts.Count -lt 2) { return $false }
    $major = 0; $minor = 0
    [void][int]::TryParse($parts[0], [ref]$major)
    [void][int]::TryParse($parts[1], [ref]$minor)
    if ($major -gt $NodeMinMajorHigh) { return $true }
    if ($major -eq $NodeMinMajorLow -and $minor -ge $NodeMinMinorLow) { return $true }
    if ($major -eq $NodeMinMajorHigh -and $minor -ge $NodeMinMinorHigh) { return $true }
    return $false
}

# ---- 1. system packages -----------------------------------------------------
function Install-BasePackages {
    Write-Section 'System packages'
    if (Test-Command git) { Write-Ok 'Git is present' }
    else {
        Write-WarnLine 'Git is required to fetch dependencies.'
        if (Confirm 'Install Git with winget?') { Install-WingetPackage -Id 'Git.Git' -Label 'Git' }
        else { throw 'Git is required.' }
    }
    if (-not (Test-Command winget)) {
        Write-WarnLine "winget was not found. Automatic installs of Node.js require 'App Installer'."
    }
}

# ---- 2. toolchains ----------------------------------------------------------
function Ensure-Uv {
    Update-SessionPath
    if (Test-Command uv) { Write-Ok ("uv {0}" -f ((& uv --version) -replace '^uv\s+', '')) ; return }
    if ($script:DryRun) {
        Write-Info '[dry-run] would install uv from https://astral.sh/uv'
        Write-Ok 'uv would be installed'
        return
    }
    Write-Info 'Installing uv for the careers scraper...'
    try { Invoke-RestMethod -Uri 'https://astral.sh/uv/install.ps1' | Invoke-Expression }
    catch {
        Write-WarnLine 'The uv installer failed; trying winget.'
        Install-WingetPackage -Id 'astral-sh.uv' -Label 'uv'
    }
    Update-SessionPath
    if (-not (Test-Command uv)) { throw 'uv was installed but is not on PATH (expected %USERPROFILE%\.local\bin\uv.exe).' }
    Write-Ok 'uv installed'
}

function Ensure-Node {
    Update-SessionPath
    if (Test-NodeVersion) { Write-Ok ("Node.js {0}" -f (& node -v)) ; return }
    if (Test-Command node) { Write-WarnLine ("Node.js {0} is older than required (need 20.19+/22.12+)." -f (& node -v)) }
    if ($script:DryRun) {
        Write-Info '[dry-run] would install Node.js LTS via winget'
        Write-Ok 'Node.js would be installed'
        return
    }
    Install-WingetPackage -Id 'OpenJS.NodeJS.LTS' -Label 'Node.js LTS'
    if (-not (Test-NodeVersion)) { throw 'Node.js installation did not produce a supported version; open a new terminal and re-run.' }
    Write-Ok ("Node.js {0}" -f (& node -v))
}

function Ensure-ShellPath {
    # Expose the per-user toolchain directories to future PowerShell sessions.
    $profileDir = Split-Path -Parent $PROFILE
    if (-not $profileDir) { return }
    $existing = ''
    if (Test-Path $PROFILE) { $existing = Get-Content -Path $PROFILE -Raw -ErrorAction SilentlyContinue }
    if ($existing -and $existing -match '\.bun\\bin' -and $existing -match '\.cargo\\bin') { return }
    if ($script:DryRun) {
        Write-Info ("[dry-run] would add the toolchain PATH to {0}" -f $PROFILE)
        return
    }
    New-Item -ItemType Directory -Force -Path $profileDir | Out-Null
    if (-not (Test-Path $PROFILE)) { New-Item -ItemType File -Force -Path $PROFILE | Out-Null }
    $line = "`n# Agent-Hire toolchain (added by scripts\install.ps1)`n" +
            "`$env:Path += ';' + (Join-Path `$HOME '.local\bin') + ';' + (Join-Path `$HOME '.bun\bin') + ';' + (Join-Path `$HOME '.cargo\bin')`n"
    Add-Content -Path $PROFILE -Value $line
    $script:ShellPathUpdated = $true
}

function Ensure-Toolchains {
    Write-Section 'Toolchains (Rust, uv, Node.js)'
    Update-SessionPath
    if (-not (Test-Command cargo)) {
        Install-WingetPackage -Id 'Rustlang.Rustup' -Label 'Rust'
        Update-SessionPath
        if (-not $script:DryRun -and -not (Test-Command cargo)) { throw 'Rust was installed but Cargo was not found on PATH.' }
    }
    Ensure-Uv
    Ensure-Node
    Ensure-ShellPath
    Write-Ok 'toolchains ready'
}

# ---- 3. workspace dependencies ----------------------------------------------
function Invoke-UvSync {
    param([string]$Directory, [string[]]$Extra = @())
    $uvArgs = @('sync') + $Extra
    Invoke-Native -FilePath 'uv' -Arguments $uvArgs -WorkingDirectory $Directory
}

function Invoke-NodeInstall {
    param([string]$Directory)
    if (Test-Path (Join-Path $Directory 'package-lock.json')) {
        Invoke-Native -FilePath 'npm' -Arguments @('ci') -WorkingDirectory $Directory
    }
    else {
        throw "No lockfile found in $Directory; refusing an unpinned install."
    }
}

function Install-WorkspaceDeps {
    Write-Section 'Workspace dependencies'
    Write-Info 'careers scraper Python environment (services\careers-scraper)...'
    Invoke-UvSync -Directory $CareersDir
    Write-Info 'Rust API dependencies (apps\api-rs)...'
    Invoke-Native -FilePath 'cargo' -Arguments @('fetch', '--locked') -WorkingDirectory $ApiDir
    Write-Info 'browser extension dependencies (apps\extension)...'
    Invoke-NodeInstall -Directory $ExtDir
    Write-Info 'agent dependencies (apps\agent)...'
    Invoke-NodeInstall -Directory $AgentDir
    Write-Ok 'all workspace dependencies installed'
}

# ---- 5. configuration -------------------------------------------------------
function Prepare-Env {
    Write-Section 'Configuration'
    if (-not $script:DryRun) {
        New-Item -ItemType Directory -Force -Path $RunDir | Out-Null
    }

    Write-Ok 'configuration ready (no secrets are written to the repository)'
}

# ---- 5. builds --------------------------------------------------------------
function Build-Artifacts {
    Write-Section 'Production builds'
    Update-SessionPath
    Write-Info 'Building the Rust API and its web UI (requires the MSVC C++ build tools)...'
    Invoke-Native -FilePath 'cargo' -Arguments @('build', '--release', '--target-dir', 'target', '-p', 'agent-hire-server') -WorkingDirectory $ApiDir
    Write-Info 'Building the browser extension...'
    Invoke-Native -FilePath 'npm' -Arguments @('run', 'build') -WorkingDirectory $ExtDir
    Write-Ok 'builds complete'
}

# ---- start / summary --------------------------------------------------------
function Show-Summary {
    if ($script:DryRun) {
        Write-Host ''
        Write-Host 'Dry run complete - no changes were made.' -ForegroundColor White
        Write-Host '  Re-run without -DryRun to perform the install (add -Start to launch afterwards).'
        return
    }
    Write-Host ''
    Write-Host 'Agent-Hire is installed.' -ForegroundColor Green
    Write-Host '  Web + API   http://127.0.0.1:8000'
    Write-Host ("  Logs        {0}\*.log" -f $RunDir)
    Write-Host ''
    Write-Host '  Start       powershell -ExecutionPolicy Bypass -File scripts\start.ps1'
    Write-Host '  Stop        powershell -ExecutionPolicy Bypass -File scripts\start.ps1 -Stop'
    Write-Host '  Docs        docs\getting-started.md'
    if ($script:Start) {
        Write-Host '  Started in the background; stop with scripts\start.ps1 -Stop'
    }
    else {
        Write-Host ''
        Write-Host '  Start it when ready:  powershell -ExecutionPolicy Bypass -File scripts\start.ps1'
    }
    Write-Host ''
    Write-Host '  Next: open the app, go to Settings, and configure an AI provider (docs\ai-providers.md).'
    if ($script:ShellPathUpdated) {
        Write-Host '  Open a new PowerShell window before running start.ps1 so the toolchains are on PATH.'
    }
    Write-Host ''
    Write-Host ("  Full install log: {0}" -f $script:LogFile)
}

function Show-Usage {
    Write-Host @'
Agent-Hire installer for Windows

Usage: powershell -ExecutionPolicy Bypass -File scripts\install.ps1 [options]

Options:
  -Yes          assume "yes" for all prompts (non-interactive installs)
  -Start        start Agent-Hire in the background after installing
  -Dev          obsolete and ignored (the web UI is part of the API now)
  -DepsOnly     only install dependencies; skip the builds
  -SkipDocker   obsolete and ignored (Agent-Hire no longer uses Docker)
  -DryRun       print what would happen without changing anything
  -Help         show this help

Exit status: 0 on success, non-zero on the first failed step.
Full output is mirrored to .run\install-*.log.
'@
}

# ---- main -------------------------------------------------------------------
function Main {
    if ($Help) { Show-Usage; return }
    if ($DepsOnly -and $Start) {
        throw '-DepsOnly cannot be combined with -Start.'
    }
    if ($env:OS -ne 'Windows_NT') {
        throw 'This installer is for Windows. Use ./scripts/install.sh on Linux or macOS.'
    }
    if (-not (Test-Path (Join-Path $RepoRoot 'Makefile')) -or -not (Test-Path $CareersDir)) {
        throw 'This does not look like the Agent-Hire repository (expected Makefile + services\careers-scraper).'
    }

    # Compute the step total for the chosen mode.
    $script:TotalSteps = 5
    if ($Start) { $script:TotalSteps = 6 }
    if ($DepsOnly) { $script:TotalSteps = 4 }

    if (-not $script:DryRun) {
        New-Item -ItemType Directory -Force -Path $RunDir | Out-Null
    }
    Write-LogLine ("Agent-Hire installer starting (dry-run={0})" -f $script:DryRun)

    # Fail early with a useful message on a machine with a tiny disk.
    try {
        $drive = (Get-Item $RepoRoot).PSDrive
        if ($drive -and $drive.Free -lt 12GB) {
            Write-WarnLine ("Less than 12 GB free on {0}: dependencies and build output may not fit." -f $drive.Name)
        }
    } catch { $null = $_ }

    Install-BasePackages
    Ensure-Toolchains
    Install-WorkspaceDeps
    Prepare-Env

    if ($DepsOnly) {
        Write-Host ''
        Write-Host '==> Dependencies only - finished' -ForegroundColor White
        Write-Ok 'Dependencies are installed. Re-run without -DepsOnly to run the builds.'
        return
    }

    Build-Artifacts

    if ($Start) {
        Write-Section 'Starting Agent-Hire'
        & (Join-Path $ScriptDir 'start.ps1')
    }

    Show-Summary
}

try {
    Main
} catch {
    Write-Host ''
    Write-Host ("Installation failed: {0}" -f $_.Exception.Message) -ForegroundColor Red
    if ($_.ScriptStackTrace) { Write-Host $_.ScriptStackTrace -ForegroundColor DarkGray }
    if ((-not $DryRun) -and (Test-Path $LogFile)) { Write-Host ("  Log: {0}" -f $LogFile) }
    exit 1
}
