$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Push-Location $repoRoot
try {
    $python = Join-Path $repoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $python)) {
        throw "AIE virtual environment is missing. Run .\deploy\windows\setup-native-aie.ps1 first."
    }

    $edgeCandidates = @(
        "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "C:\Program Files\Microsoft\Edge\Application\msedge.exe"
    )
    $edge = $edgeCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $edge) {
        throw "Microsoft Edge could not be found."
    }

    $profileDir = Join-Path $repoRoot "data\browser-profile"
    New-Item -ItemType Directory -Path $profileDir -Force | Out-Null

    $rawDir = Join-Path $repoRoot "data\raw"
    New-Item -ItemType Directory -Path $rawDir -Force | Out-Null
    $probePath = Join-Path $rawDir "aviator-network-probe.jsonl"
    if (Test-Path $probePath) {
        $archiveDir = Join-Path $rawDir "archive"
        New-Item -ItemType Directory -Path $archiveDir -Force | Out-Null
        $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
        $archivePath = Join-Path $archiveDir "aviator-network-probe-$stamp.jsonl"
        Move-Item -Path $probePath -Destination $archivePath
        Write-Host "Archived previous probe: $archivePath" -ForegroundColor DarkGray
    }

    $targetUrl = $null
    if (Test-Path ".env") {
        $line = Get-Content ".env" | Where-Object { $_ -match '^AIE_BETPAWA_AVIATOR_URL=' } | Select-Object -First 1
        if ($line) {
            $targetUrl = $line.Substring("AIE_BETPAWA_AVIATOR_URL=".Length).Trim()
        }
    }
    if (-not $targetUrl) {
        $targetUrl = "https://www.betpawa.cm/"
    }

    Write-Host "Starting normal Microsoft Edge for AIE..." -ForegroundColor Cyan
    Write-Host "This browser is not launched by Playwright." -ForegroundColor Green
    Write-Host "Log into BetPawa manually if needed and open Aviator." -ForegroundColor Yellow

    $edgeArgs = @(
        "--remote-debugging-address=127.0.0.1",
        "--remote-debugging-port=9222",
        "--user-data-dir=$profileDir",
        "--no-first-run",
        "--no-default-browser-check",
        $targetUrl
    )
    Start-Process -FilePath $edge -ArgumentList $edgeArgs

    Write-Host "Waiting for Edge debugging endpoint..." -ForegroundColor Cyan
    $ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:9222/json/version" -TimeoutSec 2
            if ($response.StatusCode -eq 200) {
                $ready = $true
                break
            }
        }
        catch {}
        Start-Sleep -Seconds 1
    }
    if (-not $ready) {
        throw "Edge did not expose the local debugging endpoint on 127.0.0.1:9222."
    }

    Write-Host "Attaching AIE collector to normal Edge..." -ForegroundColor Cyan
    & $python probe_aviator_native.py --cdp-url http://127.0.0.1:9222
}
finally {
    Pop-Location
}
