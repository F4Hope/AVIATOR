param(
    [string]$RepoUrl = "https://github.com/F4Hope/AVIATOR.git",
    [string]$RepoPath = "$HOME\AVIATOR",
    [string]$AviatorUrl = "https://www.betpawa.cm/"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

function Find-CommandPath {
    param([string]$Command, [string[]]$Fallbacks)
    $found = Get-Command $Command -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    foreach ($path in $Fallbacks) {
        if (Test-Path $path) { return $path }
    }
    return $null
}

function Install-PackageIfMissing {
    param(
        [string]$DisplayName,
        [string]$CommandName,
        [string]$WingetId,
        [string[]]$Fallbacks
    )

    $path = Find-CommandPath -Command $CommandName -Fallbacks $Fallbacks
    if ($path) {
        Write-Host "$DisplayName already installed." -ForegroundColor Green
        return $path
    }

    Write-Host "Installing $DisplayName..." -ForegroundColor Cyan
    & winget install --id $WingetId -e --accept-package-agreements --accept-source-agreements --silent
    if ($LASTEXITCODE -ne 0) {
        throw "winget could not install $DisplayName (exit $LASTEXITCODE)."
    }

    Refresh-Path
    $path = Find-CommandPath -Command $CommandName -Fallbacks $Fallbacks
    if (-not $path) {
        throw "$DisplayName was installed but is not available yet. Restart Windows and rerun this script."
    }
    return $path
}

if ($env:OS -ne "Windows_NT") {
    throw "This bootstrap script is for Windows only."
}

if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw "Windows Package Manager (winget) is required. Install App Installer from Microsoft Store, then rerun."
}

if (-not (Test-Administrator)) {
    Write-Host "Administrator permission is required. Windows will show a UAC prompt." -ForegroundColor Yellow
    $arguments = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", ('"{0}"' -f $PSCommandPath),
        "-RepoUrl", ('"{0}"' -f $RepoUrl),
        "-RepoPath", ('"{0}"' -f $RepoPath),
        "-AviatorUrl", ('"{0}"' -f $AviatorUrl)
    )
    Start-Process powershell.exe -Verb RunAs -ArgumentList $arguments
    exit 0
}

Refresh-Path

$git = Install-PackageIfMissing -DisplayName "Git for Windows" -CommandName "git" -WingetId "Git.Git" -Fallbacks @("C:\Program Files\Git\cmd\git.exe")
$pythonLauncher = Install-PackageIfMissing -DisplayName "Python 3.12" -CommandName "py" -WingetId "Python.Python.3.12" -Fallbacks @("C:\Windows\py.exe")
$tailscale = Install-PackageIfMissing -DisplayName "Tailscale" -CommandName "tailscale" -WingetId "Tailscale.Tailscale" -Fallbacks @("C:\Program Files\Tailscale\tailscale.exe")

Write-Host ""
Write-Host "Preparing AVIATOR repository..." -ForegroundColor Cyan

if (Test-Path (Join-Path $RepoPath ".git")) {
    Push-Location $RepoPath
    try {
        & $git checkout main
        if ($LASTEXITCODE -ne 0) { throw "Could not switch AVIATOR to main." }
        & $git pull --ff-only
        if ($LASTEXITCODE -ne 0) { throw "Could not update AVIATOR." }
    }
    finally {
        Pop-Location
    }
}
else {
    if (Test-Path $RepoPath) {
        $children = @(Get-ChildItem -Force $RepoPath)
        if ($children.Count -gt 0) {
            throw "RepoPath exists and is not an AVIATOR Git repository: $RepoPath"
        }
        Remove-Item $RepoPath -Force
    }
    & $git clone $RepoUrl $RepoPath
    if ($LASTEXITCODE -ne 0) { throw "Could not clone AVIATOR from GitHub." }
}

Write-Host "Checking Tailscale..." -ForegroundColor Cyan
$statusJson = & $tailscale status --json 2>$null
$tailscaleReady = $false
if ($LASTEXITCODE -eq 0 -and $statusJson) {
    try {
        $status = $statusJson | ConvertFrom-Json
        $tailscaleReady = ($status.BackendState -eq "Running" -and $status.Self.Online)
    }
    catch {
        $tailscaleReady = $false
    }
}

if (-not $tailscaleReady) {
    $tailscaleGui = "C:\Program Files\Tailscale\tailscale-ipn.exe"
    if (Test-Path $tailscaleGui) {
        Start-Process $tailscaleGui
    }

    Write-Host ""
    Write-Host "Tailscale is installed but this PC is not signed in yet." -ForegroundColor Yellow
    Write-Host "Sign in to Tailscale, then run:" -ForegroundColor Yellow
    Write-Host ('  cd "{0}"' -f $RepoPath)
    Write-Host "  Set-ExecutionPolicy -Scope Process Bypass -Force"
    Write-Host "  .\deploy\windows\setup-native-aie.ps1"
    exit 0
}

Write-Host ""
Write-Host "Git, Python, and Tailscale are ready." -ForegroundColor Green
Write-Host "Starting Docker-free AIE setup..." -ForegroundColor Cyan

Push-Location $RepoPath
try {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\deploy\windows\setup-native-aie.ps1" -AviatorUrl $AviatorUrl
    if ($LASTEXITCODE -ne 0) {
        throw "Native AIE setup returned exit code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}
