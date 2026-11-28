#Requires -Version 5.1
<#
.SYNOPSIS
    Agent-Hire one-line installer for Windows:

        irm https://raw.githubusercontent.com/Qilun-ai/Agent-Hire/main/install.ps1 | iex

.DESCRIPTION
    Downloads the latest Agent-Hire release for Windows, checks its SHA-256, and runs its installer,
    which asks where to install (default: your user folder\Agent-Hire), downloads the job models,
    adds a Start menu shortcut and starts the app. Running it again updates an existing install.

    $env:AGENT_HIRE_PACKAGE_URL = '<url of a .zip>' installs that package instead (its .sha256 must
    sit next to it); $env:AGENT_HIRE_REPO = '<owner/name>' reads releases from another repository.
#>
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$repo = if ($env:AGENT_HIRE_REPO) { $env:AGENT_HIRE_REPO } else { 'Qilun-ai/Agent-Hire' }

function Write-Step([string]$Text) { Write-Host "  > $Text" -ForegroundColor Cyan }

# Streams a download with a progress bar (Invoke-WebRequest's own bar is very slow on PowerShell 5.1).
function Save-WithProgress([string]$Url, [string]$Path, [string]$Label) {
    Add-Type -AssemblyName System.Net.Http
    $client = New-Object System.Net.Http.HttpClient
    $client.DefaultRequestHeaders.UserAgent.ParseAdd('Agent-Hire-Installer/1.0')
    try {
        $response = $client.GetAsync($Url, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead).Result
        if (-not $response.IsSuccessStatusCode) { throw "download failed ($([int]$response.StatusCode)): $Url" }
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
                if (([DateTime]::Now - $lastShown).TotalMilliseconds -gt 200) {
                    $lastShown = [DateTime]::Now
                    $status = '{0:N0} MB' -f ($done / 1MB)
                    if ($total) {
                        Write-Progress -Activity $Label -Status ('{0} of {1:N0} MB' -f $status, ($total / 1MB)) -PercentComplete ([int](100 * $done / $total))
                    } else {
                        Write-Progress -Activity $Label -Status $status
                    }
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

if ($env:OS -ne 'Windows_NT') { throw 'This installer is for Windows; on Linux run: curl -fsSL https://raw.githubusercontent.com/Qilun-ai/Agent-Hire/main/install.sh | bash' }
if (-not [Environment]::Is64BitOperatingSystem) { throw 'Agent-Hire needs 64-bit Windows.' }

$url = $env:AGENT_HIRE_PACKAGE_URL
if (-not $url) {
    $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/latest" -Headers @{ 'User-Agent' = 'Agent-Hire-Installer/1.0' }
    $asset = $release.assets | Where-Object { $_.name -like '*-windows-x86_64.zip' } | Select-Object -First 1
    if (-not $asset) { throw 'The latest release has no Windows package.' }
    $url = $asset.browser_download_url
}
$name = [IO.Path]::GetFileName(([Uri]$url).AbsolutePath)
$work = Join-Path ([IO.Path]::GetTempPath()) ('agent-hire-install-' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $work | Out-Null
try {
    Write-Step "downloading $name"
    $package = Join-Path $work $name
    Save-WithProgress $url $package "Downloading $name"
    $expected = ((Invoke-WebRequest -Uri "$url.sha256" -UseBasicParsing).Content -split '\s+')[0].Trim().ToLowerInvariant()
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $package).Hash.ToLowerInvariant()
    if ($expected -ne $actual) { throw 'Checksum mismatch: the download is damaged, try again.' }
    Write-Step 'checksum verified'
    Write-Step 'unpacking'
    Expand-Archive -LiteralPath $package -DestinationPath $work -Force
    $installer = Get-ChildItem -LiteralPath $work -Directory | ForEach-Object { Join-Path $_.FullName 'install.ps1' } |
        Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $installer) { throw 'The package has no installer.' }
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installer @args
} finally {
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}
