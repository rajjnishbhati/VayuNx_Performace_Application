# Keeps the VAYUNX Profiler reachable from the internet.
#
# A Cloudflare Quick Tunnel has no stable hostname: if Cloudflare drops the registration (sleep, a network
# blip, or no reason you will ever see), cloudflared retries a tunnel that no longer exists and the URL stays
# dead forever. Only a restart fixes it, and every restart hands out a new random *.trycloudflare.com name.
#
# So this script does one check per run - meant to be run every couple of minutes by the scheduled task
# "VAYUNX Tunnel Watchdog" (see docs/HOSTING.md):
#   1. the app answers on 127.0.0.1 (if not, ops\start-vayunx.ps1 starts it);
#   2. the public URL answers 200 (if not, the tunnel is replaced and the new URL recorded).
#
# Where to find the current URL, in order of convenience:
#   - the desktop shortcut "VAYUNX Profiler.url"
#   - ops\logs\tunnel-url.txt   (just the URL, one line)
#   - ops\logs\watchdog.log     (every check that did something, with timestamps)
#
# Run it by hand any time: powershell -ExecutionPolicy Bypass -File ops\tunnel-watchdog.ps1

[CmdletBinding()]
param(
    # Skip creating or updating the desktop shortcut.
    [switch]$NoShortcut,
    # Replace the tunnel even if the current URL answers. For testing.
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$root     = Split-Path -Parent $PSScriptRoot
$logs     = Join-Path $root "ops\logs"
$urlFile  = Join-Path $logs "tunnel-url.txt"
$histFile = Join-Path $logs "watchdog.log"
$tunLog   = Join-Path $logs "quick-tunnel.log"
$uiUrl    = "http://127.0.0.1:3000/"

New-Item -ItemType Directory -Force -Path $logs | Out-Null

function Write-Note([string]$Message, [string]$Level = "INFO") {
    $line = "{0} {1} {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level.PadRight(5), $Message
    Add-Content -Path $histFile -Value $line -Encoding utf8
    Write-Output $line
}

function Find-Cloudflared {
    $candidates = @(
        "C:\Program Files (x86)\cloudflared\cloudflared.exe",
        "C:\Program Files\cloudflared\cloudflared.exe"
    )
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    $cmd = Get-Command cloudflared.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "cloudflared.exe not found"
}

# Only the Quick Tunnel we started: it carries --url on its command line. The named tunnel
# "Benchmark_Application" runs as a SYSTEM service with a token and no --url, and must be left alone
# (its CommandLine also reads back empty for us, which is another reason never to touch it).
function Get-QuickTunnelProcess {
    Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match '--url' }
}

function Test-Url([string]$Url, [int]$TimeoutSec = 20) {
    if (-not $Url) { return $false }
    try { return (Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec $TimeoutSec).StatusCode -eq 200 }
    catch { return $false }
}

function Get-RecordedUrl {
    if (Test-Path $urlFile) {
        $recorded = (Get-Content $urlFile -TotalCount 1).Trim()
        # Anything that is not a URL (a truncated write, an edit by hand) is ignored rather than
        # treated as a dead tunnel, which would throw away a perfectly good one.
        if ($recorded -match "^https://[a-z0-9-]+\.trycloudflare\.com$") { return $recorded }
    }
    if (Test-Path $tunLog) {
        $m = Select-String -Path $tunLog -Pattern "https://[a-z0-9-]+\.trycloudflare\.com" -AllMatches |
             Select-Object -Last 1
        if ($m) { return $m.Matches[-1].Value }
    }
    return $null
}

function Update-Shortcut([string]$Url) {
    if ($NoShortcut) { return }
    try {
        $path = Join-Path ([Environment]::GetFolderPath("Desktop")) "VAYUNX Profiler.url"
        # An .url file is plain INI, so no COM and no Windows Script Host needed.
        Set-Content -Path $path -Encoding ascii -Value @(
            "[InternetShortcut]",
            "URL=$Url",
            "IconIndex=0"
        )
    } catch { Write-Note "could not write the desktop shortcut: $($_.Exception.Message)" "WARN" }
}

function Start-QuickTunnel {
    $exe = Find-Cloudflared
    Get-QuickTunnelProcess | ForEach-Object {
        Write-Note "stopping the old quick tunnel (PID $($_.ProcessId))"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
    if (Test-Path $tunLog) { Move-Item -Path $tunLog -Destination (Join-Path $logs "quick-tunnel.prev.log") -Force }

    Start-Process -FilePath $exe -WindowStyle Hidden -ArgumentList @(
        "tunnel", "--no-autoupdate", "--url", $uiUrl.TrimEnd('/'), "--logfile", $tunLog
    ) | Out-Null

    # cloudflared prints the hostname once the tunnel is registered; give it a minute.
    $url = $null
    foreach ($i in 1..60) {
        Start-Sleep -Seconds 1
        if (-not (Test-Path $tunLog)) { continue }
        $text = Get-Content $tunLog -Raw -ErrorAction SilentlyContinue
        if ($text -and $text -match "Registered tunnel connection") {
            # Read the hostname with an explicit match: -match writes $Matches, so a second test would clobber it.
            $hit = [regex]::Match($text, "https://[a-z0-9-]+\.trycloudflare\.com")
            if ($hit.Success) { $url = $hit.Value; break }
        }
    }
    if (-not $url) { Write-Note "the new tunnel did not register within 60s; see $tunLog" "ERROR"; return $null }

    # Cloudflare needs a moment to route the fresh hostname.
    foreach ($i in 1..15) { if (Test-Url $url 15) { break }; Start-Sleep -Seconds 2 }
    return $url
}

# One watchdog at a time: the task fires every couple of minutes and a restart takes longer than that.
$mutex = New-Object System.Threading.Mutex($false, "Global\VayunxTunnelWatchdog")
if (-not $mutex.WaitOne(0)) { Write-Output "another watchdog run is in progress; nothing to do"; exit 0 }

try {
    # 1. The app itself. Nothing public can work while this is down, and it is the cheaper check.
    $uiUp = Test-Url $uiUrl 10
    if (-not $uiUp) {
        Write-Note "the UI is not answering on 127.0.0.1:3000; starting the app" "WARN"
        & (Join-Path $root "ops\start-vayunx.ps1") | ForEach-Object { Write-Note "start-vayunx: $_" }
        $uiUp = Test-Url $uiUrl 15
        if (-not $uiUp) { Write-Note "the app is still not answering; leaving the tunnel alone" "ERROR"; exit 1 }
    }

    # 2. The public URL. A live tunnel process means nothing - it retries a dead registration forever.
    $url     = Get-RecordedUrl
    $healthy = (-not $Force) -and (Test-Url $url)

    if ($healthy) {
        # The tunnel may be one this run did not start (a manual cloudflared, or a stale record), so keep
        # the recorded URL and the shortcut in step with whatever is actually answering.
        $recorded = if (Test-Path $urlFile) { (Get-Content $urlFile -TotalCount 1).Trim() } else { "" }
        if ($recorded -ne $url) {
            Set-Content -Path $urlFile -Value $url -Encoding ascii
            Update-Shortcut $url
            Write-Note "recorded the current URL: $url"
        }
        Write-Output "ok: $url"
        exit 0
    }

    $why = if ($Force) { "forced" } elseif ($url) { "$url stopped answering" } else { "no tunnel recorded yet" }
    Write-Note "replacing the quick tunnel ($why)" "WARN"

    $new = Start-QuickTunnel
    if (-not $new) { exit 1 }

    Set-Content -Path $urlFile -Value $new -Encoding ascii
    Update-Shortcut $new
    Write-Note "new public URL: $new"
    Write-Output $new
    exit 0
}
finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
