# Cloudflare Tunnel + Caddy Prefix Gate

## Quickstart

1. Install Caddy:

```bash
# Debian/Ubuntu example:
sudo apt update
sudo apt install -y caddy
```

2. Install cloudflared (if not installed):  
   https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
3. Ensure your named tunnel exists in `~/.cloudflared/config.yml` with `tunnel`, `credentials-file`, and `hostname`.
4. Start your backend locally (example: `localhost:3000`).
5. Run:

```bash
./scripts/run-tunnel-with-caddy.sh 3000
```

6. Leave this terminal open, and open the printed URL: `https://<hostname>/<ID>/` and append any backend route (for example `swagger`).

Optional alias for faster startup:

```bash
echo 'alias tunnel-share="cd $HOME/path/to/cloudflared-alias && ./scripts/run-tunnel-with-caddy.sh"' >> ~/.bashrc
source ~/.bashrc
```

Then run:

```bash
tunnel-share 3000
```

This project adds a local Caddy-based prefix gate in front of your existing backend, without changing backend routes.

On every launch, a new random path ID is generated (default: 12 lowercase alphanumeric chars), and only requests under that prefix are proxied.

## What this feature does

- Keeps your backend unchanged (for example, backend `/swagger` still exists as `/swagger`).
- Runs Caddy locally in front of the backend.
- Generates a fresh random path prefix on each run.
- Exposes your app publicly only at `https://<hostname>/<ID>/...` via Cloudflare Tunnel.
- Strips `/<ID>` in Caddy before proxying to the backend.
- Returns `404` for all requests without the correct prefix.

## Why Caddy

Caddy is used as a lightweight local reverse proxy because it makes path matching and prefix stripping simple in a readable Caddyfile.

## Architecture

```text
Internet -> Cloudflare Tunnel -> Caddy -> Local backend
```

## Prerequisites

- Linux + `bash`
- [`caddy`](https://caddyserver.com/docs/install)
- [`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)
- An existing named Cloudflare tunnel configured in `~/.cloudflared/config.yml`
  - must include `tunnel:`
  - must include `credentials-file:`
  - must include an ingress `hostname:` entry

## File overview

- `scripts/run-tunnel-with-caddy.sh`: Launcher script.
- `deploy/caddy/Caddyfile.template`: Caddy template rendered at runtime.
- `deploy/cloudflared/config.template.yml`: Cloudflared template rendered at runtime.
- `.runtime/caddy/Caddyfile`: Rendered Caddy config.
- `.runtime/cloudflared/config.yml`: Rendered cloudflared config (points to Caddy).
- `.runtime/current-share-url.txt`: Current generated share URL.
- `.runtime/current-path-id.txt`: Current generated prefix ID.
- `.runtime/caddy/caddy.log`: Caddy log.
- `.runtime/cloudflared/cloudflared.log`: Cloudflared log.

## How to run

1. Ensure your backend is running locally (example: `localhost:3000`).
2. Run:

```bash
./scripts/run-tunnel-with-caddy.sh 3000
```

3. Read the printed hostname, generated ID, and final URL.

## Example command

```bash
./scripts/run-tunnel-with-caddy.sh 3000
```

## Example output

```text
[run-tunnel-with-caddy] Starting Caddy on localhost:8080
[run-tunnel-with-caddy] Starting cloudflared tunnel 'localhost-tunnel'
[run-tunnel-with-caddy] Tunnel hostname : example.com
[run-tunnel-with-caddy] Generated path ID: k4m8q2w9x7pz
[run-tunnel-with-caddy] Share URL       : https://example.com/k4m8q2w9x7pz/
[run-tunnel-with-caddy] Runtime files   : /.../cloudflared-alias/.runtime
[run-tunnel-with-caddy] Logs            : /.../.runtime/caddy/caddy.log, /.../.runtime/cloudflared/cloudflared.log
[run-tunnel-with-caddy] Running in foreground. Press Ctrl+C to stop.
```

## Example final URL

```text
https://example.com/k4m8q2w9x7pz/
```

## How prefix stripping works

- Public request: `https://<hostname>/<ID>/swagger`
- Caddy matches only `/<ID>` and `/<ID>/*`
- Caddy strips `/<ID>`
- Backend receives `/swagger`

Expected behavior:

- `/<ID>/swagger` -> proxied to backend `/swagger`
- `/<ID>/docs` -> proxied to backend `/docs`
- `/swagger` -> `404`
- `/wrongid/swagger` -> `404`

## Stop and restart

Stop runtime Caddy + cloudflared processes started by this script:

```bash
# foreground mode (default): press Ctrl+C in the running terminal
# detached mode (DETACH=1): use explicit stop
./scripts/run-tunnel-with-caddy.sh stop
```

Restart with a new random ID:

```bash
./scripts/run-tunnel-with-caddy.sh 3000
```

## Optional environment variables

- `CADDY_PORT` (default `8080`)
- `ID_LENGTH` (default `12`, allowed `10-32`)
- `DETACH` (default `0`; set `1` to run in background)
- `CLOUDFLARED_BASE_CONFIG` (default `~/.cloudflared/config.yml`)
- `TUNNEL_NAME` (override source config)
- `TUNNEL_HOSTNAME` (override source config)
- `TUNNEL_CREDENTIALS_FILE` (override source config)

Example custom Caddy port:

```bash
CADDY_PORT=18080 ./scripts/run-tunnel-with-caddy.sh 3000
```

Example detached mode:

```bash
DETACH=1 ./scripts/run-tunnel-with-caddy.sh 3000
```

## Troubleshooting

### Caddy missing

If you see `Error: 'caddy' is required but not installed`, install Caddy and re-run.

### cloudflared missing

If you see `Error: 'cloudflared' is required but not installed`, install cloudflared and re-run.

### tunnel name not found

If tunnel details are missing in your source config, ensure `~/.cloudflared/config.yml` has:

- `tunnel: <name>`
- `credentials-file: <path>`
- ingress with `hostname:`

Or set env overrides (`TUNNEL_NAME`, `TUNNEL_HOSTNAME`, `TUNNEL_CREDENTIALS_FILE`).

### port already in use

If Caddy port is busy, either stop the conflicting process or run with another port:

```bash
CADDY_PORT=18080 ./scripts/run-tunnel-with-caddy.sh 3000
```

### public URL returns 404

Common causes:

- You used an old prefix ID from a previous launch.
- You requested a URL without the generated `/<ID>/...` prefix.
- Caddy failed to start; check `.runtime/caddy/caddy.log`.

### backend works directly but not through the tunnel

Check:

- backend is reachable at `http://localhost:<backend_port>`
- cloudflared is running (check `.runtime/cloudflared/cloudflared.log`)
- rendered cloudflared config points to Caddy (`service: http://localhost:<CADDY_PORT>`)

## Security note

The random prefix is obscurity, not authentication.

For stronger protection, put Cloudflare Access in front of the tunnel hostname.
