# Free Windows + Tailscale hosting

This is the recommended no-card deployment for AIE.

One Windows computer acts as the always-on host. Docker Desktop runs AIE,
SQLite, Browserless Chromium, and the collector. Tailscale provides private
HTTPS access from the user's other computers and phones.

Nothing in this mode is exposed to the public internet.

## What runs where

Host Windows PC:

- Docker Desktop
- AIE dashboard and SQLite database
- Browserless Chromium with persistent browser data
- optional Aviator discovery collector
- Tailscale

Remote phone/laptop:

- Tailscale app
- ordinary web browser

The remote device does not need Python, Docker, Git, or the AIE source code.

## Prerequisites on the host PC

Install:

1. Git for Windows.
2. Docker Desktop with the Linux/WSL2 backend.
3. Tailscale for Windows.

Sign into Tailscale on the host PC.

Clone the repository and open PowerShell in the repository root.

## First-time setup

Update the repository first:

```powershell
git checkout main
git pull
```

Run:

```powershell
.\deploy\windows\setup-aie.ps1
```

The script:

- verifies Docker Desktop is running;
- verifies Tailscale is connected;
- reads the host's Tailscale DNS name;
- generates a private random Browserless token locally;
- writes it to the ignored `.env.tailscale` file;
- initializes the SQLite database;
- starts persistent Browserless and AIE containers;
- binds local container ports only to `127.0.0.1`;
- publishes the dashboard privately with Tailscale Serve HTTPS 443;
- publishes the protected Browserless UI privately on HTTPS 8443.

At completion it prints two URLs similar to:

```text
Dashboard: https://host-name.tailnet-name.ts.net/
Browser:   https://host-name.tailnet-name.ts.net:8443/
```

Tailscale Serve is private to the tailnet and is not Tailscale Funnel.

## Access from a phone or another computer

Install Tailscale on that device and sign into the same tailnet.

Then open the Dashboard URL printed by the setup script.

This works on Windows, macOS, Linux, Android, and iPhone/iPad through a normal
browser as long as that device can join the same Tailscale network.

## BetPawa authentication

Open the Browser URL printed by the setup script.

Log into BetPawa manually. Do not put the BetPawa password, PIN, cookies,
authorization tokens, or session values into AIE source code or environment
files.

Open Aviator in that persistent browser session.

Then start sanitized collection:

```powershell
.\deploy\windows\start-collector.ps1
```

After several completed rounds:

```powershell
.\deploy\windows\inspect-probe.ps1
```

The inspection output is intentionally value-free. It helps identify the real
round/result message before a provider-specific parser is written.

## Status

```powershell
.\deploy\windows\status-aie.ps1
```

## Update AIE

```powershell
.\deploy\windows\update-aie.ps1
```

## Stop AIE

Preserve the database and browser profile:

```powershell
.\deploy\windows\stop-aie.ps1
```

Delete persistent data only when deliberately requested:

```powershell
.\deploy\windows\stop-aie.ps1 -RemoveData
```

That command requires typing `DELETE` before Docker volumes are removed.

## Keep the host available

The Windows host must remain:

- powered on;
- connected to the internet;
- signed into Tailscale;
- running Docker Desktop;
- awake.

Configure Windows power settings so the PC does not sleep while plugged in if
continuous collection is required.

## Security

The Compose stack publishes only:

- `127.0.0.1:8000` for AIE;
- `127.0.0.1:8081` for the local Browserless reverse proxy.

These loopback ports are not reachable directly from other LAN or internet
devices. Tailscale Serve is the remote-access layer.

The Browserless API token is stored only in the ignored local
`.env.tailscale` file and injected by the local reverse proxy. It is not
needed in the browser URL.

`.dockerignore` prevents local environment files, databases, browser data,
and other secrets from entering Docker build contexts.

This deployment still does not automate betting, deposits, withdrawals,
credential entry, or CAPTCHA bypasses.
