# Cloudflare deployment

Publish AI Factory through a remotely-managed Cloudflare Tunnel. The browser-facing
URL is `https://dev.aydin.cloud`, while `cloudflared` connects to the application
over loopback at `http://127.0.0.1:8000`. No inbound Internet port is required.

## 1. Secure AI Factory before publishing

1. Sign in as `admin` and change the existing admin password from the Profile page.
   Changing `ADMIN_PASSWORD` later does not update an account already stored in the
   database.
2. Generate a new JWT secret:

   ```bash
   openssl rand -hex 32
   ```

3. In the project-root `.env`, set the generated value and require HTTPS cookies:

   ```dotenv
   SECRET_KEY=paste-the-generated-value-here
   COOKIE_SECURE=True
   ```

4. Pull the latest application code and reload it:

   ```bash
   git pull --ff-only origin main
   bash deploy/restart.sh
   ```

## 2. Install cloudflared on Rocky Linux

Add Cloudflare's RPM repository and install the connector:

```bash
curl -fsSl https://pkg.cloudflare.com/cloudflared.repo | sudo tee /etc/yum.repos.d/cloudflared.repo
sudo dnf install -y cloudflared
cloudflared --version
```

## 3. Create the named tunnel

1. In Cloudflare, open **Networking > Tunnels** and create a remotely-managed
   tunnel named `aifactory-demo`.
2. Select Linux / Red Hat / 64-bit and copy the generated service-install command.
3. Run that command on the AI Factory VM. Treat the tunnel token as a password and
   do not save it in Git or paste it into chat.
4. In the tunnel's **Routes**, add a **Published application**:
   - Hostname: `dev.aydin.cloud`
   - Service type: `HTTP`
   - Service URL: `http://127.0.0.1:8000`
5. Confirm the connector and service are healthy:

   ```bash
   sudo systemctl enable --now cloudflared
   sudo systemctl status cloudflared --no-pager
   curl -I http://127.0.0.1:8000/login
   curl -I https://dev.aydin.cloud/login
   ```

Use a named tunnel, not a temporary `trycloudflare.com` quick tunnel. AI Factory's
deployment logs use Server-Sent Events, which quick tunnels do not support.

## 4. Protect the demo with Cloudflare Access

Public registration is intentionally open inside AI Factory. Without an outer access
policy, anyone could create many one-workspace accounts and exhaust the VM.

1. Open **Zero Trust > Access controls > Applications**.
2. Create a **Self-hosted** application for `dev.aydin.cloud`.
3. Add an **Allow** policy containing only the exact presenter/attendee email
   addresses, or a trusted email domain.
4. Enable One-time PIN as an identity provider if no other IdP is configured.
5. Do not use an `Everyone` rule or a policy that only checks the One-time PIN
   login method; either choice permits any valid email address.

Users will pass Cloudflare Access first, then create or sign in to their AI Factory
account. This preserves the in-app per-user quota demonstration.

## 5. Cloudflare settings for browser IDEs

- Under **Network**, keep WebSockets enabled for VS Code terminals and Jupyter
  kernels.
- Enable **Always Use HTTPS** for the zone.
- Add a cache rule that bypasses cache when the hostname equals
  `dev.aydin.cloud`; IDE and notebook responses are dynamic.
- Avoid challenge rules on this hostname after Access authentication, because
  repeated browser challenges can interrupt API and WebSocket traffic.

## 6. Publish offline downloads

AI Factory can asynchronously stream verified files from `DOWNLOADS_ROOT` at
`/download/`, including byte-range requests for resumable multi-gigabyte
downloads. Enable `DOWNLOADS_ENABLED=True` only after the directory has been
created with read access for the AI Factory systemd user. To expose the admin-only
rebuild control, also set `DOWNLOAD_UPDATES_ENABLED=True` and configure the build
directory described in [`AIRGAP.md`](AIRGAP.md).

Cloudflare limits request/upload sizes but does not impose the same limit on a
streamed response body. Add a cache rule that bypasses cache when the URI path
starts with `/download/`; AI Factory also sends `Cache-Control: private, no-store`.
Confirm the existing Access application covers `/download/*` so the archive is
not unintentionally public.

```bash
curl -I https://dev.aydin.cloud/download/
```

## 7. Close the direct application port

Only after the public hostname works, remove the external firewalld rule for port
`8000`. The loopback origin remains reachable by `cloudflared`:

```bash
sudo firewall-cmd --permanent --remove-port=8000/tcp
sudo firewall-cmd --reload
sudo firewall-cmd --list-ports
```

## Troubleshooting

```bash
sudo journalctl -u cloudflared -n 100 --no-pager
sudo journalctl -u aifactory -n 100 --no-pager
curl -I http://127.0.0.1:8000/login
```

If the tunnel cannot connect, verify outbound TCP or UDP port `7844` is permitted.
If the dashboard works but terminals or kernels do not, verify Cloudflare
WebSockets are enabled and test both a VS Code terminal and a Jupyter kernel.

Official references:

- https://developers.cloudflare.com/tunnel/setup/
- https://developers.cloudflare.com/tunnel/advanced/local-management/create-local-tunnel/
- https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/
- https://developers.cloudflare.com/cloudflare-one/integrations/identity-providers/one-time-pin/
- https://developers.cloudflare.com/cloudflare-one/faq/cloudflare-tunnels-faq/
