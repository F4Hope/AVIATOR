param(
    [string]$AviatorUrl = "https://www.betpawa.cm/"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Require-Command {
    param([string]$Name, [string]$Hint)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "$Name is required. $Hint"
    }
}

function Get-TailscaleStatus {
    $json = & tailscale status --json 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $json) { return $null }
    return ($json | ConvertFrom-Json)
}

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Push-Location $repoRoot
try {
    Require-Command "tailscale" "Install Tailscale for Windows and sign in."
    Require-Command "py" "Install Python 3.12 or newer for Windows."

    $versionText = & py -3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    if ($LASTEXITCODE -ne 0) { throw "Python could not be started." }
    $parts = $versionText.Trim().Split(".")
    if ([int]$parts[0] -lt 3 -or ([int]$parts[0] -eq 3 -and [int]$parts[1] -lt 12)) {
        throw "Python 3.12 or newer is required."
    }

    $status = Get-TailscaleStatus
    if ($null -eq $status -or $status.BackendState -ne "Running" -or -not $status.Self.Online) {
        Write-Host "Tailscale is not connected. Starting login..." -ForegroundColor Yellow
        & tailscale up
        if ($LASTEXITCODE -ne 0) {
            throw "Tailscale login did not complete."
        }
        $status = Get-TailscaleStatus
    }

    $tailHost = ([string]$status.Self.DNSName).TrimEnd(".")
    if (-not $tailHost.EndsWith(".ts.net")) {
        throw "Could not determine a valid Tailscale DNS hostname."
    }

    if (-not $AviatorUrl.StartsWith("https://")) {
        throw "AviatorUrl must use HTTPS."
    }

    $envPath = Join-Path $repoRoot ".env"
    @"
AIE_ENVIRONMENT=PRODUCTION
AIE_LOG_LEVEL=INFO
AIE_DATABASE_FILENAME=aie.sqlite3
AIE_BETPAWA_AVIATOR_URL=$AviatorUrl
"@ | Set-Content -Path $envPath -Encoding utf8

    if (-not (Test-Path ".venv\Scripts\python.exe")) {
        Write-Host "Creating Python virtual environment..." -ForegroundColor Cyan
        & py -3 -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw "Could not create virtual environment." }
    }

    $python = Join-Path $repoRoot ".venv\Scripts\python.exe"

    Write-Host "Installing AIE Python dependencies..." -ForegroundColor Cyan
    & $python -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }
    & $python -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }

    Write-Host "Initializing AIE database..." -ForegroundColor Cyan
    & $python main.py
    if ($LASTEXITCODE -ne 0) { throw "AIE initialization failed." }

    Write-Host "Starting native dashboard..." -ForegroundColor Cyan
    $dashboardArgs = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-Command",
        ('Set-Location "{0}"; & "{1}" run_dashboard.py --host 127.0.0.1 --port 8000 --public-host "{2}"' -f $repoRoot, $python, $tailHost)
    )
    Start-Process powershell.exe -WindowStyle Minimized -ArgumentList $dashboardArgs

    $dashboardReady = $false
    for ($i = 0; $i -lt 30; $i++) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8000/api/status" -Headers @{ Host = $tailHost } -TimeoutSec 2
            if ($response.StatusCode -eq 200) {
                $dashboardReady = $true
                break
            }
        }
        catch {}
        Start-Sleep -Seconds 1
    }
    if (-not $dashboardReady) {
        throw "Dashboard did not become ready on localhost:8000."
    }

    Write-Host "Publishing dashboard privately through Tailscale..." -ForegroundColor Cyan
    & tailscale serve --bg --yes --https=443 http://127.0.0.1:8000
    if ($LASTEXITCODE -ne 0) {
        throw "Could not configure Tailscale Serve."
    }

    Write-Host ""
    Write-Host "AIE NATIVE WINDOWS HOST IS READY" -ForegroundColor Green
    Write-Host "Dashboard: https://$tailHost/"
    Write-Host ""
    Write-Host "Start the authenticated Aviator browser/collector with:"
    Write-Host "  .\deploy\windows\start-native-collector.ps1"
    Write-Host ""
    Write-Host "The collector opens a separate persistent Microsoft Edge profile."
    Write-Host "Log into BetPawa manually in that Edge window and leave it open while collecting."
}
finally {
    Pop-Location
}
