$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Push-Location $repoRoot
try {
    if (-not (Test-Path ".env.tailscale")) {
        throw ".env.tailscale is missing. Run .\deploy\windows\setup-aie.ps1 first."
    }

    & docker compose --env-file .env.tailscale -f docker-compose.tailscale.yml --profile collector up -d collector predictor
    if ($LASTEXITCODE -ne 0) {
        throw "Collector/predictor services failed to start."
    }

    Write-Host "AIE collector and live predictor are running." -ForegroundColor Green
    Write-Host "Follow logs with:"
    Write-Host "  docker compose --env-file .env.tailscale -f docker-compose.tailscale.yml logs -f collector predictor"
    Write-Host ""
    Write-Host "After several Aviator rounds, inspect sanitized traffic with:"
    Write-Host "  .\deploy\windows\inspect-probe.ps1"
}
finally {
    Pop-Location
}
