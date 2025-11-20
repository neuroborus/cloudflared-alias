# Cloudflared Alias

Small helper script to quickly switch the local port used by a Cloudflare Tunnel
and start the tunnel with a single command.

The script:

1. Updates the `service: http://localhost:<port>` line in your Cloudflare config.
2. Prints the effective **port** and **hostname**.
3. Kills any running `cloudflared` processes.
4. Starts the specified tunnel with the updated config.

Create an alias:

1. `echo 'alias port-forward="$HOME/.cloudflared/run-tunnel.sh"' >> ~/.bashrc` (or your path to the script)
2. `source ~/.bashrc`

Then use:

- `port-forward 3000`

---

## Requirements

- Linux (tested on Ubuntu).
- `bash`
- [`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation/)
- A configured named tunnel (e.g. `localhost-tunnel`).
- Config file at:

```text
~/.cloudflared/config.yml
```

With structure similar to:

```text
tunnel: localhost-tunnel
credentials-file: /home/USER/.cloudflared/<uuid>.json

ingress:
  - hostname: localhost.hasso.tech
    service: http://localhost:3000
  - service: http_status:404
```

### Cloudflare Cache Rule (example: `localhost.hasso.tech`)

> Replace `hasso.tech` / `localhost.hasso.tech` with your own zone and hostname.

1. In your zone (e.g. `hasso.tech`) go to: **Rules → Cache Rules → Create rule**.
2. **Rule name:** `no-cache localhost`
3. **When incoming requests match:**
   - Condition:
     - **Field:** `Hostname`
     - **Operator:** `equals`
     - **Value:** `localhost.hasso.tech`  ← your hostname
4. **Then… (action):**
   - **Action:** `Cache`
   - **Setting:** `Bypass cache` (or `Cache level: Bypass` in the old UI)
5. **Save** and ensure the rule is **Enabled**.

