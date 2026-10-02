# AIE hosted deployment

This deployment is designed for an always-on Linux VPS with Docker Compose,
two DNS hostnames, and ports 80/443 reachable from the internet.

Example hostnames:

- `app.example.com` — AIE dashboard
- `browser.example.com` — protected Browserless debugger/browser UI

Both public sites are protected by HTTPS and Caddy Basic Auth. Browserless also
has its own long random API token; Caddy injects that token server-side so it
does not need to appear in the browser URL.

## 1. Server prerequisites

Install Docker Engine and the Docker Compose plugin. Clone the repository on the
server and enter its directory.

The server should have persistent disk storage. The Compose stack creates named
volumes for:

- AIE SQLite/raw/processed data
- Browserless browser profile/session data
- Caddy certificates and configuration state

Back up the Docker volumes independently of the repository.

## 2. DNS

Create DNS A/AAAA records pointing both hostnames to the VPS.

Caddy obtains and renews public TLS certificates automatically when the DNS
records point to the server and ports 80/443 are reachable.

## 3. Secrets and deployment configuration

Copy the template:

```bash
cp deploy/.env.example .env
```

Generate a long Browserless token:

```bash
openssl rand -hex 32
```

Generate the dashboard/browser Basic Auth password hash:

```bash
docker run --rm caddy:2-alpine \
  caddy hash-password --plaintext 'CHOOSE-A-LONG-PASSWORD'
```

Put only the generated hash in `AIE_BASIC_AUTH_HASH`. Keep the plaintext
password outside the repository.

Set `AIE_BETPAWA_AVIATOR_URL` to the exact HTTPS Aviator page you use. Do not
put BetPawa credentials, cookies, PINs, or session tokens in `.env`.

## 4. Start the persistent browser and dashboard

```bash
docker compose up -d --build browserless aie caddy
docker compose ps
```

Open:

```text
https://browser.example.com
```

Enter the Caddy Basic Auth credentials, then use Browserless's debugger/browser
UI. Navigate to BetPawa and log in manually yourself. Do not paste the BetPawa
password/PIN into AIE source code or environment variables.

Browserless uses a persistent Docker volume for its user-data directory, so
cookies/local storage can survive service restarts.

## 5. Start sanitized Aviator discovery

After the server-side browser profile is authenticated and can open Aviator:

```bash
docker compose --profile collector up -d collector
docker compose logs -f collector
```

The collector writes only sanitized discovery metadata to:

```text
data/raw/aviator-network-probe.jsonl
```

Inside the AIE data volume. It stores endpoint paths without query strings,
frame hashes/sizes, content types, and JSON key paths. It does not intentionally
store raw WebSocket/JSON values, cookies, authorization headers, passwords,
Browserless tokens, or BetPawa credentials.

Inspect the sanitized structure:

```bash
docker compose run --rm aie python inspect_aviator_probe.py
```

Use this output to identify the real observable round/result message before
writing a provider-specific parser. Do not guess endpoint names or payload
fields.

## 6. Dashboard access

Open:

```text
https://app.example.com
```

The dashboard remains read-only. SQLite and collected data stay on the server,
so you can access the same state from phone or desktop.

## 7. Updating

```bash
git pull
docker compose build --pull
docker compose up -d
```

If dependencies or the collector changed, restart it explicitly:

```bash
docker compose --profile collector up -d --force-recreate collector
```

## 8. Backups

The existing AIE backup command still produces a verified SQLite backup:

```bash
docker compose run --rm aie \
  python backup_database.py --output hosted-backup.sqlite3
```

Copy important backups off the VPS. The Browserless profile volume should also
be backed up if retaining the authenticated browser state is important.

## Security boundary

This deployment does not automate wagers, deposits, withdrawals, CAPTCHA
bypasses, or credential entry. The user performs account authentication
manually. AIE observes only data legitimately available to that authenticated
browser session and must keep pre-round and post-round information separated
before any future live prediction is implemented.
