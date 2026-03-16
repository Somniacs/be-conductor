# Self-hosted VPN with headscale

[Headscale](https://github.com/juanfont/headscale) is an open-source, self-hosted implementation of the Tailscale control server. It gives you the same encrypted mesh VPN as Tailscale, but you own the entire infrastructure — no cloud account needed.

be-conductor works with headscale out of the box. No code changes or special configuration required — if it works with Tailscale, it works with headscale.

## Architecture

```
┌─────────────────────────────────┐
│  VPS / fixed-IP server          │
│  ┌───────────┐                  │
│  │ headscale │  (control plane) │
│  └───────────┘                  │
└─────────────────────────────────┘
        ▲           ▲
        │ WireGuard │ WireGuard
        │ (p2p)     │ (p2p)
┌───────┴───┐  ┌────┴──────────┐
│  Laptop   │  │  Workstation  │
│ tailscale │  │  tailscale    │
│           │  │  be-conductor │
└───────────┘  └───────────────┘
```

The headscale server handles key exchange and device registration. Actual traffic flows peer-to-peer between your devices via WireGuard — headscale never sees it.

## Prerequisites

- A server with a fixed IP or domain (small VPS works fine)
- Tailscale client installed on each device you want to connect

## 1. Install headscale on your server

```bash
# Download the latest release (check https://github.com/juanfont/headscale/releases)
wget https://github.com/juanfont/headscale/releases/latest/download/headscale_linux_amd64
chmod +x headscale_linux_amd64
sudo mv headscale_linux_amd64 /usr/local/bin/headscale
```

Create the config directory and a minimal config:

```bash
sudo mkdir -p /etc/headscale
sudo headscale generate private-key > /etc/headscale/private.key
```

Create `/etc/headscale/config.yaml`:

```yaml
server_url: https://your-server.example.com:443
listen_addr: 0.0.0.0:443
private_key_path: /etc/headscale/private.key
database:
  type: sqlite
  sqlite:
    path: /var/lib/headscale/db.sqlite
noise:
  private_key_path: /etc/headscale/noise_private.key
ip_prefixes:
  - 100.64.0.0/10
```

See the [headscale documentation](https://headscale.net/stable/) for the full configuration reference.

## 2. Start headscale

```bash
sudo headscale serve
```

For production, set it up as a systemd service:

```bash
sudo systemctl enable --now headscale
```

## 3. Create a user

```bash
sudo headscale users create myuser
```

## 4. Connect your devices

On each device (workstation, laptop, phone), point the Tailscale client at your headscale server:

**Linux:**

```bash
tailscale up --login-server https://your-server.example.com
```

**macOS:**

```bash
tailscale up --login-server https://your-server.example.com
```

**iOS / Android:**

Use the Tailscale app, but you'll need to configure a custom control server URL. See the [headscale client docs](https://headscale.net/stable/usage/connect/). Note: the official Tailscale iOS/Android apps have limited support for custom control servers. Consider using alternative clients if needed.

Register each device on the server:

```bash
sudo headscale nodes register --user myuser --key <node-key>
```

Or generate a pre-auth key for easier enrollment:

```bash
sudo headscale preauthkeys create --user myuser --reusable --expiration 24h
tailscale up --login-server https://your-server.example.com --authkey <key>
```

## 5. Run be-conductor

On your workstation (connected to the tailnet):

```bash
be-conductor serve
```

That's it. Any device on your headscale network can now reach the dashboard at `http://<tailnet-ip>:7777`. Traffic is encrypted end-to-end via WireGuard.

## Headscale vs Tailscale

| | Tailscale | Headscale |
|---|---|---|
| Control server | Tailscale cloud | Self-hosted |
| Setup | Zero-config | Requires a VPS |
| Cost | Free tier (limited) | Free (open source) |
| Privacy | Tailscale sees metadata | Fully self-hosted |
| Client apps | Official apps | Same Tailscale clients |
| be-conductor | Works out of the box | Works out of the box |

## Tips

- **No HTTPS needed** — traffic within the tailnet is already encrypted via WireGuard. You can skip the HTTPS setup for be-conductor.
- **Firewall** — your headscale server only needs port 443 open. The be-conductor port (7777) does not need to be exposed to the internet — it's only reachable within the tailnet.
- **Multiple machines** — if you run be-conductor on several workstations, they're all reachable from any device on the tailnet. The multi-server dashboard works seamlessly.
