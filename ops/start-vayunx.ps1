# Starts the VAYUNX Crypto Profiler on this machine: the Service (8010) and the UI (3000).
# Both listen on 127.0.0.1 only, so they are reachable through the Cloudflare Tunnel and nothing else.
# Run it by hand, or let the "VAYUNX Profiler" scheduled task run it at sign-in (see docs/HOSTING.md).

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$logs = Join-Path $root "ops\logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null

function Test-Port([int]$Port) {
    [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

# 1. Profiler Service (FastAPI). One worker: the Lab runs one experiment at a time.
if (Test-Port 8010) {
    "Service already listening on 8010"
} else {
    Start-Process -FilePath "$root\.venv\Scripts\python.exe" -ArgumentList "-m", "profiler_service" `
        -WorkingDirectory $root -WindowStyle Hidden `
        -RedirectStandardOutput "$logs\service.out.log" -RedirectStandardError "$logs\service.err.log" | Out-Null
    "Started Service on 127.0.0.1:8010"
}

# 2. UI (Next.js, production build). It proxies /api to the Service, so one hostname serves both.
if (Test-Port 3000) {
    "UI already listening on 3000"
} else {
    Start-Process -FilePath "cmd.exe" -ArgumentList "/c", "npx next start -p 3000 -H 127.0.0.1 > `"$logs\ui.log`" 2>&1" `
        -WorkingDirectory "$root\web" -WindowStyle Hidden | Out-Null
    "Started UI on 127.0.0.1:3000"
}

# 3. Wait for both to answer, so a failure shows up here rather than in the browser.
foreach ($check in @(@{ Name = "Service"; Url = "http://127.0.0.1:8010/v2/presets" }, @{ Name = "UI"; Url = "http://127.0.0.1:3000/" })) {
    $ok = $false
    foreach ($i in 1..45) {
        try {
            if ((Invoke-WebRequest -UseBasicParsing -Uri $check.Url -TimeoutSec 5).StatusCode -eq 200) { $ok = $true; break }
        } catch { Start-Sleep -Seconds 2 }
    }
    if ($ok) { "$($check.Name) is ready" } else { Write-Warning "$($check.Name) did not answer at $($check.Url); see $logs" }
}
