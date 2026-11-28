#Requires -Version 5.1
<#
.SYNOPSIS
    Agent-Hire launcher for Windows. It lives in the install folder and keeps everything the app
    reads or writes inside that folder.

.DESCRIPTION
    agent-hire start      start the app in the background
    agent-hire stop       stop it
    agent-hire restart    stop, then start
    agent-hire status     show whether it runs
    agent-hire open       start if needed, then open the app in the browser
    agent-hire logs       show the end of the logs
    agent-hire version    print the installed version
    agent-hire update     download the latest release and update this install (keeps your data)
    agent-hire uninstall  remove the app (asks before deleting your data)
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)][string]$Command = 'start'
)

$ErrorActionPreference = 'Stop'
$InstallDir = $PSScriptRoot
$App = Join-Path $InstallDir 'app'
$Data = Join-Path $InstallDir 'data'
$Run = Join-Path $InstallDir 'run'
$Logs = Join-Path $InstallDir 'logs'
$EnvFile = Join-Path $InstallDir '.env'
$ApiExe = Join-Path $App 'bin\agent-hire-api.exe'
$NodeExe = Join-Path $App 'runtime\node\node.exe'

function Write-Info([string]$Text) { Write-Host "> $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text) { Write-Host "v $Text" -ForegroundColor Green }
function Write-Warn([string]$Text) { Write-Host "! $Text" -ForegroundColor Yellow }

if (-not (Test-Path -LiteralPath $ApiExe -PathType Leaf)) {
    throw "No app found in $App; run the installer again."
}

function Import-Settings {
    if (Test-Path -LiteralPath $EnvFile) {
        foreach ($line in Get-Content -LiteralPath $EnvFile) {
            if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') {
                $value = $Matches[2].Trim()
                if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) {
                    $value = $value.Substring(1, $value.Length - 2)
                }
                [Environment]::SetEnvironmentVariable($Matches[1], $value, 'Process')
            }
        }
    }
    $script:AppPort = Get-Port 'APP_PORT' 7841
    $script:ApiPort = Get-Port 'API_INTERNAL_PORT' 7851
    if ($script:AppPort -eq $script:ApiPort) { throw "APP_PORT and API_INTERNAL_PORT in $EnvFile must differ" }
    $script:Url = "http://127.0.0.1:$($script:AppPort)"
}

function Get-Port([string]$Name, [int]$Default) {
    $raw = [Environment]::GetEnvironmentVariable($Name, 'Process')
    if (-not $raw) { return $Default }
    $parsed = 0
    if ($raw -notmatch '^[1-9][0-9]{0,4}$' -or -not [int]::TryParse($raw, [ref]$parsed) -or $parsed -gt 65535) {
        throw "$Name in $EnvFile must be a port number (1-65535)"
    }
    return $parsed
}

# Provider keys the app recognises, in the spellings people use (OPENAI_API_KEY, OPENAI_API,
# OPENAI_KEY, OPENAI_API_KEY_1, OPENAI_API_1, likewise for each provider).
$KeyProviders = 'ANTHROPIC|GEMINI|OPENAI|GROQ|NVIDIA|DEEPSEEK|XAI|MOONSHOT|MISTRAL|OPENROUTER|TOGETHER|FIREWORKS|CEREBRAS|PERPLEXITY|COHERE|SAMBANOVA|DASHSCOPE|AZURE_OPENAI|OPENCODE|OPENAI_COMPAT'
$KeyPattern = "^(($KeyProviders)_(API_KEY|API|KEY|API_KEY_1|API_1)|GOOGLE_API_KEY|HF_TOKEN|OPENCODE_BASE_URL|OPENAI_COMPAT_BASE_URL)$"

# A terminal opened before a key was added to the user's environment does not see it yet; read
# the stored user and machine variables directly. Only the names above are copied.
function Import-StoredKeys {
    foreach ($scope in @('User', 'Machine')) {
        $stored = [Environment]::GetEnvironmentVariables($scope)
        foreach ($name in $stored.Keys) {
            if ($name -match $KeyPattern -and -not [Environment]::GetEnvironmentVariable($name, 'Process')) {
                [Environment]::SetEnvironmentVariable($name, [string]$stored[$name], 'Process')
            }
        }
    }
}

# Every path the app and the agent use points inside the install folder, and so do the profile,
# AppData and temp folders, so libraries cannot write anywhere else.
function Set-ConfinedEnvironment {
    $homeDir = Join-Path $Data 'home'
    foreach ($dir in @((Join-Path $Data 'app'), (Join-Path $Data 'jobs'), (Join-Path $Data 'agent'),
            (Join-Path $Data 'scrape-runs'), $homeDir, (Join-Path $homeDir 'AppData\Roaming'),
            (Join-Path $homeDir 'AppData\Local'), (Join-Path $Run 'tmp'), $Logs, (Join-Path $InstallDir 'cache'))) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
    $values = [ordered]@{
        HOME = $homeDir; USERPROFILE = $homeDir
        APPDATA = (Join-Path $homeDir 'AppData\Roaming'); LOCALAPPDATA = (Join-Path $homeDir 'AppData\Local')
        TEMP = (Join-Path $Run 'tmp'); TMP = (Join-Path $Run 'tmp')
        XDG_DATA_HOME = (Join-Path $homeDir '.local\share'); XDG_CONFIG_HOME = (Join-Path $homeDir '.config')
        XDG_CACHE_HOME = (Join-Path $InstallDir 'cache')
        AGENT_HIRE_DATA_DIR = (Join-Path $Data 'app'); AGENT_HIRE_JOBS_DIR = (Join-Path $Data 'jobs')
        AGENT_HIRE_SCRAPE_RUNS_DIR = (Join-Path $Data 'scrape-runs'); AGENT_HIRE_AGENT_DIR = (Join-Path $Data 'agent')
        AGENT_HIRE_RUN_DIR = $Run; AGENT_HIRE_WEB_DIST = (Join-Path $App 'web\dist')
        AGENT_HIRE_SCRAPER_DIR = (Join-Path $App 'scraper'); AGENT_HIRE_BUNDLED_MODELS_DIR = (Join-Path $App 'models')
        UV_CACHE_DIR = (Join-Path $InstallDir 'cache\uv'); UV_PYTHON_INSTALL_DIR = (Join-Path $InstallDir 'cache\python')
        UV_PROJECT_ENVIRONMENT = (Join-Path $Data 'scraper-venv'); UV_PYTHON_PREFERENCE = 'only-managed'
        UV_NO_CONFIG = '1'; UV_FROZEN = '1'
        AGENT_HIRE_API_URL = "http://127.0.0.1:$($script:ApiPort)"; API_ORIGIN = $script:Url
        APP_PORT = "$($script:AppPort)"; API_INTERNAL_PORT = "$($script:ApiPort)"
    }
    foreach ($entry in $values.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, 'Process')
    }
    # The private runtimes first; the system path stays available for optional tools such as Poppler.
    $env:Path = (Join-Path $App 'runtime\bin') + ';' + (Join-Path $App 'runtime\node') + ';' + $env:Path
}

function Initialize-AgentSecret {
    $file = Join-Path $Run 'agent.env'
    if (-not (Test-Path -LiteralPath $file) -or -not (Get-Content -LiteralPath $file -Raw)) {
        $bytes = New-Object byte[] 32
        [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        $secret = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
        Set-Content -LiteralPath $file -Value "AGENT_HIRE_AGENT_SECRET=$secret" -Encoding ASCII
    }
    $line = Get-Content -LiteralPath $file | Where-Object { $_ -match '^AGENT_HIRE_AGENT_SECRET=' } | Select-Object -First 1
    $env:AGENT_HIRE_AGENT_SECRET = $line.Substring('AGENT_HIRE_AGENT_SECRET='.Length)
}

function Get-RecordedProcess([string]$Name) {
    $file = Join-Path $Run "$Name.pid"
    if (-not (Test-Path -LiteralPath $file)) { return $null }
    $id = Get-Content -LiteralPath $file -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $id) { return $null }
    return Get-Process -Id ([int]$id) -ErrorAction SilentlyContinue
}

function Test-PortInUse([int]$Port) {
    try { return [bool](Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction Stop) }
    catch { return $false }
}

function Wait-Ready([string]$CheckUrl, [string]$Label, [int]$Seconds, [string]$Name) {
    for ($i = 0; $i -lt $Seconds; $i++) {
        try {
            $response = Invoke-WebRequest -Uri $CheckUrl -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 400) { Write-Ok "$Label ready"; return }
        } catch { }
        if (-not (Get-RecordedProcess $Name)) { throw "$Label stopped while starting; see $Logs\$Name.err.log" }
        Start-Sleep -Seconds 1
    }
    throw "$Label did not start in time; see $Logs\$Name.err.log"
}

function Start-Part([string]$Name, [string]$FilePath, [string[]]$Arguments) {
    $proc = Start-Process -FilePath $FilePath -ArgumentList $Arguments -WorkingDirectory $InstallDir `
        -RedirectStandardOutput (Join-Path $Logs "$Name.out.log") `
        -RedirectStandardError (Join-Path $Logs "$Name.err.log") `
        -WindowStyle Hidden -PassThru
    Set-Content -LiteralPath (Join-Path $Run "$Name.pid") -Value $proc.Id
}

function Start-App {
    Import-Settings
    if ((Get-RecordedProcess 'api') -and (Get-RecordedProcess 'agent')) {
        Write-Ok "Agent-Hire is already running  ->  $Url"
        return
    }
    Stop-App -Quiet
    foreach ($port in @($ApiPort, $AppPort)) {
        if (Test-PortInUse $port) { throw "port $port is in use; change APP_PORT / API_INTERNAL_PORT in $EnvFile" }
    }
    Import-StoredKeys
    Set-ConfinedEnvironment
    New-Item -ItemType Directory -Force -Path $Run | Out-Null
    Initialize-AgentSecret
    Write-Info "starting the API on :$ApiPort (internal)"
    Start-Part 'api' $ApiExe @('--port', "$ApiPort", '--no-token-stdout')
    Wait-Ready "http://127.0.0.1:$ApiPort/health" 'API' 240 'api'
    Write-Info "starting Agent-Hire on :$AppPort"
    Start-Part 'agent' $NodeExe @("`"$(Join-Path $App 'agent\dist\main.js')`"")
    Wait-Ready "$Url/agent/health" 'agent' 60 'agent'
    Write-Ok "Agent-Hire is up  ->  $Url"
    Write-Host "   browser extension: load the folder $InstallDir\extension (chrome://extensions -> Load unpacked)"
}

function Stop-App([switch]$Quiet) {
    foreach ($name in @('agent', 'api')) {
        $file = Join-Path $Run "$name.pid"
        if (-not (Test-Path -LiteralPath $file)) { continue }
        $id = Get-Content -LiteralPath $file -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($id) { & taskkill.exe /PID $id /T /F 2>$null | Out-Null }
        Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
    }
    if (-not $Quiet) { Write-Ok 'stopped' }
}

function Show-Status {
    Import-Settings
    $version = Get-Content -LiteralPath (Join-Path $App 'VERSION') -ErrorAction SilentlyContinue
    Write-Host "Agent-Hire $version in $InstallDir"
    $api = Get-RecordedProcess 'api'
    $agent = Get-RecordedProcess 'agent'
    if ($api -and $agent) { Write-Ok "running  ->  $Url" }
    elseif ($api -or $agent) { Write-Warn 'partly running; try: agent-hire restart' }
    else { Write-Host '  not running (start: agent-hire start)' }
    Write-Host "  data: $Data     logs: $Logs     settings: $EnvFile"
}

function Remove-App {
    Write-Host "This removes Agent-Hire from $InstallDir."
    $wipe = Read-Host "Also delete your data (profile, documents, jobs, chats) in $Data? [y/N]"
    $sure = Read-Host 'Continue? [y/N]'
    if ($sure -notmatch '^[Yy]') { Write-Host 'cancelled'; return }
    Stop-App -Quiet
    $shortcut = Join-Path ([Environment]::GetFolderPath('Programs')) 'Agent-Hire.lnk'
    Remove-Item -LiteralPath $shortcut -Force -ErrorAction SilentlyContinue
    $userPath = @([Environment]::GetEnvironmentVariable('Path', 'User') -split ';' | Where-Object { $_ -and $_ -ne $InstallDir })
    [Environment]::SetEnvironmentVariable('Path', ($userPath -join ';'), 'User')
    $pointer = Join-Path ([Environment]::GetFolderPath('ApplicationData')) 'Agent-Hire\install-dir'
    if ((Test-Path -LiteralPath $pointer) -and ((Get-Content -LiteralPath $pointer -Raw).Trim() -eq $InstallDir)) {
        Remove-Item -LiteralPath $pointer -Force
    }
    foreach ($part in @('app', 'extension', 'run', 'cache', '.agent-hire', 'LICENSE', 'NOTICE', 'THIRD_PARTY_NOTICES.md', 'README.txt', 'agent-hire.cmd')) {
        Remove-Item -LiteralPath (Join-Path $InstallDir $part) -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($wipe -match '^[Yy]') {
        foreach ($part in @('data', 'logs', '.env')) {
            Remove-Item -LiteralPath (Join-Path $InstallDir $part) -Recurse -Force -ErrorAction SilentlyContinue
        }
        Write-Ok 'Agent-Hire and its data were removed (delete the folder itself when this window closes)'
    } else {
        Write-Ok "Agent-Hire was removed; your data is still in $Data (run the installer again to use it)"
    }
    # This script cannot delete itself while running; schedule it.
    Start-Process -FilePath cmd.exe -ArgumentList "/c timeout /t 2 >nul & del `"$PSCommandPath`"" -WindowStyle Hidden
}

# Runs the one-line installer for this folder: it downloads the latest release and updates in place.
function Update-App {
    $repo = if ($env:AGENT_HIRE_REPO) { $env:AGENT_HIRE_REPO } else { 'Qilun-ai/Agent-Hire' }
    $current = Get-Content -LiteralPath (Join-Path $App 'VERSION') -ErrorAction SilentlyContinue
    Write-Host "Updating Agent-Hire $current in $InstallDir to the latest release..."
    $bootstrap = Join-Path ([IO.Path]::GetTempPath()) ('agent-hire-update-' + [Guid]::NewGuid().ToString('N').Substring(0, 8) + '.ps1')
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri "https://raw.githubusercontent.com/$repo/main/install.ps1" -OutFile $bootstrap -UseBasicParsing
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $bootstrap -Dir $InstallDir
        if ($LASTEXITCODE -ne 0) { throw 'The update did not finish; your current install is unchanged or was only partly updated. Run the update again.' }
    } finally {
        Remove-Item -LiteralPath $bootstrap -Force -ErrorAction SilentlyContinue
    }
}

switch ($Command) {
    'start' { Start-App }
    'stop' { Stop-App }
    'restart' { Stop-App -Quiet; Start-App }
    'status' { Show-Status }
    'open' { Start-App; Start-Process $Url }
    'logs' { Get-Content -LiteralPath (Join-Path $Logs 'api.err.log'), (Join-Path $Logs 'agent.err.log') -Tail 50 -ErrorAction SilentlyContinue }
    'version' { Get-Content -LiteralPath (Join-Path $App 'VERSION') }
    'update' { Update-App }
    'uninstall' { Remove-App }
    default { Get-Help $PSCommandPath -Detailed; exit 2 }
}
