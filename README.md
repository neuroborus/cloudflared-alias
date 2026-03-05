# cloudflared-alias

Cloudflare Tunnel with a local Caddy gate: share a backend via a keyed URL (path or subdomain) without changing the app.

## Quickstart

1. Install Caddy:

```bash
# Debian/Ubuntu example:
sudo apt update
sudo apt install -y caddy
```

2. Install cloudflared (if not installed):  
   https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
3. Ensure your named tunnel exists in `~/.cloudflared/config.yml` with `tunnel`, `credentials-file` and `hostname:`.
4. Start your backend locally (example: `localhost:3000`).
5. Run (default is **path** mode with a random key):

```bash
./scripts/tunnel.sh 3000
```

6. Open the printed share URL and append your route (e.g. `/swagger`). Use `-n` for no key, `-s` for subdomain; see **Modes** below.

Optional alias for faster startup:

```bash
echo 'alias tunnel-share="cd $HOME/path/to/cloudflared-alias && ./scripts/tunnel.sh"' >> ~/.bashrc
source ~/.bashrc
```

Then run:

```bash
tunnel-share 3000
```

## Modes

Default mode is **path** (key in URL path). Override with a flag or via config (see **Config file**).

| Mode | Flag | URL |
|------|------|-----|
| **path** (default) | `-p` / `--path` | `https://<hostname>/<key>/` — key in path; Caddy strips it before proxying. |
| **subdomain** | `-s` / `--subdomain` | `https://<key>.<domain>/` — key in hostname; Swagger/relative URLs work without backend changes. |
| **no-key** | `-n` / `--no-key` | `https://<hostname>/` — no key; all routes as-is (e.g. for quick local share). |

Examples:

```bash
./scripts/tunnel.sh 3000              # path, random key
./scripts/tunnel.sh -n 3000           # no key
./scripts/tunnel.sh -s 3000           # subdomain
./scripts/tunnel.sh -p 3000 mykey     # path, key "mykey"
```

**When to use which:** In most cases **subdomain** is preferable — Swagger and any relative URLs work without backend changes. It requires **wildcard setup in Cloudflare** (DNS and tunnel hostname for `*.<domain>`), and for HTTPS on multi-level subdomains (`<key>.<domain>`) Cloudflare does not issue a certificate by default: either extra setup/overhead (e.g. custom certificate) or a **paid plan** (Total TLS / Advanced Certificate Manager). So **path** is the default — it works with free Universal SSL and no wildcard.

## What this feature does

- Keeps your backend unchanged (e.g. `/swagger` stays `/swagger`).
- Runs Caddy locally in front of the backend.
- Generates a fresh random key on each run.
- **Subdomain mode:** exposes the app at `https://<key>.<domain>/...`; requests use the same origin, so Swagger "Try it out" and all relative URLs work by default.
- **Path mode:** exposes at `https://<hostname>/<key>/...`; Caddy strips `/<key>` before proxying.
- Returns `404` for requests that don’t match the current key (wrong subdomain or path).

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
- An existing named Cloudflare tunnel config with `tunnel:`, `credentials-file:` and `hostname:`. By default the script reads `~/.cloudflared/config.yml`; to use another file set `CLOUDFLARED_BASE_CONFIG=/path/to/config.yml`.
  - **Path / no-key:** one hostname in config (e.g. `local.hasso.tech`). One CNAME in DNS.
  - **Subdomain:** wildcard hostname (e.g. `*.local.hasso.tech`) and DNS wildcard; domain is derived from this.

## Config file (defaults)

**`cloudflared-alias.conf`** in the project root holds defaults. Key=value, one per line; `#` = comment. Env vars override.

Common options:

| Option | Default | Description |
|--------|---------|-------------|
| `DEFAULT_MODE` | `path` | Default mode when no `-p`/`-s`/`-n`: `path`, `subdomain`, `no-key` |
| `CADDY_PORT` | `9090` | Port Caddy listens on (cloudflared forwards here) |
| `ID_LENGTH` | `4` | Length of random key when not provided |
| `DETACH` | `0` | `1` = run in background |

You can also set `SUBDOMAIN_DOMAIN`, `CLOUDFLARED_BASE_CONFIG` in the config file.

## File overview

- `scripts/tunnel.sh`: Main entrypoint.
- `cloudflared-alias.conf`: Defaults (edit to change DEFAULT_MODE, CADDY_PORT, etc.).
- `deploy/caddy/Caddyfile.template`: Caddy (path with key).
- `deploy/caddy/Caddyfile.subdomain.template`: Caddy (subdomain).
- `deploy/caddy/Caddyfile.path-nokey.template`: Caddy (path, no key).
- `deploy/cloudflared/config.template.yml`: Cloudflared template rendered at runtime.
- `.runtime/registry`: List of running tunnel instances (key, port, Caddy port, PIDs).
- `.runtime/instances/<port>/`: Per-instance Caddy config, PID, and log (one dir per running tunnel).
- `.runtime/cloudflared/config.yml`: Rendered cloudflared config (multi-ingress to all instance ports).
- `.runtime/current-share-url.txt`: Current generated share URL.
- `.runtime/current-path-id.txt`: Current generated prefix ID.
- `.runtime/cloudflared/cloudflared.log`: Cloudflared log.
- `.runtime/tunnel-history`: Last 10 tunnels (for `history` / `-h`).

## How to run

1. Ensure your backend is running locally (example: `localhost:3000`).
2. Run:

```bash
./scripts/tunnel.sh 3000
```

3. Read the printed hostname, path ID, and final URL.

Pass a key after the port to use it; otherwise a random key is generated (path/subdomain). Use `-n` for no key:

```bash
./scripts/tunnel.sh 3000        # path, random key
./scripts/tunnel.sh 3000 mykey  # path, key "mykey"
./scripts/tunnel.sh -n 3000     # no key
./scripts/tunnel.sh -s 3000      # subdomain
```

## Example command

```bash
./scripts/tunnel.sh 3000
```

## Example output (path mode, default)

```text
[tunnel] Starting Caddy on localhost:9090
[tunnel] Starting cloudflared tunnel 'localhost-tunnel'
[tunnel] Mode            : path (key in path)
[tunnel] Tunnel hostname : local.hasso.tech
[tunnel] Path ID         : k4m8q2w9x7pz (generated)
[tunnel] Share URL       : https://local.hasso.tech/k4m8q2w9x7pz/
[tunnel] Runtime files   : /.../cloudflared-alias/.runtime
[tunnel] Logs            : /.../.runtime/instances/9090/caddy.log, /.../.runtime/cloudflared/cloudflared.log
[tunnel] Running in foreground. Press Ctrl+C to stop.
```

## Example final URL

- **Path (default):** `https://local.hasso.tech/k4m8q2w9x7pz/` — Caddy strips `/<key>` before proxying.
- **No-key (`-n`):** `https://local.hasso.tech/` — no key; Swagger and all routes work as-is.
- **Subdomain (`-s`):** `https://k4m8q2w9x7pz.local.hasso.tech/` — key in hostname; relative URLs work without backend changes.

## How it works

- **path:** Caddy accepts only `/<key>` and `/<key>/*`, strips `/<key>`, and proxies. Backend sees paths like `/swagger`. For Swagger "Try it out" in path mode, the backend must use the `X-Forwarded-Prefix` header (see Troubleshooting).
- **no-key:** Caddy proxies all requests to the backend; no key, no stripping. Share URL = `https://<hostname>/`.
- **subdomain:** Caddy accepts only `Host: <key>.<domain>`, then proxies without path change. Same-origin requests (e.g. Swagger) work without backend config.

## Parallel runs and conflicts

You can run several tunnels at once with **different keys and different backend ports**. Each run gets its own Caddy instance (on a free port from `CADDY_PORT` upward) and one shared cloudflared process forwards traffic to all of them.

If you start a tunnel with a **key or backend port** that is already in use by another run, the script **stops the previous tunnel** (with a short message) and then starts the new one. Examples:

- Same key, different port: the old tunnel for that key is stopped.
- Same backend port, different key: the old tunnel using that port is stopped.

```bash
./scripts/tunnel.sh 3000 key1    # first tunnel
./scripts/tunnel.sh 3001 key2    # second tunnel (runs in parallel)
./scripts/tunnel.sh 3000 key1    # stops the first key1 tunnel, starts a new one
```

## Stop and restart

Stop **all** runtime Caddy instances and cloudflared:

```bash
# foreground mode (default): press Ctrl+C in the running terminal (stops only that instance)
# detached mode (DETACH=1): use explicit stop to stop everything
./scripts/tunnel.sh stop
```

Restart with a new random ID:

```bash
./scripts/tunnel.sh 3000
```

## History (interactive pick)

The last 10 tunnels are stored in `.runtime/tunnel-history` with **created** and **last used** timestamps. Use **`history`** or **`-h`** to open an interactive list in the terminal and pick a tunnel by number; the chosen entry is then started with the same mode and key (same share URL). You can override the backend port when prompted.

```bash
./scripts/tunnel.sh history   # or: ./scripts/tunnel.sh -h
```

You’ll see a numbered list (1 = most recent). Enter a number to run that tunnel, or Enter with no number to cancel. Choosing an entry updates its **last used** time in the history.

## Environment variables (override config file)

- `DEFAULT_MODE` — default mode when no flag: `path`, `subdomain`, `no-key`.
- `CADDY_PORT`, `ID_LENGTH`, `DETACH` — same as in config file.
- `CLOUDFLARED_BASE_CONFIG` — path to cloudflared config (default: `~/.cloudflared/config.yml`).
- `SUBDOMAIN_DOMAIN` — for subdomain mode: domain for `<key>.<domain>` (else from cloudflared hostname).
- `TUNNEL_NAME`, `TUNNEL_HOSTNAME`, `TUNNEL_CREDENTIALS_FILE` — override tunnel config.

Example custom Caddy port:

```bash
CADDY_PORT=18080 ./scripts/tunnel.sh 3000
```

Example detached mode:

```bash
DETACH=1 ./scripts/tunnel.sh 3000
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
CADDY_PORT=18080 ./scripts/tunnel.sh 3000
```

### public URL returns 404

- **Subdomain:** You used an old key hostname, or DNS/ingress for `*.<SUBDOMAIN_DOMAIN>` is missing.
- **Path:** You used an old key or a URL without the `/<key>/` prefix.
- Caddy failed to start; check `.runtime/caddy/caddy.log`.

### Swagger / OpenAPI: "Try it out" returns 404

Use **subdomain mode** (default): open `https://<key>.local.hasso.tech/swagger` — requests from the UI go to the same origin, so they work without any backend config.

In **path mode**, the backend must use the **`X-Forwarded-Prefix`** header to set Swagger’s server base path (Caddy sends this header).

### backend works directly but not through the tunnel

Check:

- backend is reachable at `http://localhost:<backend_port>`
- cloudflared is running (check `.runtime/cloudflared/cloudflared.log`)
- rendered cloudflared config points to Caddy (`service: http://localhost:<CADDY_PORT>`)

## Security note

The random prefix is obscurity, not authentication.

For stronger protection, put Cloudflare Access in front of the tunnel hostname.
