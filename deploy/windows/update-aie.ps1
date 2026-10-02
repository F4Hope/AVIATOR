$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Push-Location $repoRoot
try {
    if (-not (Test-Path ".env.tailscale")) {
        throw ".env.tailscale is missing. Run .\deploy\windows\setup-aie.ps1 first."
    }

    & git checkout main
    if ($LASTEXITCODE -ne 0) { throw "Could not switch to main." }

    & git pull --ff-only
    if ($LASTEXITCODE -ne 0) { throw "Git update failed." }

    & docker compose --env-file .env.tailscale -f docker-compose.tailscale.yml build --pull
    if ($LASTEXITCODE -ne 0) { throw "Docker build failed." }

    & docker compose --env-file .env.tailscale -f docker-compose.tailscale.yml up -d browserless browser-proxy aie
    if ($LASTEXITCODE -ne 0) { throw "AIE services failed to restart." }

    $collector = & docker compose --env-file .env.tailscale -f docker-compose.tailscale.yml --profile collector ps --status running --services
    if ($collector -contains "collector") {
        & docker compose --env-file .env.tailscale -f docker-compose.tailscale.yml --profile collector up -d --force-recreate collector
    }

    Write-Host "AIE updated." -ForegroundColor Green
}
finally {
    Pop-Location
}
