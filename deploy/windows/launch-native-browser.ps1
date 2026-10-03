$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Push-Location $repoRoot
try {
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

    Write-Host "Launching Edge with AIE profile and local debug port only..." -ForegroundColor Cyan
    Write-Host "No AIE collector will attach yet." -ForegroundColor Green
    Write-Host "Log in, open Aviator, and wait until the game is visibly running." -ForegroundColor Yellow

    $edgeArgs = @(
        "--remote-debugging-address=127.0.0.1",
        "--remote-debugging-port=9222",
        "--user-data-dir=$profileDir",
        "--no-first-run",
        "--no-default-browser-check",
        $targetUrl
    )
    Start-Process -FilePath $edge -ArgumentList $edgeArgs

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

    Write-Host ""
    Write-Host "Edge is ready. AIE is NOT attached." -ForegroundColor Green
    Write-Host "When Aviator is fully running, execute:" -ForegroundColor Yellow
    Write-Host "  .\deploy\windows\attach-native-collector.ps1" -ForegroundColor White
}
finally {
    Pop-Location
}
