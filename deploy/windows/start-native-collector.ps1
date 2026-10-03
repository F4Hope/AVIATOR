$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Push-Location $repoRoot
try {
    $python = Join-Path $repoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $python)) {
        throw "AIE virtual environment is missing. Run .\deploy\windows\setup-native-aie.ps1 first."
    }

    Write-Host "Opening persistent Microsoft Edge for Aviator..." -ForegroundColor Cyan
    Write-Host "Log into BetPawa manually if needed, open Aviator, and keep this window running."
    & $python probe_aviator_native.py --browser msedge
}
finally {
    Pop-Location
}
