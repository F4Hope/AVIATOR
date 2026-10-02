$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Push-Location $repoRoot
try {
    if (-not (Test-Path ".env.tailscale")) {
        throw ".env.tailscale is missing. Run .\deploy\windows\setup-aie.ps1 first."
    }

    & docker compose --env-file .env.tailscale -f docker-compose.tailscale.yml run --rm aie python inspect_aviator_probe.py
    if ($LASTEXITCODE -ne 0) {
        throw "Probe inspection failed."
    }
}
finally {
    Pop-Location
}
