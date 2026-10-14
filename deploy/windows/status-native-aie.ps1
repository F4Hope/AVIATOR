$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Push-Location $repoRoot
try {
    $status = & tailscale status --json | ConvertFrom-Json
    $dnsName = ([string]$status.Self.DNSName).TrimEnd(".")
    Write-Host "Tailscale device: $dnsName"
    Write-Host "Dashboard: https://$dnsName/"
    Write-Host ""
    Write-Host "Tailscale Serve:"
    & tailscale serve status
    Write-Host ""
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8000/api/status" -Headers @{ Host = $dnsName } -TimeoutSec 2
        Write-Host "Dashboard local status: $($response.StatusCode)" -ForegroundColor Green
    }
    catch {
        Write-Host "Dashboard local status: unavailable" -ForegroundColor Yellow
    }

    $predictor = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine -like "*run_live_predictions.py*" -and
            $_.CommandLine -like "*$repoRoot*"
        } |
        Select-Object -First 1
    if ($predictor) {
        Write-Host "Live predictor: running (PID $($predictor.ProcessId))" -ForegroundColor Green
    }
    else {
        Write-Host "Live predictor: not running" -ForegroundColor Yellow
    }
}
finally {
    Pop-Location
}
