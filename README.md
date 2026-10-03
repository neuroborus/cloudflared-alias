# cloudflared-alias

Cloudflare Tunnel with a local Caddy gate: share a backend via a keyed URL (path or subdomain) without changing the app.

## Development and Agent Runner

Read [AGENTS.md](AGENTS.md) for ownership and working agreements. The canonical
[finalization skill](.agents/skills/finalization/SKILL.md) is discovered by Agent
Runner's `finalization: "auto"` setting. `.claude/skills` links to the same skills.
`CLAUDE.md` links to `AGENTS.md` so Claude uses the same project instructions.

Install Bash, ShellCheck, Python 3, Caddy and the Linux utilities listed below,
then run `bash scripts/check.sh` for offline syntax, lint and regression checks.
Tests use temporary project copies
and synthetic data; they do not start public tunnels or use your `.runtime/`.
Daemons are mocked; Caddy template checks inspect adapted configuration rather
than exercising HTTP forwarding or public connectivity.
The finalization skill owns the complete required-check sequence.

Keep local tasks, plans and reports under the ignored `LOCAL_ARTIFACTS/` directory.
Agent Runner's optional project configuration belongs at
`LOCAL_ARTIFACTS/agent-runner.json`, and local operator additions at
`LOCAL_ARTIFACTS/agent-runner/rules.md`. Keep its authoritative run state outside
both the project and task trees. Read the installed operator guide through
`guidance_read` or `agent-run guidance --project /path/to/cloudflared-alias`
before supervising a run.

Use `plan-authoring` for a reviewed plan, `plan-execution` on a clean worktree for
planned local commits, and `polishing` for an existing non-empty local change set.
`independent` is the default review mode. Finalization validates content; Agent
Runner owns staging in its commit or handoff phase. Outside a run, requested
finalization stages relevant changes and drafts a message without committing.

## Commands and flags

| Flag / command | Description |
|----------------|-------------|
| `--help`, `-h` | Show usage and exit. |
| `--list`, `-l` | Show last 10 tunnels and pick one interactively (run without port). |
| `stop` | Stop all Caddy instances and cloudflared. |
| `-p`, `--path` | Path mode (default): key in URL path. |
| `-s`, `--subdomain` | Subdomain mode: key in hostname. |
| `-n`, `--no-key` | No key: share URL = `https://<hostname>/`. |

Examples: `./scripts/tunnel.sh -h` (help), `./scripts/tunnel.sh -l` (pick from history), `./scripts/tunnel.sh 3000` (start tunnel), `./scripts/tunnel.sh stop` (stop all).

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

6. Open the printed share URL and append your route (e.g. `/swagger`). See **Commands and flags** above for `-n`, `-s`, `-l`, `-h`; **Modes** below for details.

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

Default mode is **path** (key in URL path). Override with `-p` / `-s` / `-n` or via config (see **Config file**).

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
- Generates a fresh random key in path/subdomain mode when no key is provided.
- **Subdomain mode:** exposes the app at `https://<key>.<domain>/...`; requests use the same origin, so Swagger "Try it out" and all relative URLs work by default.
- **Path mode:** exposes at `https://<hostname>/<key>/...`; Caddy strips `/<key>` before proxying.
- Returns `404` for requests that don’t match a keyed route, unless a no-key
  instance provides a fallback on that hostname.

## Why Caddy

Caddy is used as a lightweight local reverse proxy because it makes path matching and prefix stripping simple in a readable Caddyfile.

## Architecture

```text
Internet -> Cloudflare Tunnel -> Caddy -> Local backend
```

## Prerequisites

- Linux + `bash`
- Standard Linux utilities, including `awk`, `sed`, `tr`, `head`, `tail`, `nohup`, `mktemp` and
  `flock` (util-linux), plus `ss` (iproute2) or `netstat` to detect busy ports.
- [`caddy`](https://caddyserver.com/docs/install)
- [`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)
- An existing named Cloudflare tunnel config with `tunnel:`, `credentials-file:` and `hostname:`. By default the script reads `~/.cloudflared/config.yml`; to use another file set `CLOUDFLARED_BASE_CONFIG=/path/to/config.yml`.
  - **Path / no-key:** one hostname in config (e.g. `local.hasso.tech`). One CNAME in DNS.
  - **Subdomain:** wildcard hostname (e.g. `*.local.hasso.tech`) and DNS wildcard; domain is derived from this.

## Config file (defaults)

**`cloudflared-alias.conf`** in the project root holds defaults. Use `KEY=value`,
one per line. Lines starting with `#` and unquoted inline comments after whitespace
are ignored; single or double quotes preserve spaces and `#` in values. Environment
variables override the file, and the last file value wins for duplicate options.
Unknown options and invalid values produce an error.

Common options:

| Option | Default | Description |
|--------|---------|-------------|
| `DEFAULT_MODE` | `path` | Default mode when no `-p`/`-s`/`-n`: `path`, `subdomain`, `no-key` |
| `CADDY_PORT` | `9090` | Starting port for selecting a free Caddy listener (cloudflared forwards here) |
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
- `.runtime/registry`: Tab-delimited running instances (mode, key, backend port,
  Caddy port, Caddy PID, instance directory and hostname).
- `.runtime/instances/<caddy-port>.<suffix>/`: Per-instance Caddy config, PID, log
  and private Caddy data/config directories. Each run gets a unique directory.
- `.runtime/launcher.lock`: Serializes start, stop and shared-state updates.
- `.runtime/cloudflared/config.yml`: Rendered cloudflared config (multi-ingress to all instance ports).
- `.runtime/current-share-url.txt`: Current generated share URL.
- `.runtime/current-path-id.txt`: Current generated prefix ID.
- `.runtime/cloudflared/cloudflared.log`: Cloudflared log.
- `.runtime/tunnel-history`: Last 10 tunnels (for `--list` / `-l`).

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
./scripts/tunnel.sh -s 3000     # subdomain
```

## Example command

```bash
./scripts/tunnel.sh 3000
```

## Example output (path mode, default)

```text
[tunnel] Starting Caddy on localhost:9090
[tunnel] Starting cloudflared tunnel 'localhost-tunnel'
[tunnel] Mode            : path
[tunnel] Tunnel hostname : local.hasso.tech
[tunnel] Path ID         : k4m8
[tunnel] Share URL       : https://local.hasso.tech/k4m8/
[tunnel] Runtime files   : /.../cloudflared-alias/.runtime
[tunnel] Logs            : /.../.runtime/instances/9090.A1b2C3/caddy.log, /.../.runtime/cloudflared/cloudflared.log
[tunnel] Running in foreground. Press Ctrl+C to stop.
```

## Example final URL

- **Path (default):** `https://local.hasso.tech/k4m8/` — Caddy strips `/<key>` before proxying.
- **No-key (`-n`):** `https://local.hasso.tech/` — no key; Swagger and all routes work as-is.
- **Subdomain (`-s`):** `https://k4m8.local.hasso.tech/` — key in hostname; relative URLs work without backend changes.

## How it works

- **path:** Caddy accepts only `/<key>` and `/<key>/*`, strips `/<key>`, and proxies. Backend sees paths like `/swagger`. For Swagger "Try it out" in path mode, the backend must use the `X-Forwarded-Prefix` header (see Troubleshooting).
- **no-key:** Caddy proxies all requests to the backend; no key, no stripping. Share URL = `https://<hostname>/`.
- **subdomain:** Caddy accepts only `Host: <key>.<domain>`, then proxies without path change. Same-origin requests (e.g. Swagger) work without backend config.

## Parallel runs and conflicts

You can run several tunnels at once with **different keys and different backend ports**. Each run gets its own Caddy instance (on a free port from `CADDY_PORT` upward) and one shared cloudflared process forwards traffic to all of them.

Caddy listens over HTTP on loopback, including when its selected port is 443,
with its admin API disabled so parallel instances do not compete for the default
admin port. All instances must use the same tunnel
name and credentials-file; stop them before changing that identity. If the shared
config is missing or its identity cannot be recovered, startup fails and preserves
existing state. Restore that config or stop the instances explicitly. Hostnames are
kept per instance. Path ingress rules precede subdomain rules, followed by no-key
fallbacks on the same host.

Legacy registry rows without a hostname recover it from the persisted cloudflared
ingress rule for their Caddy port before startup changes routing. If that mapping
is missing or ambiguous, startup fails and preserves existing state; restore the
persisted ingress or explicitly stop the instances before starting again.

Backend ports must refer to the app, and cannot use a running launcher's Caddy
listener port.

Foreground mode reports unexpected Caddy or cloudflared exits and cleans up its
instance. Shared cloudflared restarts when other runs start or stop keep
foreground runs active.

If you start a tunnel with a **key or backend port** that is already in use by
another run, the script starts the new Caddy instance and shared cloudflared
replacement before **stopping the previous tunnel** (with a short message).
If either daemon fails to start, existing live instances and history are preserved.
Dead instances are pruned even when startup fails; shared ingress and current
share metadata are updated to the surviving instances, provided legacy hostnames
can be recovered. When none remain, failed startup stops stale cloudflared and
removes the current share metadata. Examples:

- Same key, different port: the old tunnel for that key is stopped.
- Same backend port, different key: the old tunnel using that port is stopped.
- A second no-key run replaces the previous no-key instance.

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

The last 10 tunnels are stored in `.runtime/tunnel-history` with **created** and **last used** timestamps. Use **`--list`** or **`-l`** to open an interactive list in the terminal and pick a tunnel by number; the chosen entry is then started with the same mode and key (same share URL). You can override the backend port when prompted. Use **`--help`** or **`-h`** to show usage.

```bash
./scripts/tunnel.sh --list   # or: ./scripts/tunnel.sh -l
```

You’ll see a numbered list (1 = most recent). Enter a number to run that tunnel, or Enter with no number to cancel. Choosing an entry updates its **last used** time in the history.

History updates after a successful start. If the saved URL differs from the
current hostname configuration, start a new tunnel explicitly instead.

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
- Caddy failed to start; check `.runtime/instances/<caddy-port>.<suffix>/caddy.log`.

### Swagger / OpenAPI: "Try it out" returns 404

Use **subdomain mode** (`-s`): open `https://<key>.local.hasso.tech/swagger` — requests from the UI go to the same origin, so they work without any backend config.

In **path mode**, the backend must use the **`X-Forwarded-Prefix`** header to set Swagger’s server base path (Caddy sends this header).

### backend works directly but not through the tunnel

Check:

- backend is reachable at `http://localhost:<backend_port>`
- cloudflared is running (check `.runtime/cloudflared/cloudflared.log`)
- rendered cloudflared config points to Caddy (`service: http://localhost:<CADDY_PORT>`)

## Security note

The random prefix is obscurity, not authentication.

For stronger protection, put Cloudflare Access in front of the tunnel hostname.
