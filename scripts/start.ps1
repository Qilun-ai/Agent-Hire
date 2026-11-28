#Requires -Version 5.1
<#
.SYNOPSIS
    Start or stop Agent-Hire on Windows.

.DESCRIPTION
    Starts Agent-Hire the same way scripts/start.sh does on Linux and macOS, as hidden background
    processes, waiting for each part's health check before starting the next:

      1. the Rust API on an internal port (8010);
      2. the agent, the front door on port 8000: it serves the built UI and the chat, and forwards
         API calls to the internal port.

    There is no database server to start. Logs and pid files live in .run\.

    The API renders the web UI itself; -Dev is obsolete and ignored.
    With -Stop, every process started by this script is terminated (whole process tree).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\start.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\start.ps1 -Stop
#>
[CmdletBinding()]
param(
    [switch]$Stop,
    [switch]$Dev,
    [switch]$SkipBuild,
    [string]$EnvFile = '.env'
)

$ErrorActionPreference = 'Stop'

$ScriptDir  = $PSScriptRoot
$RepoRoot   = (Resolve-Path (Join-Path $ScriptDir '..')).Path
$ApiRsDir   = Join-Path $RepoRoot 'apps\api-rs'
$AgentDir   = Join-Path $RepoRoot 'apps\agent'
$WebDir     = Join-Path $RepoRoot 'apps\web'
if (-not [IO.Path]::IsPathRooted($EnvFile)) { $EnvFile = Join-Path $RepoRoot $EnvFile }
if (Test-Path -LiteralPath $EnvFile) {
    foreach ($line in Get-Content -LiteralPath $EnvFile) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') {
            $value = $Matches[2].Trim()
            if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            [Environment]::SetEnvironmentVariable($Matches[1], $value, 'Process')
        }
    }
} elseif ([IO.Path]::GetFileName($EnvFile) -ne '.env') { throw 'selected environment file is missing' }
$RunDir = if ($env:AGENT_HIRE_RUN_DIR) { $env:AGENT_HIRE_RUN_DIR } else { Join-Path $RepoRoot '.run' }
if (-not [IO.Path]::IsPathRooted($RunDir)) { $RunDir = Join-Path $RepoRoot $RunDir }
function Get-ServicePort([string]$Name, [int]$Default) {
    $raw = [Environment]::GetEnvironmentVariable($Name, 'Process')
    if (-not $raw) { return $Default }
    $parsed = 0
    if ($raw -notmatch '^[1-9][0-9]{0,4}$' -or -not [int]::TryParse($raw, [ref]$parsed) -or $parsed -gt 65535) {
        throw "$Name must be a decimal port in 1..65535"
    }
    return $parsed
}
if ($env:APP_PORT -and $env:API_PORT -and $env:APP_PORT -ne $env:API_PORT) { throw 'APP_PORT and API_PORT disagree' }
$AppPort = Get-ServicePort 'APP_PORT' (Get-ServicePort 'API_PORT' 8000)
$ApiInternalPort = Get-ServicePort 'API_INTERNAL_PORT' 8010
if ($AppPort -eq $ApiInternalPort) { throw 'APP_PORT and API_INTERNAL_PORT must differ' }
$env:APP_PORT = "$AppPort"; $env:API_INTERNAL_PORT = "$ApiInternalPort"
$env:AGENT_HIRE_API_URL = "http://127.0.0.1:$ApiInternalPort"
if (-not $env:API_ORIGIN) { $env:API_ORIGIN = "http://127.0.0.1:$AppPort" }

# The agent reads the AI provider settings from the API through an internal route guarded by a
# shared secret (the browser's token must not unlock stored API keys). Generate it once into the
# private run directory; a value you set yourself wins.
function Initialize-AgentSecret {
    if ($env:AGENT_HIRE_AGENT_SECRET) { return }
    New-Item -ItemType Directory -Force -Path $RunDir | Out-Null
    $file = Join-Path $RunDir 'agent.env'
    if (-not (Test-Path -LiteralPath $file) -or -not (Get-Content -LiteralPath $file -Raw)) {
        $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
        $bytes = New-Object byte[] 32; $rng.GetBytes($bytes)
        $secret = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
        Set-Content -LiteralPath $file -Value "AGENT_HIRE_AGENT_SECRET=$secret" -Encoding ASCII
    }
    $line = Get-Content -LiteralPath $file | Where-Object { $_ -match '^AGENT_HIRE_AGENT_SECRET=' } | Select-Object -First 1
    $env:AGENT_HIRE_AGENT_SECRET = $line.Substring('AGENT_HIRE_AGENT_SECRET='.Length)
}

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

function Test-PortInUse {
    param([int]$Port)
    try { return [bool](Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction Stop) }
    catch { return $false }
}

function Stop-All {
    if (-not (Test-Path $RunDir)) { Write-Host 'stopped (nothing recorded)'; return }
    Get-ChildItem -Path $RunDir -Filter '*.pid' -ErrorAction SilentlyContinue | ForEach-Object {
        $processId = (Get-Content $_.FullName -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($processId) {
            # /T kills the whole tree: the API and JavaScript tooling can spawn children.
            taskkill /PID $processId /T /F 2>$null | Out-Null
        }
        Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue
    }
    Write-Host 'stopped' -ForegroundColor Green
}

function Wait-Http {
    param([string]$Url, [string]$Label, [int]$Tries = 120)
    for ($i = 0; $i -lt $Tries; $i++) {
        $ready = $false
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
            $ready = ($response.StatusCode -ge 200 -and $response.StatusCode -lt 400)
        } catch {
            $ready = $false
        }
        if ($ready) {
            Write-Host ("  v {0} ready" -f $Label) -ForegroundColor Green
            return
        }
        Start-Sleep -Seconds 1
    }
    throw ("{0} did not become ready; see {1}" -f $Label, $RunDir)
}

function Start-ServiceProcess {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )
    New-Item -ItemType Directory -Force -Path $RunDir | Out-Null
    # Prefer a real Application over a PowerShell shim; Start-Process cannot run npm.ps1 correctly.
    $found = @(Get-Command $FilePath -All -ErrorAction Stop)
    $app = @($found | Where-Object { $_.CommandType -eq 'Application' }) | Select-Object -First 1
    $exe = if ($app) { $app.Source } else { $found[0].Source }
    $proc = Start-Process -FilePath $exe -ArgumentList $Arguments -WorkingDirectory $WorkingDirectory `
        -RedirectStandardOutput (Join-Path $RunDir "$Name.out.log") `
        -RedirectStandardError (Join-Path $RunDir "$Name.err.log") `
        -WindowStyle Hidden -PassThru
    Set-Content -Path (Join-Path $RunDir "$Name.pid") -Value $proc.Id
    Write-Host ("  > started {0} (pid {1})" -f $Name, $proc.Id) -ForegroundColor Cyan
}

if ($Stop) {
    Stop-All
    exit 0
}

if ($env:OS -ne 'Windows_NT') {
    throw 'This script is for Windows. Use ./scripts/start.sh on Linux or macOS.'
}

Update-SessionPath

$checkedPorts = @($ApiInternalPort, $AppPort)
foreach ($port in $checkedPorts) {
    if (Test-PortInUse -Port $port) {
        throw ("port {0} is already in use - run 'scripts\start.ps1 -Stop' first" -f $port)
    }
}

Initialize-AgentSecret

if (-not $SkipBuild) {
    if (-not (Test-Command cargo)) { throw 'Cargo is required. Run scripts\install.ps1 first.' }
    Write-Host 'building the API' -ForegroundColor Cyan
    Push-Location -LiteralPath $ApiRsDir
    try {
        & cargo build --release --target-dir target -p agent-hire-server
        if ($LASTEXITCODE -ne 0) { throw 'API build failed' }
    } finally {
        Pop-Location
    }
}
$ApiBin = Join-Path $ApiRsDir 'target\release\agent-hire-api.exe'
if (-not (Test-Path -LiteralPath $ApiBin -PathType Leaf)) { throw "API binary not found at $ApiBin" }
Write-Host ("starting API on :{0} (internal)" -f $ApiInternalPort) -ForegroundColor Cyan
Start-ServiceProcess -Name 'api' -FilePath $ApiBin `
    -Arguments @('--port', $ApiInternalPort, '--no-token-stdout') `
    -WorkingDirectory $ApiRsDir
# Cold starts (for example after a power cut) can take minutes.
Wait-Http -Url ("http://127.0.0.1:{0}/health" -f $ApiInternalPort) -Label 'API' -Tries 240

$jsTool = if (Test-Command npm) { 'npm' } elseif (Test-Command bun) { 'bun' } else { $null }
if (-not $jsTool) { throw 'npm or Bun is required; run scripts\install.ps1 first.' }
if (-not $SkipBuild) {
    Write-Host 'building the web UI' -ForegroundColor Cyan
    Push-Location -LiteralPath $WebDir
    try {
        if (-not (Test-Path -LiteralPath (Join-Path $WebDir 'node_modules'))) {
            & $jsTool install
            if ($LASTEXITCODE -ne 0) { throw 'installing the web UI packages failed' }
        }
        & $jsTool run build
        if ($LASTEXITCODE -ne 0) { throw 'web UI build failed' }
    } finally {
        Pop-Location
    }
    Write-Host 'building the agent' -ForegroundColor Cyan
    Push-Location -LiteralPath $AgentDir
    try {
        & $jsTool run build
        if ($LASTEXITCODE -ne 0) { throw 'agent build failed' }
    } finally {
        Pop-Location
    }
}

Write-Host ("starting Agent-Hire on :{0}" -f $AppPort) -ForegroundColor Cyan
# The agent serves the web UI and forwards /api, /ws and /bootstrap to the API.
$env:AGENT_HIRE_WEB_DIST = Join-Path $WebDir 'dist'
Start-ServiceProcess -Name 'agent' -FilePath 'node' -Arguments @('dist/main.js') -WorkingDirectory $AgentDir
Wait-Http -Url ("http://127.0.0.1:{0}/agent/health" -f $AppPort) -Label 'agent'

$url = "http://127.0.0.1:{0}" -f $AppPort

Write-Host ''
Write-Host ("Agent-Hire is up  ->  {0}" -f $url) -ForegroundColor Green
Write-Host ("   logs: {0}\*.log     stop: powershell -ExecutionPolicy Bypass -File scripts\start.ps1 -Stop" -f $RunDir)
