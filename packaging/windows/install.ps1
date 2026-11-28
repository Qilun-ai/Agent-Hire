#Requires -Version 5.1
<#
.SYNOPSIS
    Agent-Hire installer and updater for Windows.

.DESCRIPTION
    Run it from an unpacked Agent-Hire package (double-click "Install Agent-Hire.cmd"). It asks
    where to install (default: your user folder\Agent-Hire). The app and everything it stores live
    in that folder. Run the installer from a newer package to update: it finds the existing install,
    keeps your data and settings, and replaces only the app.

.PARAMETER Dir
    Install to (or update) this folder.
.PARAMETER Port
    The browser port for a new install (default 7841).
.PARAMETER ModelsFrom
    Copy the job classifier models from this folder (it holds level-model\ and department-model\)
    instead of downloading them.
.PARAMETER Yes
    Accept every default without asking.
.PARAMETER NoStart
    Do not start the app at the end.
.PARAMETER NoShortcut
    Do not add a Start menu shortcut.
.PARAMETER NoModels
    Skip the job classifier models (built-in rules are used).
#>
[CmdletBinding()]
param(
    [string]$Dir = '',
    [int]$Port = 7841,
    [string]$ModelsFrom = '',
    [switch]$Yes,
    [switch]$NoStart,
    [switch]$NoShortcut,
    [switch]$NoModels
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Payload = Join-Path $PSScriptRoot 'payload'
$Pointer = Join-Path ([Environment]::GetFolderPath('ApplicationData')) 'Agent-Hire\install-dir'
$NewVersion = (Get-Content -LiteralPath (Join-Path $Payload 'app\VERSION') -ErrorAction SilentlyContinue)

function Write-Info([string]$Text) { Write-Host "  > $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text) { Write-Host "  v $Text" -ForegroundColor Green }
function Write-Warn([string]$Text) { Write-Host "  ! $Text" -ForegroundColor Yellow }

function Read-Answer([string]$Question, [string]$Default) {
    if ($Yes) { return $Default }
    $answer = Read-Host "  $Question [$Default]"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
    return $answer.Trim()
}

function Confirm-Choice([string]$Question, [string]$Default) {
    return (Read-Answer "$Question (y/n)" $Default) -match '^[Yy]'
}

function Resolve-FullPath([string]$Path) {
    $expanded = [Environment]::ExpandEnvironmentVariables($Path.Trim('"'))
    if ($expanded.StartsWith('~')) { $expanded = Join-Path $HOME $expanded.Substring(1).TrimStart('\', '/') }
    return [IO.Path]::GetFullPath($expanded)
}

function Test-Install([string]$Path) { return Test-Path -LiteralPath (Join-Path $Path '.agent-hire\install.json') }
function Test-KeptData([string]$Path) {
    return (Test-Path -LiteralPath (Join-Path $Path 'data\app')) -and (Test-Path -LiteralPath (Join-Path $Path '.env'))
}
function Get-InstalledVersion([string]$Path) {
    $version = Get-Content -LiteralPath (Join-Path $Path 'app\VERSION') -ErrorAction SilentlyContinue
    if ($version) { return $version } else { return 'unknown' }
}

function Assert-Ready {
    if (-not (Test-Path -LiteralPath (Join-Path $Payload 'app'))) {
        throw 'Run this from an unpacked Agent-Hire package (the payload folder is missing).'
    }
    if ($env:OS -ne 'Windows_NT') { throw 'This package is for Windows.' }
    if (-not [Environment]::Is64BitOperatingSystem) { throw 'Agent-Hire needs 64-bit Windows.' }
    if ([Environment]::OSVersion.Version.Major -lt 10) { throw 'Agent-Hire needs Windows 10 or newer.' }
    if ($Port -lt 1 -or $Port -gt 65534) { throw '-Port must be between 1 and 65534.' }
    if (-not (Get-Command pdftotext.exe -ErrorAction SilentlyContinue)) {
        Write-Warn 'Poppler is not installed: reading PDF resumes will be limited.'
        Write-Warn '  install it with: winget install oschwartz10612.Poppler'
    }
}

# The API needs the Microsoft Visual C++ runtime. Most PCs have it; if not, install Microsoft's
# official redistributable (Windows asks for permission).
function Install-VCRuntime {
    $system = Join-Path $env:SystemRoot 'System32'
    $missing = @('vcruntime140.dll', 'vcruntime140_1.dll', 'msvcp140.dll', 'msvcp140_1.dll') |
        Where-Object { -not (Test-Path -LiteralPath (Join-Path $system $_)) }
    if (-not $missing) { return }
    Write-Info 'installing the Microsoft Visual C++ runtime (Windows will ask for permission)'
    $installer = Join-Path $env:TEMP 'vc_redist.x64.exe'
    Invoke-WebRequest -Uri 'https://aka.ms/vs/17/release/vc_redist.x64.exe' -OutFile $installer -UseBasicParsing
    $signature = Get-AuthenticodeSignature -FilePath $installer
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') {
        Remove-Item -LiteralPath $installer -Force
        throw 'The downloaded Visual C++ runtime is not signed by Microsoft; install it from https://aka.ms/vs/17/release/vc_redist.x64.exe'
    }
    $process = Start-Process -FilePath $installer -ArgumentList '/install', '/quiet', '/norestart' -Verb RunAs -Wait -PassThru
    Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
    if (@(0, 1638, 3010) -notcontains $process.ExitCode) {
        throw "The Visual C++ runtime installer failed (exit code $($process.ExitCode))."
    }
    Write-Ok 'Visual C++ runtime installed'
}

function Select-Target {
    if ($Dir) { return Resolve-FullPath $Dir }
    if (Test-Path -LiteralPath $Pointer) {
        $remembered = (Get-Content -LiteralPath $Pointer -Raw).Trim()
        if ($remembered -and (Test-Install $remembered)) {
            Write-Host "  Agent-Hire $(Get-InstalledVersion $remembered) is installed in $remembered."
            if (Confirm-Choice "Update it to $NewVersion?" 'y') { return $remembered }
        }
    }
    Write-Host '  Agent-Hire keeps the app and all of its data (profile, documents, job index, chats)'
    Write-Host '  in one folder.'
    return Resolve-FullPath (Read-Folder (Join-Path $HOME 'Agent-Hire'))
}

function Read-Folder([string]$Default) {
    if ($Yes) { return $Default }
    Write-Host ''
    Write-Host '  Where should Agent-Hire be installed?'
    Write-Host '    Default: ' -NoNewline
    Write-Host $Default -ForegroundColor White
    Write-Host '    Press Enter to use it, or type another folder.'
    $answer = Read-Host '  Folder'
    $folder = if ([string]::IsNullOrWhiteSpace($answer)) { $Default } else { $answer.Trim().Trim('"') }
    Write-Host "  -> Installing to $folder" -ForegroundColor Green
    return $folder
}

function Assert-Target([string]$Target) {
    $forbidden = @($HOME, $env:SystemRoot, $env:ProgramFiles, ${env:ProgramFiles(x86)}, [IO.Path]::GetPathRoot($Target)) |
        Where-Object { $_ } | ForEach-Object { $_.TrimEnd('\') }
    if ($forbidden -contains $Target.TrimEnd('\')) { throw "Choose a dedicated folder, not $Target." }
    if ((Test-Path -LiteralPath $Target) -and -not (Test-Install $Target) -and -not (Test-KeptData $Target)) {
        if (@(Get-ChildItem -LiteralPath $Target -Force).Count -gt 0) {
            throw "$Target is not empty and is not an Agent-Hire install; choose an empty or new folder."
        }
    }
    New-Item -ItemType Directory -Force -Path $Target | Out-Null
}

function Stop-Running([string]$Target) {
    $launcher = Join-Path $Target 'agent-hire.ps1'
    if (-not (Test-Path -LiteralPath $launcher)) { return $false }
    $status = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $launcher status 2>$null | Out-String
    if ($status -match 'running  ->') {
        Write-Info 'stopping the running app'
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $launcher stop | Out-Null
        return $true
    }
    return $false
}

# Replace app\ and extension\ by moving a complete copy into place; the previous app is kept in
# .agent-hire\previous until the next update.
function Install-Files([string]$Target) {
    $state = Join-Path $Target '.agent-hire'
    foreach ($dir in @((Join-Path $state 'staging'), (Join-Path $state 'previous'))) {
        if (Test-Path -LiteralPath $dir) { Remove-Item -LiteralPath $dir -Recurse -Force }
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
    Write-Info 'copying the app'
    foreach ($part in @('app', 'extension')) {
        Copy-Item -LiteralPath (Join-Path $Payload $part) -Destination (Join-Path $state "staging\$part") -Recurse
        $current = Join-Path $Target $part
        if (Test-Path -LiteralPath $current) { Move-Item -LiteralPath $current -Destination (Join-Path $state "previous\$part") }
        Move-Item -LiteralPath (Join-Path $state "staging\$part") -Destination $current
    }
    foreach ($file in @('agent-hire.ps1', 'agent-hire.cmd', 'LICENSE', 'NOTICE', 'THIRD_PARTY_NOTICES.md', 'README.txt')) {
        $source = Join-Path $Payload $file
        if (Test-Path -LiteralPath $source) { Copy-Item -LiteralPath $source -Destination (Join-Path $Target $file) -Force }
    }
    Remove-Item -LiteralPath (Join-Path $state 'staging') -Recurse -Force
    foreach ($dir in @('data', 'logs', 'run', 'cache')) { New-Item -ItemType Directory -Force -Path (Join-Path $Target $dir) | Out-Null }
    Write-Ok 'app files in place'
}

# A new install gets the template; an update keeps the user's file and appends settings that
# newer versions added (commented out).
function Write-Settings([string]$Target) {
    $envFile = Join-Path $Target '.env'
    $template = Get-Content -LiteralPath (Join-Path $Payload 'env.template')
    $apiPort = if ($Port + 10 -le 65535) { $Port + 10 } else { $Port - 10 }
    $rendered = $template | ForEach-Object { $_.Replace('__APP_PORT__', "$Port").Replace('__API_PORT__', "$apiPort") }
    if (-not (Test-Path -LiteralPath $envFile)) {
        Set-Content -LiteralPath $envFile -Value $rendered -Encoding UTF8
        Write-Ok "settings written to $envFile"
        return
    }
    $existing = Get-Content -LiteralPath $envFile
    if (($existing -contains 'APP_PORT=8000') -and ($existing -contains 'API_INTERNAL_PORT=8010')) {
        $existing = $existing | ForEach-Object {
            if ($_ -eq 'APP_PORT=8000') { "APP_PORT=$Port" }
            elseif ($_ -eq 'API_INTERNAL_PORT=8010') { "API_INTERNAL_PORT=$apiPort" }
            else { $_ }
        }
        Set-Content -LiteralPath $envFile -Value $existing -Encoding UTF8
        Write-Ok "moved to the new default port $Port (8000 is used by many other tools)"
    }
    $added = @()
    foreach ($line in $rendered) {
        if ($line -match '^#?([A-Z_]+)=') {
            $name = $Matches[1]
            $known = $existing | Where-Object { $_ -match "^#?$name=" }
            if (-not $known -and -not ($added | Where-Object { $_ -match "^#$name=" })) { $added += ('#' + $line.TrimStart('#')) }
        }
    }
    if ($added.Count -gt 0) {
        Add-Content -LiteralPath $envFile -Value (@('', "# Added by the update to $NewVersion (uncomment to use):") + $added)
        Write-Ok "kept your settings; added $($added.Count) new option(s) to $envFile"
    } else {
        Write-Ok "kept your settings in $envFile"
    }
}

function Set-ExtensionAddress([string]$Target) {
    $line = Get-Content -LiteralPath (Join-Path $Target '.env') | Where-Object { $_ -match '^APP_PORT=(\d+)' } | Select-Object -Last 1
    $appPort = if ($line -match '^APP_PORT=(\d+)') { $Matches[1] } else { '7841' }
    Set-Content -LiteralPath (Join-Path $Target 'extension\agent-hire.json') -Value "{`"url`": `"http://127.0.0.1:$appPort`"}" -Encoding ASCII
}

function Initialize-Scraper([string]$Target) {
    Write-Info 'preparing the careers scraper (downloads Python and its packages once)'
    $log = Join-Path $Target 'logs\install-scraper.log'
    $env:UV_CACHE_DIR = Join-Path $Target 'cache\uv'
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $Target 'cache\python'
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $Target 'data\scraper-venv'
    $env:UV_PYTHON_PREFERENCE = 'only-managed'
    $env:UV_NO_CONFIG = '1'
    $uv = Join-Path $Target 'app\runtime\bin\uv.exe'
    Push-Location -LiteralPath (Join-Path $Target 'app\scraper')
    try {
        & $uv sync --frozen --no-dev *> $log
        if ($LASTEXITCODE -eq 0) { Write-Ok 'careers scraper ready' }
        else { Write-Warn "the careers scraper could not be prepared (see $log); job scraping needs this installer to run again online" }
    } finally {
        Pop-Location
    }
}

function Test-Model([string]$Path) {
    return (Test-Path -LiteralPath (Join-Path $Path 'labels.json')) -and (Test-Path -LiteralPath (Join-Path $Path 'tokenizer.json')) -and
        [bool](Get-ChildItem -LiteralPath $Path -Filter '*.onnx' -ErrorAction SilentlyContinue)
}

# The job classifiers are not part of the package: they are downloaded from Hugging Face (or copied
# from -ModelsFrom / an existing install). Without them the app uses its built-in rules.
$ModelsBaseUrl = if ($env:AGENT_HIRE_MODELS_BASE_URL) { $env:AGENT_HIRE_MODELS_BASE_URL } else { 'https://huggingface.co' }
$ModelRepos = @(
    @{ Folder = 'level-model'; Repo = 'qilun-ai/job-level-classifier'; Label = 'Experience-level' },
    @{ Folder = 'department-model'; Repo = 'qilun-ai/job-department-classifier'; Label = 'Department' }
)

# Streams a download with a progress bar (Invoke-WebRequest's own bar is very slow on PowerShell 5.1).
function Save-WithProgress([string]$Url, [string]$Path, [string]$Label) {
    Add-Type -AssemblyName System.Net.Http
    $client = New-Object System.Net.Http.HttpClient
    $client.DefaultRequestHeaders.UserAgent.ParseAdd('Agent-Hire-Installer/1.0')
    if ($env:HF_TOKEN) { $client.DefaultRequestHeaders.Authorization = New-Object System.Net.Http.Headers.AuthenticationHeaderValue('Bearer', $env:HF_TOKEN) }
    try {
        $response = $client.GetAsync($Url, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead).Result
        if (-not $response.IsSuccessStatusCode) { throw "download failed ($([int]$response.StatusCode))" }
        $total = $response.Content.Headers.ContentLength
        $source = $response.Content.ReadAsStreamAsync().Result
        $target = [IO.File]::Create($Path)
        try {
            $buffer = New-Object byte[] 1048576
            $done = 0L
            $lastShown = [DateTime]::MinValue
            while (($read = $source.Read($buffer, 0, $buffer.Length)) -gt 0) {
                $target.Write($buffer, 0, $read)
                $done += $read
                if ($total -and ([DateTime]::Now - $lastShown).TotalMilliseconds -gt 200) {
                    $lastShown = [DateTime]::Now
                    Write-Progress -Activity $Label -Status ('{0:N0} of {1:N0} MB' -f ($done / 1MB), ($total / 1MB)) -PercentComplete ([int](100 * $done / $total))
                }
            }
        } finally {
            $target.Dispose()
            $source.Dispose()
        }
        Write-Progress -Activity $Label -Completed
    } finally {
        $client.Dispose()
    }
}

function Test-Model([string]$Path) {
    return (Test-Path -LiteralPath (Join-Path $Path 'labels.json')) -and (Test-Path -LiteralPath (Join-Path $Path 'tokenizer.json')) -and
        [bool](Get-ChildItem -LiteralPath $Path -Filter '*.onnx' -ErrorAction SilentlyContinue)
}

function Get-Model([string]$Jobs, [hashtable]$Model) {
    $work = Join-Path $Jobs "$($Model.Folder).download"
    if (Test-Path -LiteralPath $work) { Remove-Item -LiteralPath $work -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $work | Out-Null
    $base = "$ModelsBaseUrl/$($Model.Repo)/resolve/main/onnx"
    Save-WithProgress "$base/labels.json" (Join-Path $work 'labels.json') "$($Model.Label) model settings"
    $modelFile = (Get-Content -LiteralPath (Join-Path $work 'labels.json') -Raw | ConvertFrom-Json).model_file
    if (-not $modelFile) { $modelFile = 'model.onnx' }
    $modelFile = [IO.Path]::GetFileName($modelFile)
    Save-WithProgress "$base/$modelFile" (Join-Path $work $modelFile) "$($Model.Label) model"
    Save-WithProgress "$base/tokenizer.json" (Join-Path $work 'tokenizer.json') "$($Model.Label) tokenizer"
    if (-not (Test-Model $work)) { throw 'incomplete download' }
    $destination = Join-Path $Jobs $Model.Folder
    if (Test-Path -LiteralPath $destination) { Remove-Item -LiteralPath $destination -Recurse -Force }
    Move-Item -LiteralPath $work -Destination $destination
}

function Copy-Models([string]$Source, [string]$Jobs) {
    foreach ($name in @('level-model', 'department-model')) {
        if (-not (Test-Model (Join-Path $Source $name))) { throw "$Source\$name is not a classifier model folder" }
        $destination = Join-Path $Jobs $name
        if (Test-Path -LiteralPath $destination) { Remove-Item -LiteralPath $destination -Recurse -Force }
        Copy-Item -LiteralPath (Join-Path $Source $name) -Destination $destination -Recurse
    }
    Write-Ok "job classifier models copied from $Source"
}

function Install-Models([string]$Target) {
    $jobs = Join-Path $Target 'data\jobs'
    New-Item -ItemType Directory -Force -Path $jobs | Out-Null
    if ((Test-Model (Join-Path $jobs 'level-model')) -and (Test-Model (Join-Path $jobs 'department-model'))) {
        Write-Ok 'job classifier models already installed'
        return
    }
    if ($ModelsFrom) { Copy-Models (Resolve-FullPath $ModelsFrom) $jobs; return }
    if ($NoModels) {
        Write-Info 'skipped: job levels and departments use built-in rules (re-run the installer to add the models)'
        return
    }
    Write-Info 'downloading the job classifier models from Hugging Face (about 450 MB)'
    foreach ($model in $ModelRepos) {
        if (Test-Model (Join-Path $jobs $model.Folder)) { continue }
        try {
            Get-Model $jobs $model
        } catch {
            Remove-Item -LiteralPath (Join-Path $jobs "$($model.Folder).download") -Recurse -Force -ErrorAction SilentlyContinue
            Write-Warn "could not download the $($model.Label) model ($($_.Exception.Message)); job levels and departments use built-in rules until you re-run the installer"
            return
        }
    }
    Write-Ok 'job classifier models installed'
}

function Add-Shortcut([string]$Target) {
    if ($NoShortcut) { return }
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Programs')) 'Agent-Hire.lnk'))
    $link.TargetPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $link.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Target\agent-hire.ps1`" open"
    $link.WorkingDirectory = $Target
    $link.IconLocation = Join-Path $Target 'app\share\agent-hire.ico'
    $link.Description = 'Your local job-search agent'
    $link.Save()
    Write-Ok 'Start menu shortcut added'
}

# Puts the install folder (agent-hire.cmd) on the user PATH for new terminals.
function Add-ToPath([string]$Target) {
    $current = [Environment]::GetEnvironmentVariable('Path', 'User')
    $parts = @($current -split ';' | Where-Object { $_ })
    if ($parts -contains $Target) { return }
    [Environment]::SetEnvironmentVariable('Path', (($parts + $Target) -join ';'), 'User')
    Write-Ok 'the agent-hire command works in new terminals'
}

function Save-Install([string]$Target) {
    $state = Join-Path $Target '.agent-hire'
    $record = Join-Path $state 'install.json'
    $now = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    $first = $now
    if (Test-Path -LiteralPath $record) {
        $old = Get-Content -LiteralPath $record -Raw | ConvertFrom-Json
        if ($old.installed_at) { $first = $old.installed_at }
    }
    $json = [ordered]@{ version = $NewVersion; installed_at = $first; updated_at = $now } | ConvertTo-Json -Compress
    Set-Content -LiteralPath $record -Value $json -Encoding UTF8
    New-Item -ItemType Directory -Force -Path (Split-Path $Pointer) | Out-Null
    Set-Content -LiteralPath $Pointer -Value $Target -Encoding UTF8
}

Write-Host ''
Write-Host "  Agent-Hire $NewVersion installer"
Write-Host ''
Assert-Ready
Install-VCRuntime
$target = Select-Target
Assert-Target $target
$wasRunning = $false
if (Test-Install $target) {
    $done = 'updated'
    Write-Info "updating $(Get-InstalledVersion $target) -> $NewVersion in $target (your data stays)"
    $wasRunning = Stop-Running $target
} elseif (Test-KeptData $target) {
    $done = 'installed'
    Write-Info "installing to $target and reusing the data kept there"
} else {
    $done = 'installed'
    Write-Info "installing to $target"
}
Install-Files $target
Write-Settings $target
Set-ExtensionAddress $target
Initialize-Scraper $target
Install-Models $target
Add-Shortcut $target
Add-ToPath $target
Save-Install $target

$appLine = Get-Content -LiteralPath (Join-Path $target '.env') | Where-Object { $_ -match '^APP_PORT=(\d+)' } | Select-Object -Last 1
$appPort = if ($appLine -match '^APP_PORT=(\d+)') { $Matches[1] } else { '7841' }
Write-Host ''
Write-Ok "Agent-Hire $NewVersion is $done in $target"
Write-Host ''
Write-Host "    open the app       Start menu -> Agent-Hire   (or: $target\agent-hire.cmd open)"
Write-Host "    address            http://127.0.0.1:$appPort"
Write-Host "    browser extension  chrome://extensions -> Developer mode -> Load unpacked -> $target\extension"
Write-Host "    settings           $target\.env   (decision model, ports; AI keys in your environment are used automatically)"
Write-Host "    update             $target\agent-hire.cmd update"
Write-Host "    uninstall          $target\agent-hire.cmd uninstall"
Write-Host ''
if (-not $NoStart -or $wasRunning) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $target 'agent-hire.ps1') open
}
