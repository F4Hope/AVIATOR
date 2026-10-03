$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Push-Location $repoRoot
try {
    $python = Join-Path $repoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $python)) {
        throw "AIE virtual environment is missing. Run .\deploy\windows\setup-native-aie.ps1 first."
    }

    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:9222/json/version" -TimeoutSec 3
        if ($response.StatusCode -ne 200) {
            throw "unexpected status"
        }
    }
    catch {
        throw "No AIE Edge debug endpoint is available. Run .\deploy\windows\launch-native-browser.ps1 first."
    }

    $rawDir = Join-Path $repoRoot "data\raw"
    New-Item -ItemType Directory -Path $rawDir -Force | Out-Null
    $probePath = Join-Path $rawDir "aviator-network-probe.jsonl"
    $markerPath = Join-Path $rawDir "aviator-round-markers.jsonl"
    $domPath = Join-Path $rawDir "aviator-dom-multipliers.jsonl"
    $archiveDir = Join-Path $rawDir "archive"
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"

    if ((Test-Path $probePath) -or (Test-Path $markerPath) -or (Test-Path $domPath)) {
        New-Item -ItemType Directory -Path $archiveDir -Force | Out-Null
    }
    if (Test-Path $probePath) {
        $archivePath = Join-Path $archiveDir "aviator-network-probe-$stamp.jsonl"
        Move-Item -Path $probePath -Destination $archivePath
        Write-Host "Archived previous probe: $archivePath" -ForegroundColor DarkGray
    }
    if (Test-Path $markerPath) {
        $markerArchivePath = Join-Path $archiveDir "aviator-round-markers-$stamp.jsonl"
        Move-Item -Path $markerPath -Destination $markerArchivePath
        Write-Host "Archived previous markers: $markerArchivePath" -ForegroundColor DarkGray
    }
    if (Test-Path $domPath) {
        $domArchivePath = Join-Path $archiveDir "aviator-dom-multipliers-$stamp.jsonl"
        Move-Item -Path $domPath -Destination $domArchivePath
        Write-Host "Archived previous DOM multipliers: $domArchivePath" -ForegroundColor DarkGray
    }

    Write-Host "Attaching AIE to the already-running Edge session..." -ForegroundColor Cyan
    Write-Host "Watch the Aviator page while this attaches." -ForegroundColor Yellow
    & $python probe_aviator_native.py --cdp-url http://127.0.0.1:9222
}
finally {
    Pop-Location
}
