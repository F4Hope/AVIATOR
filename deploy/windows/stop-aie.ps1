param(
    [switch]$RemoveData
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Push-Location $repoRoot
try {
    if (-not (Test-Path ".env.tailscale")) {
        throw ".env.tailscale is missing. Run .\deploy\windows\setup-aie.ps1 first."
    }

    & tailscale serve --https=443 off
    & tailscale serve --https=8443 off

    if ($RemoveData) {
        Write-Warning "This will delete the persistent AIE database and Browserless profile volumes."
        $confirmation = Read-Host "Type DELETE to continue"
        if ($confirmation -ne "DELETE") {
            Write-Host "Data deletion cancelled."
            exit 0
        }
        & docker compose --env-file .env.tailscale -f docker-compose.tailscale.yml --profile collector down -v
    } else {
        & docker compose --env-file .env.tailscale -f docker-compose.tailscale.yml --profile collector down
    }

    if ($LASTEXITCODE -ne 0) {
        throw "AIE services did not stop cleanly."
    }

    Write-Host "AIE stopped." -ForegroundColor Green
    if (-not $RemoveData) {
        Write-Host "Persistent database and browser profile were preserved."
    }
}
finally {
    Pop-Location
}
