---
name: woltspace-setup-tunnel
description: Set up a named Cloudflare Tunnel for a permanent, auth-protected lodge URL.
user_invocable: true
---

# Named Tunnel Setup

Guide the human through setting up a named Cloudflare Tunnel with Access auth for their lodge. Step by step, one at a time.

By default, woltspace uses free quick tunnels that generate a random URL on every restart. This skill upgrades to a permanent URL on the user's own domain with password protection at Cloudflare's edge.

**This skill is idempotent** — safe to run again. If things are already configured, validate and skip.

## Step 0: Check existing config

Check if a named tunnel is already configured:

```bash
echo "CLOUDFLARE_TUNNEL_TOKEN=${CLOUDFLARE_TUNNEL_TOKEN:+SET}"
echo "CLOUDFLARE_TUNNEL_URL=${CLOUDFLARE_TUNNEL_URL:-NOT SET}"
```

**If both are set:** The named tunnel is already configured. Verify it's running:

```bash
ps aux | grep "cloudflared tunnel run" | grep -v grep
```

If running, tell the human and stop. If not running, it will start on next server boot.

## Step 1: Domain on Cloudflare

The user needs a domain managed by Cloudflare DNS (free plan). Ask:

> Do you have a domain on Cloudflare already? If not, you'll need one.
>
> **If you have a domain elsewhere** (Namecheap, GoDaddy, etc.):
> 1. Sign up at [cloudflare.com](https://cloudflare.com) (free)
> 2. Add your domain — Cloudflare auto-imports your existing DNS records
> 3. **Verify the imported records match your current DNS** — especially A/CNAME for any existing sites
> 4. Change nameservers at your registrar to Cloudflare's (they'll tell you which ones)
> 5. Wait for propagation: `dig NS yourdomain.com +short` should show `*.ns.cloudflare.com`
>
> Existing sites (Vercel, GitHub Pages, etc.) keep working — Cloudflare just proxies the same DNS records.

Wait for the human to confirm their domain is on Cloudflare before continuing.

## Step 2: Create a Cloudflare API token

Tell the human:

> Go to [dash.cloudflare.com/profile/api-tokens](https://dash.cloudflare.com/profile/api-tokens) → **Create Token** → **Custom Token**
>
> Permissions needed:
> - **Account / Cloudflare Tunnel: Edit**
> - **Zone / DNS: Edit** (scoped to your domain)
> - **Account / Access: Apps and Policies: Edit** (for auth — optional but recommended)
>
> Also grab your **Account ID** and **Zone ID** from the Cloudflare dashboard → your domain → Overview page (right sidebar).

Wait for all three values. Add them to the shared `.env` (`/workspace/wolts/.env`):

```
CLOUDFLARE_API_TOKEN=<token>
CLOUDFLARE_ACCOUNT_ID=<account_id>
CLOUDFLARE_ZONE_ID=<zone_id>
```

Verify the token works:

```bash
source /workspace/wolts/.env
curl -s "https://api.cloudflare.com/client/v4/user/tokens/verify" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" | python3 -c "
import json,sys; r=json.load(sys.stdin)
print('token OK' if r.get('success') else f'token FAILED: {r}')
"
```

## Step 3: Choose a subdomain

Ask the human what subdomain they want for their lodge. For example, if their domain is `example.com`:

> What subdomain do you want for your lodge? e.g. `lodge.example.com`, `my.example.com`

This will NOT affect their root domain or any existing subdomains.

## Step 4: Create the tunnel

Run the following API calls. Replace `<subdomain>` and `<domain>` with the user's choices.

```bash
source /workspace/wolts/.env

# Create the tunnel
TUNNEL_RESULT=$(curl -s -X POST \
  "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/cfd_tunnel" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  -H "Content-Type: application/json" \
  --data "{\"name\": \"woltspace-lodge\", \"tunnel_secret\": \"$(openssl rand -base64 32)\"}")

TUNNEL_ID=$(echo "$TUNNEL_RESULT" | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['id'])")
TUNNEL_TOKEN=$(echo "$TUNNEL_RESULT" | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['token'])")

echo "Tunnel ID: $TUNNEL_ID"
echo "Token: $TUNNEL_TOKEN"
```

Configure the tunnel ingress:

```bash
curl -s -X PUT \
  "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/cfd_tunnel/$TUNNEL_ID/configurations" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  -H "Content-Type: application/json" \
  --data "{
    \"config\": {
      \"ingress\": [
        {\"hostname\": \"<subdomain>.<domain>\", \"service\": \"http://localhost:7777\"},
        {\"service\": \"http_status:404\"}
      ]
    }
  }"
```

Create the DNS CNAME record:

```bash
curl -s -X POST \
  "https://api.cloudflare.com/client/v4/zones/$CLOUDFLARE_ZONE_ID/dns_records" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  -H "Content-Type: application/json" \
  --data "{
    \"type\": \"CNAME\",
    \"name\": \"<subdomain>\",
    \"content\": \"$TUNNEL_ID.cfargotunnel.com\",
    \"proxied\": true
  }"
```

Add the tunnel token and URL to `.env`:

```
CLOUDFLARE_TUNNEL_TOKEN=<token from above>
CLOUDFLARE_TUNNEL_URL=https://<subdomain>.<domain>
```

## Step 5: Set up Access auth (optional but recommended)

This adds email OTP login at Cloudflare's edge — unauthenticated requests never reach the container.

Ask the human for their email address, then:

```bash
source /workspace/wolts/.env

# Create Access application
APP_RESULT=$(curl -s -X POST \
  "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/access/apps" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  -H "Content-Type: application/json" \
  --data "{
    \"name\": \"woltspace-lodge\",
    \"domain\": \"<subdomain>.<domain>\",
    \"type\": \"self_hosted\",
    \"session_duration\": \"24h\"
  }")

APP_ID=$(echo "$APP_RESULT" | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['id'])")

# Create policy allowing the user's email via OTP
curl -s -X POST \
  "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/access/apps/$APP_ID/policies" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  -H "Content-Type: application/json" \
  --data "{
    \"name\": \"allow-owner\",
    \"decision\": \"allow\",
    \"precedence\": 1,
    \"include\": [{\"email\": {\"email\": \"<user-email>\"}}]
  }"
```

More users or identity providers (GitHub, Google) can be added later from **Cloudflare Zero Trust → Access → Applications**.

## Step 6: Test

Start the named tunnel manually to verify:

```bash
source /workspace/wolts/.env
cloudflared tunnel run --token "$CLOUDFLARE_TUNNEL_TOKEN" &
sleep 3
curl -s -o /dev/null -w "%{http_code}" "https://<subdomain>.<domain>"
```

A `403` or `302` (Access redirect) means the tunnel and auth are working. A `200` means it's working without auth.

Tell the human to visit the URL in their browser. If Access is configured, they'll see a login page — enter their email, check for the OTP code, and they're in.

On next container restart, the server will use the named tunnel automatically.

## Step 7: Verify rollback works

Explain to the human how to revert:

> To go back to quick tunnels: remove `CLOUDFLARE_TUNNEL_TOKEN` and `CLOUDFLARE_TUNNEL_URL` from `.env` and restart.
>
> `localhost:7777` always works regardless of tunnel configuration.

## Troubleshooting

- **DNS_PROBE_FINISHED_NXDOMAIN** — nameserver propagation isn't complete yet. Check `dig NS <domain> @8.8.8.8 +short` — it should show Cloudflare nameservers. If it still shows your old registrar, wait.
- **"Authentication error" on API calls** — token missing required permissions. Edit at cloudflare.com/profile/api-tokens.
- **Tunnel starts but site doesn't load** — verify ingress config: `curl -s "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/cfd_tunnel/$TUNNEL_ID/configurations" -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN"`
- **Access login page doesn't appear** — Access app might not be created, or domain doesn't match. Check Zero Trust → Access → Applications.
- **"This site can't be reached" after DNS propagated** — tunnel process might not be running. Check `ps aux | grep cloudflared`.
- **Local DNS cache** — flush with `sudo dscacheutil -flushcache && sudo killall -HUP mDNSResponder` (macOS) or try `dig <subdomain>.<domain> @8.8.8.8` to bypass cache.

## Architecture

```
Browser → subdomain.domain.com
       → Cloudflare Edge (Access auth: email OTP)
       → Cloudflare Tunnel (QUIC, auto-reconnect)
       → localhost:7777 (FastAPI)
```

- **Auth at the edge** — unauthorized requests never reach the container
- **Auto-reconnect** — named tunnels survive network blips, same URL persists across restarts
- **Free tier** — unlimited tunnels, 50 Access users, no credit card required
- **Quick tunnels remain the default** — named tunnels only activate when env vars are set
