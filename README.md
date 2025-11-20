# Cloudflared Alias

Small helper script to quickly switch the local port used by a Cloudflare Tunnel
and start the tunnel with a single command.

The script:

1. Updates the `service: http://localhost:<port>` line in your Cloudflare config.
2. Prints the effective **port** and **hostname**.
3. Kills any running `cloudflared` processes.
4. Starts the specified tunnel with the updated config.

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