param(
    [string]$AviatorUrl = "https://www.betpawa.cm/",
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Require-Command {
    param([string]$Name, [string]$InstallHint)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "$Name is required. $InstallHint"
    }
}

function Get-TailscaleStatus {
    $json = & tailscale status --json 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $json) {
        return $null
    }
    return ($json | ConvertFrom-Json)
}

function New-RandomHex {
    param([int]$Bytes = 32)
    $buffer = New-Object byte[] $Bytes
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($buffer)
    return ([Convert]::ToHexString($buffer)).ToLowerInvariant()
}

function Invoke-Compose {
    param([string[]]$Arguments)
    & docker compose --env-file .env.tailscale -f docker-compose.tailscale.yml @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose failed: $($Arguments -join ' ')"
    }
}

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Push-Location $repoRoot
try {
    Require-Command "docker" "Install Docker Desktop for Windows, start it, then rerun this script."
    Require-Command "tailscale" "Install Tailscale for Windows, sign in, then rerun this script."

    & docker info *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Desktop is installed but its engine is not running. Start Docker Desktop and retry."
    }

    $status = Get-TailscaleStatus
    if ($null -eq $status -or $status.BackendState -ne "Running" -or -not $status.Self.Online) {
        Write-Host "Tailscale is not connected. Starting Tailscale login..." -ForegroundColor Yellow
        & tailscale up
        if ($LASTEXITCODE -ne 0) {
            throw "Tailscale login did not complete. Open the Tailscale app, sign in, then rerun this script."
        }
        $status = Get-TailscaleStatus
    }

    if ($null -eq $status -or -not $status.Self.DNSName) {
        throw "Could not determine this computer's Tailscale DNS name."
    }

    $tailHost = ([string]$status.Self.DNSName).TrimEnd(".")
    if (-not $tailHost.EndsWith(".ts.net")) {
        throw "Unexpected Tailscale DNS name: $tailHost"
    }

    $envPath = Join-Path $repoRoot ".env.tailscale"
    $token = New-RandomHex 32
    if (Test-Path $envPath) {
        $existing = Get-Content $envPath -Raw
        $match = [regex]::Match($existing, "(?m)^AIE_BROWSERLESS_TOKEN=(.+)$")
        if ($match.Success -and $match.Groups[1].Value.Trim() -and
            $match.Groups[1].Value.Trim() -ne "replace-with-a-long-random-token") {
            $token = $match.Groups[1].Value.Trim()
        }
    }

    if (-not $AviatorUrl.StartsWith("https://")) {
        throw "AviatorUrl must be an HTTPS URL."
    }

    @"
AIE_TAILSCALE_HOST=$tailHost
AIE_BROWSERLESS_TOKEN=$token
AIE_BETPAWA_AVIATOR_URL=$AviatorUrl
AIE_DATABASE_FILENAME=aie.sqlite3
AIE_LOG_LEVEL=INFO
"@ | Set-Content -Path $envPath -Encoding utf8

    Write-Host "Starting persistent Browserless services..." -ForegroundColor Cyan
    if ($SkipBuild) {
        Invoke-Compose @("up", "-d", "browserless", "browser-proxy")
    } else {
        Invoke-Compose @("up", "-d", "--build", "browserless", "browser-proxy")
    }

    Write-Host "Initializing AIE database..." -ForegroundColor Cyan
    Invoke-Compose @("run", "--rm", "aie", "python", "main.py")

    Write-Host "Starting AIE dashboard..." -ForegroundColor Cyan
    if ($SkipBuild) {
        Invoke-Compose @("up", "-d", "aie")
    } else {
        Invoke-Compose @("up", "-d", "--build", "aie")
    }

    Write-Host "Configuring private Tailscale HTTPS access..." -ForegroundColor Cyan
    & tailscale serve --bg --yes --https=443 http://127.0.0.1:8000
    if ($LASTEXITCODE -ne 0) {
        throw "Could not configure Tailscale Serve on HTTPS 443. Run 'tailscale serve status' to check for an existing listener."
    }

    & tailscale serve --bg --yes --https=8443 http://127.0.0.1:8081
    if ($LASTEXITCODE -ne 0) {
        throw "Dashboard is running, but Browserless could not be published on HTTPS 8443. Run 'tailscale serve status' and resolve any existing listener."
    }

    $dashboardUrl = "https://$tailHost/"
    $browserUrl = "https://$($tailHost):8443/"

    Write-Host ""
    Write-Host "AIE FREE HOST IS READY" -ForegroundColor Green
    Write-Host "Dashboard: $dashboardUrl"
    Write-Host "Authenticated browser: $browserUrl"
    Write-Host ""
    Write-Host "Install Tailscale on each phone/computer you want to use, sign into the same tailnet, then open these URLs."
    Write-Host "The host PC must stay powered on, connected to the internet, and awake."
    Write-Host ""
    Write-Host "Next: open the authenticated browser URL, log into BetPawa manually, open Aviator, then run:"
    Write-Host "  .\deploy\windows\start-collector.ps1"
}
finally {
    Pop-Location
}
