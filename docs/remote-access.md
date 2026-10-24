# Remote access (dashboard from other devices)

The hub listens on `127.0.0.1:8765` by default. To reach it from your phone or laptop, forward that port to a server with a public IP and turn on login.

## 1. Create an account (required before any remote access)
```powershell
.\.venv\Scripts\python.exe -m infohub user add <name>      # hidden password prompt, min 10 characters
.\.venv\Scripts\python.exe -m infohub user list
```
Once one user exists, every page and API call requires login (`hub.auth.mode: auto`). That includes requests that arrive through a tunnel as 127.0.0.1. The browser keeps a 30-day session cookie (HttpOnly, SameSite=Strict), so you log in once per device. `user passwd <name>` changes the password and logs out all of that user's sessions. `user remove <name>` deletes the account.

For agents and scripts, use `python -m infohub token add <name>`. The token is shown once; send it as `Authorization: Bearer <token>`.

## 2. Tell the hub which address you will use
`config/hub.local.yaml`:
```yaml
hub:
  dashboard:
    allowed_hosts: ["203.0.113.5:18765"]     # or "hub.example.com"; must match what the browser shows
```
Requests with any other `Host` header are rejected, which protects against DNS rebinding.

## 3. Forward the port (pick one)
- **SSH reverse tunnel.** There's nothing to install on the server; on the hub machine run:
  `ssh -N -R 0.0.0.0:18765:127.0.0.1:8765 user@203.0.113.5`
  The server's sshd needs `GatewayPorts clientspecified`. To keep the tunnel up, use `autossh` or a Scheduled Task.
- **frp** (`frpc` on the hub, `frps` on the server): a TCP proxy from `remotePort = 18765` to `localPort = 8765`.

The hub itself stays bound to 127.0.0.1. Never bind it to `0.0.0.0` without an account; it refuses to start that way.

## 4. Use HTTPS
Over plain HTTP, the password and session cookie can be read by anyone on the network path. Use one of:
- **Built-in TLS:** set `hub.dashboard.tls_cert` / `tls_key` to PEM files, e.g. a Let's Encrypt certificate for a domain that points at the server. Cookies then get the `Secure` flag automatically.
- **A reverse proxy on the public server** (Caddy, nginx) that terminates HTTPS and forwards to the tunnel port. Set `hub.auth.cookie_secure: true`, and `trust_forwarded_for: true` so login throttling sees real client addresses.

Failed logins are throttled: 5 per client address per 15 minutes.
