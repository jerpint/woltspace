# Wildcard Subdomain Setup

Apps can be served at `{app-name}.{your-domain}` (e.g. `corework.woltspace.com`) through your lodge's named Cloudflare tunnel. This gives stable, auth-protected URLs with zero per-app configuration.

## Prerequisites

- A Cloudflare account with your domain's DNS managed by Cloudflare
- A named tunnel already configured (see `woltspace-cloudflare` skill, sub-doc `setup.md`)
- Environment variables set in `wolts/.env`:
  - `CLOUDFLARE_API_TOKEN` — API token with DNS:Edit, Tunnel:Edit, Access:Edit permissions
  - `CLOUDFLARE_ACCOUNT_ID` — your Cloudflare account ID
  - `CLOUDFLARE_ZONE_ID` — zone ID for your domain
  - `CLOUDFLARE_TUNNEL_TOKEN` — tunnel run token
  - `CLOUDFLARE_TUNNEL_URL` — your lodge URL (e.g. `https://jerpint.woltspace.com`)

## Setup steps

### 1. Wildcard DNS record

Add a `*.yourdomain.com` CNAME pointing to your tunnel:

```bash
curl -X POST \
  "https://api.cloudflare.com/client/v4/zones/${CLOUDFLARE_ZONE_ID}/dns_records" \
  -H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}" \
  -H "Content-Type: application/json" \
  --data '{
    "type": "CNAME",
    "name": "*",
    "content": "YOUR_TUNNEL_ID.cfargotunnel.com",
    "proxied": true
  }'
```

Replace `YOUR_TUNNEL_ID` with your tunnel UUID. Find it with:
```bash
curl "https://api.cloudflare.com/client/v4/accounts/${CLOUDFLARE_ACCOUNT_ID}/cfd_tunnel?is_deleted=false" \
  -H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}" | python3 -m json.tool
```

### 2. Tunnel ingress rule

Add a wildcard ingress rule that routes `*.yourdomain.com` to the FastAPI server. This must include all existing rules (the API replaces the entire config):

```bash
curl -X PUT \
  "https://api.cloudflare.com/client/v4/accounts/${CLOUDFLARE_ACCOUNT_ID}/cfd_tunnel/${TUNNEL_ID}/configurations" \
  -H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}" \
  -H "Content-Type: application/json" \
  --data '{
    "config": {
      "ingress": [
        {"hostname": "your-lodge.yourdomain.com", "service": "http://localhost:7777"},
        {"hostname": "*.yourdomain.com", "service": "http://localhost:7777"},
        {"service": "http_status:404"}
      ]
    }
  }'
```

**Important:** The wildcard rule must come AFTER any specific hostname rules (like the lodge or apps with dedicated ports like `deck`). Cloudflare matches rules top-to-bottom, first match wins. The catch-all `http_status:404` must always be last.

### 3. Cloudflare Access policy (recommended)

Without this step, any app subdomain is publicly accessible to anyone who knows the URL. Add an Access application to require authentication:

```bash
# Create Access application
APP_RESULT=$(curl -X POST \
  "https://api.cloudflare.com/client/v4/accounts/${CLOUDFLARE_ACCOUNT_ID}/access/apps" \
  -H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}" \
  -H "Content-Type: application/json" \
  --data '{
    "name": "woltspace-apps",
    "domain": "*.yourdomain.com",
    "type": "self_hosted",
    "session_duration": "24h"
  }')

# Extract app ID
APP_ID=$(echo "$APP_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['result']['id'])")

# Create allow policy (email OTP)
curl -X POST \
  "https://api.cloudflare.com/client/v4/accounts/${CLOUDFLARE_ACCOUNT_ID}/access/apps/${APP_ID}/policies" \
  -H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}" \
  -H "Content-Type: application/json" \
  --data '{
    "name": "allow-owner",
    "decision": "allow",
    "include": [{"email": {"email": "your-email@example.com"}}],
    "exclude": [],
    "require": []
  }'
```

The auth cookie is scoped to `.yourdomain.com`, so logging in once (at the lodge or any app) grants access to all subdomains for the session duration.

## How it works

```
Browser → corework.yourdomain.com
  → Cloudflare Access (auth check at the edge)
  → Cloudflare Tunnel (wildcard ingress rule)
  → localhost:7777 (FastAPI server)
  → subdomain proxy middleware (extracts app name from hostname)
  → proxy to localhost:{app_port}
  → response flows back
```

The server reads `CLOUDFLARE_TUNNEL_URL` at boot to determine the parent domain. When a request arrives at `corework.woltspace.com`, the middleware strips `.woltspace.com` to get the app name, looks up the running app's port, and proxies the request. The app sees clean requests at `/` on its own port — no path prefixes, no URL rewriting.

## Fallback: quick tunnels

Lodges without a custom domain (`CLOUDFLARE_TUNNEL_URL` not set) automatically fall back to per-app quick tunnels with random `trycloudflare.com` URLs. The `public: true` flag in `woltspace.json` triggers this. No additional setup needed.

## Verifying the setup

```bash
# Check DNS resolves
dig corework.yourdomain.com

# Test from inside the container (bypasses Access)
curl -H "Host: corework.yourdomain.com" http://localhost:7777/

# Test from outside (should get Access login page)
curl -sL https://corework.yourdomain.com/ | grep -i "cloudflare"
```

## Troubleshooting

- **App not found (503):** The app isn't running. Start it via the lodge or API.
- **DNS not resolving:** Wildcard CNAME may need a few minutes to propagate. Check with `dig *.yourdomain.com @1.1.1.1`.
- **No Access challenge:** The Access application may not be created, or the domain pattern doesn't match. Check Access apps in the Cloudflare Zero Trust dashboard.
- **Tunnel not routing:** Verify the wildcard ingress rule exists. The tunnel config is a full replacement — make sure you included ALL rules when updating.
