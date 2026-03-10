# HTTPS Setup

Enable encrypted connections to be-conductor without Tailscale. Useful for accessing the dashboard from another device on your local network.

## Why HTTPS?

When you access be-conductor from another device over plain HTTP, browsers block secure-context APIs like clipboard access (`navigator.clipboard`). HTTPS fixes this — and encrypts all traffic between your device and the server.

If you already use Tailscale, you don't need this — Tailscale encrypts traffic at the network level.

## Option 1: Generate a self-signed certificate (easiest)

### From the dashboard

1. Open the dashboard at `http://127.0.0.1:7777`
2. Hamburger menu → **Settings** → **General** tab
3. Scroll to the **HTTPS** section
4. Click **Generate self-signed certificate**
5. Restart the server when prompted

The certificate is saved to `~/.be-conductor/certs/` and configured automatically. It includes your LAN IP as a Subject Alternative Name (SAN), so other devices on your network can connect without certificate errors for the IP.

### From the CLI

```bash
be-conductor cert
be-conductor restart -f
```

The `cert` command detects your LAN IP and includes it in the certificate. To set a custom validity period:

```bash
be-conductor cert --days 730
```

### After generating

Open the dashboard at `https://127.0.0.1:7777` (note **https**). Your browser will warn about the self-signed certificate — accept it once and it won't ask again.

From another device, use `https://<your-ip>:7777`. The LAN IP is included in the certificate's SAN entries, so most browsers accept it after you confirm the security exception once.

## Option 2: Upload or paste your own certificate

If you have a certificate from a CA (e.g. Let's Encrypt) or your own PKI:

### From the dashboard

1. Open Settings → General → HTTPS
2. Click **Upload / paste PEM**
3. Paste your certificate PEM into the first field and your private key PEM into the second
4. Click **Upload**
5. Restart the server when prompted

Both files are saved to `~/.be-conductor/certs/` with the key file permissions set to `0600`.

### From the CLI

Point the server at your cert and key files directly:

```bash
be-conductor serve --ssl-cert /path/to/cert.pem --ssl-key /path/to/key.pem
```

Or set environment variables (useful for autostart):

```bash
export BE_CONDUCTOR_SSL_CERTFILE=/path/to/cert.pem
export BE_CONDUCTOR_SSL_KEYFILE=/path/to/key.pem
be-conductor up
```

Environment variables take precedence over the config file.

## Option 3: Environment variables

Set these before starting the server:

```bash
export BE_CONDUCTOR_SSL_CERTFILE=/path/to/cert.pem
export BE_CONDUCTOR_SSL_KEYFILE=/path/to/key.pem
```

This is the best option for systemd/launchd/Task Scheduler autostart setups where you manage certs externally.

## Removing HTTPS

### From the dashboard

Settings → General → HTTPS → **Remove HTTPS**, then restart.

### From the CLI

Remove the environment variables (if set) and clear the config:

```bash
# Remove env vars from your shell profile, then:
be-conductor restart -f
```

The dashboard's remove button deletes the cert/key files from `~/.be-conductor/certs/` and clears the SSL config.

## How it works

- Certificates are stored in `~/.be-conductor/certs/` (cert.pem and key.pem)
- Configuration is persisted in `~/.be-conductor/config.yaml` (`ssl_certfile` and `ssl_keyfile` keys)
- uvicorn serves HTTPS natively — no reverse proxy needed
- The CLI automatically uses `verify=False` for self-signed certs when talking to the local server
- WebSocket connections upgrade to `wss://` automatically — no frontend changes needed
- The self-signed generator creates an RSA 2048-bit certificate with SAN entries for `localhost`, `127.0.0.1`, and your detected LAN IP

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Browser says "Not Secure" | Expected for self-signed certs — accept the exception once |
| Certificate error on LAN IP | Regenerate the cert after your IP changed (`be-conductor cert`) |
| `openssl` not found | Install OpenSSL: `apt install openssl` (Debian/Ubuntu), `brew install openssl` (macOS) |
| Server won't start after enabling SSL | Check that both cert and key files exist and are valid PEM format |
| Want to go back to HTTP | Remove HTTPS from Settings → General, or delete `~/.be-conductor/certs/` and restart |

## Combining with auth token

For secure access on a shared network, combine HTTPS with an auth token:

1. Enable HTTPS (any option above)
2. Set a token: Settings → General → Auth Token, or `export BE_CONDUCTOR_TOKEN=my-secret`
3. Restart the server

Now all connections are encrypted and authenticated.
