# SSH to the Host Through the Tunnel

Expose the host machine's SSH over the existing named tunnel at `ssh.{domain}` — real terminal access (ssh, scp, VS Code Remote) from anywhere, with zero ports opened on the router and Cloudflare Access email OTP in front.

How it works:

```
laptop → ssh ssh.{domain}
       → cloudflared (ProxyCommand on the laptop)
       → Cloudflare Edge (Access: email OTP — dedicated app, owner-only)
       → Cloudflare Tunnel (runs inside the container)
       → host.docker.internal:22 (the host's sshd)
```

- **No open ports** — the tunnel is outbound-only; port scanners see nothing on the home IP.
- **Auth before the handshake** — unauthenticated traffic dies at the edge; nobody reaches sshd without passing the OTP.
- **SSH stays end-to-end encrypted** — Cloudflare proxies the SSH bytes, it can't read the session.
- **Still real ssh auth after that** — Access is the outer gate, the ssh key is the inner one.

The tunnel runs *inside the container*, so the ingress target is `host.docker.internal:22` (Docker Desktop's alias for the host), not `localhost:22`.

## Prerequisites

Named tunnel + Access already set up (`setup.md`). Verify:

```bash
source /workspace/wolts/.env
echo "API_TOKEN=${CLOUDFLARE_API_TOKEN:+SET}"
echo "ACCOUNT_ID=${CLOUDFLARE_ACCOUNT_ID:-NOT SET}"
echo "ZONE_ID=${CLOUDFLARE_ZONE_ID:-NOT SET}"
getent hosts host.docker.internal || echo "WARNING: host.docker.internal not resolvable (non-Docker-Desktop setup — find the host gateway IP instead)"
```

Ask the user for:
1. **The domain** — e.g. `example.com` (ssh will live at `ssh.example.com`)
2. **Owner email(s)** — ONLY the owner's emails. This gate protects a host shell; do not reuse the wildcard app's user list.

## Step 1: Enable SSH on the host

The user does this on the host machine (not in the container):

- **macOS:** System Settings → General → Sharing → turn on **Remote Login**.
- **Linux:** `sudo systemctl enable --now ssh` (or `sshd` on some distros).

## Step 2: Find the tunnel and snapshot its config

The named tunnel's ingress is remotely managed (stored on Cloudflare's side). Always snapshot before touching it — rollback is one PUT of the snapshot.

```bash
source /workspace/wolts/.env
TUNNEL_ID=$(curl -s -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/cfd_tunnel?is_deleted=false" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['result'][0]['id'])")
echo "Tunnel: $TUNNEL_ID"

curl -s -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/cfd_tunnel/$TUNNEL_ID/configurations" \
  | python3 -m json.tool > /tmp/tunnel-config-backup.json
cat /tmp/tunnel-config-backup.json
```

If the config already has an `ssh.{domain}` rule, skip to Step 4.

## Step 3: Add the ssh ingress rule

Take the ingress list from the snapshot and re-PUT it with one new rule inserted:

```json
{"service": "ssh://host.docker.internal:22", "hostname": "ssh.<domain>"}
```

**Rule order matters:** the new rule must sit ABOVE the `*.{domain}` wildcard rule (Cloudflare matches top-down; below the wildcard it would never fire). Keep every existing rule exactly as-is and keep the `http_status:404` catch-all last.

```bash
source /workspace/wolts/.env
# Build the body from the snapshot: config.ingress with the ssh rule inserted
# before the wildcard. Example final shape:
curl -s -X PUT \
  "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/cfd_tunnel/$TUNNEL_ID/configurations" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{
    "config": {
      "ingress": [
        {"service": "http://localhost:7777", "hostname": "<lodge-subdomain>.<domain>"},
        {"service": "ssh://host.docker.internal:22", "hostname": "ssh.<domain>"},
        {"service": "http://localhost:7777", "hostname": "*.<domain>"},
        {"service": "http_status:404"}
      ],
      "warp-routing": {"enabled": false}
    }
  }' | python3 -c "import json,sys; r=json.load(sys.stdin); print('success:', r['success'], r.get('errors') or '')"
```

The running `cloudflared` picks up remote config changes automatically — no container restart.

**Rollback if anything breaks:** PUT the snapshot's `config` object back the same way.

## Step 4: DNS record

```bash
source /workspace/wolts/.env
curl -s -X POST "https://api.cloudflare.com/client/v4/zones/$CLOUDFLARE_ZONE_ID/dns_records" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  -H "Content-Type: application/json" \
  --data "{
    \"type\": \"CNAME\",
    \"name\": \"ssh\",
    \"content\": \"$TUNNEL_ID.cfargotunnel.com\",
    \"proxied\": true,
    \"comment\": \"SSH over cloudflare tunnel\"
  }" | python3 -c "import json,sys; r=json.load(sys.stdin); print('success:', r['success'], r.get('errors') or '')"
```

Already exists (error 81057)? Fine — idempotent, move on.

## Step 5: Dedicated Access app, owner-only

Do NOT rely on the wildcard Access app: it covers every user ever granted app access, and none of them should reach the host's ssh handshake. A dedicated exact-hostname app takes precedence over the wildcard at the edge.

```bash
source /workspace/wolts/.env
APP_ID=$(curl -s -X POST \
  "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/access/apps" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{
    "name": "woltspace-ssh",
    "type": "self_hosted",
    "domain": "ssh.<domain>",
    "session_duration": "24h"
  }' | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['id'])")
echo "App: $APP_ID"

curl -s -X POST \
  "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/access/apps/$APP_ID/policies" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  -H "Content-Type: application/json" \
  --data '{
    "name": "allow-owner-ssh",
    "decision": "allow",
    "precedence": 1,
    "include": [{"email": {"email": "<owner-email>"}}]
  }' | python3 -c "import json,sys; r=json.load(sys.stdin); print('success:', r['success'], r.get('errors') or '')"
```

Verify the dedicated app (not the wildcard) is answering — its `aud` should appear in the login redirect:

```bash
curl -s -o /dev/null -w "%{redirect_url}\n" https://ssh.<domain>/ | grep -o "kid=[a-f0-9]*"
# should match the new app's aud (in the app-create response), not the wildcard app's
```

Propagation can take ~30s; probe again if the wildcard's aud shows first.

## Step 6: Client setup (the machine the user sshes FROM)

```bash
brew install cloudflared        # macOS; other OS: developers.cloudflare.com/cloudflared
```

Add to `~/.ssh/config`:

```
Host ssh.<domain>
  ProxyCommand cloudflared access ssh --hostname %h
```

Test:

```bash
ssh <host-username>@ssh.<domain>
```

First connect opens a browser for the email OTP; after that, connections are silent for the app's `session_duration`. `scp` and VS Code Remote-SSH work through the same config entry.

## Step 7: Harden — key-only auth

Once key login works (`ssh-copy-id <host-username>@ssh.<domain>`), disable password auth on the host:

- **macOS:** in `/etc/ssh/sshd_config` set `PasswordAuthentication no` and `KbdInteractiveAuthentication no`, then `sudo launchctl kickstart -k system/com.openssh.sshd`.
- **Linux:** same settings, then `sudo systemctl restart ssh`.

Final posture: an attacker needs to compromise the owner's email inbox (to pass Access) AND hold the owner's private ssh key. The host exposes no ports either way.

## Removing it later

1. PUT the tunnel config back without the ssh rule (or restore the snapshot).
2. Delete the `ssh` CNAME record.
3. Delete the `woltspace-ssh` Access app (dashboard → Zero Trust → Access → Applications, or API DELETE — note some scoped API tokens can create but not delete Access apps; use the dashboard then).
4. Turn off Remote Login on the host if nothing else uses it.
