$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Push-Location $repoRoot
try {
    if (-not (Test-Path ".env.tailscale")) {
        throw ".env.tailscale is missing. Run .\deploy\windows\setup-aie.ps1 first."
    }

    Write-Host "Docker services" -ForegroundColor Cyan
    & docker compose --env-file .env.tailscale -f docker-compose.tailscale.yml --profile collector ps

    Write-Host ""
    Write-Host "Tailscale Serve" -ForegroundColor Cyan
    & tailscale serve status

    Write-Host ""
    Write-Host "Tailscale device" -ForegroundColor Cyan
    $status = & tailscale status --json | ConvertFrom-Json
    $dnsName = ([string]$status.Self.DNSName).TrimEnd(".")
    Write-Host "Dashboard: https://$dnsName/"
    Write-Host "Browser:   https://$($dnsName):8443/"
}
finally {
    Pop-Location
}
