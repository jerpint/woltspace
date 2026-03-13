---
name: setup-tunnel
description: Set up a permanent Cloudflare tunnel with a custom domain — or check/manage your current tunnel config.
user_invocable: true
---

# Setup Tunnel — Permanent Domain

Guide the human through upgrading from an ephemeral `*.trycloudflare.com` tunnel to a permanent domain via Cloudflare. Step by step, one at a time.

The goal: their wolt gets a stable URL that survives restarts. No more random URLs.

## Important: Read this fully before responding

This is a guided conversation. Go step by step, one exchange at a time. Wait for the human to respond before moving on. Be concise — they're probably reading this on their phone.

## Step 0: Check current state

Before starting, check what's already configured:

```bash
# Check for existing tunnel config
grep -E '^CF_TUNNEL_TOKEN=|^CF_TUNNEL_HOSTNAME=' .env 2>/dev/null
```

If permanent tunnel vars already exist, tell the human what's configured and ask what they want to do:
- Test the connection
- Change the domain
- Switch back to ephemeral
- View current tunnel status

If nothing's configured, proceed to Step 1.

## Step 1: Prerequisites

Tell the human:

> You'll need two things:
> 1. A **Cloudflare account** (free tier works) — https://dash.cloudflare.com/sign-up
> 2. A **domain on Cloudflare** — either buy one there or transfer an existing domain's DNS to Cloudflare
>
> Got both? Paste the domain you want to use (e.g. `nw.yourdomain.com`).

Wait for their response. Save the hostname they give you — you'll need it later.

If they don't have a domain yet, point them to Cloudflare Registrar (cheapest option, no markup) or explain they can transfer an existing domain's nameservers to Cloudflare.

## Step 2: Create the tunnel in Cloudflare dashboard

Tell the human:

> Head to the **Cloudflare Zero Trust dashboard**:
> https://one.dash.cloudflare.com/
>
> Then: **Networks** > **Tunnels** > **Create a tunnel**
>
> 1. Choose **Cloudflared** as the connector type
> 2. Name it whatever you want (e.g. your wolt's name)
> 3. On the "Install connector" page — **don't install anything**. Just copy the **token** from the install command. It's the long string after `--token` in the command they show you. Looks like `eyJ...`
> 4. On the "Route tunnel" page, add a **public hostname**:
>    - Subdomain: the one you chose (e.g. `nw`)
>    - Domain: your Cloudflare domain
>    - Service type: **HTTP**
>    - Service URL: `localhost:3000`
> 5. Save the tunnel.
>
> Paste the token here when you have it.

Wait for the token. It should start with `eyJ` (base64 JSON). Validate it looks reasonable.

## Step 3: Configure env vars

Once you have the token and hostname, add them to `.env`:

```bash
# Read current .env to avoid overwriting
cat .env
```

Then append the tunnel config:

```
CF_TUNNEL_TOKEN=<their token>
CF_TUNNEL_HOSTNAME=<their hostname, e.g. nw.example.com>
```

Use the Edit tool to add these to `.env`. Place them near the top, after any existing tunnel-related vars. Add a comment:

```
# Permanent tunnel — custom domain via Cloudflare managed tunnel
CF_TUNNEL_TOKEN=eyJ...
CF_TUNNEL_HOSTNAME=nw.example.com
```

## Step 4: Update entrypoint (if needed)

Check if the entrypoint already supports `CF_TUNNEL_TOKEN`. Look at the tunnel startup section:

```bash
grep -n 'CF_TUNNEL_TOKEN' container/entrypoint.sh 2>/dev/null || grep -n 'CF_TUNNEL_TOKEN' /workspace/woltspace/container/entrypoint.sh 2>/dev/null
```

If the three-tier tunnel logic is NOT yet in the entrypoint, apply it. The tunnel startup section (look for `cloudflared tunnel`) should become:

```bash
if [ "${ENABLE_TUNNEL:-true}" != "false" ]; then
  TUNNEL_LOG="$WOLTS_DIR/.state/tunnel.log"

  if [ -n "${CF_TUNNEL_TOKEN:-}" ]; then
    # Managed tunnel: permanent domain via Cloudflare dashboard
    echo "starting managed tunnel → ${CF_TUNNEL_HOSTNAME:-<no hostname configured>}..."
    cloudflared tunnel run --token "$CF_TUNNEL_TOKEN" > "$TUNNEL_LOG" 2>&1 &
    TUNNEL_PID=$!

    if [ -n "${CF_TUNNEL_HOSTNAME:-}" ]; then
      TUNNEL_URL="https://${CF_TUNNEL_HOSTNAME}"
      echo "$TUNNEL_URL" > "$WOLTS_DIR/.state/tunnel-url"
      [ -d "$WOLT_DIR/.state" ] && echo "$TUNNEL_URL" > "$WOLT_DIR/.state/tunnel-url"
      echo "tunnel ready: $TUNNEL_URL (permanent)"
    else
      echo "warning: CF_TUNNEL_TOKEN set but CF_TUNNEL_HOSTNAME missing — tunnel-url not written"
    fi

  else
    # Quick tunnel: ephemeral *.trycloudflare.com URL (default)
    echo "opening tunnel..."
    cloudflared tunnel --url http://localhost:3000 > "$TUNNEL_LOG" 2>&1 &
    TUNNEL_PID=$!

    for i in $(seq 1 30); do
      URL=$(grep -o 'https://[^ ]*trycloudflare.com' "$TUNNEL_LOG" 2>/dev/null | head -1)
      if [ -n "$URL" ]; then
        echo "$URL" > "$WOLTS_DIR/.state/tunnel-url"
        [ -d "$WOLT_DIR/.state" ] && echo "$URL" > "$WOLT_DIR/.state/tunnel-url"
        echo "tunnel ready: $URL"
        break
      fi
      sleep 1
    done
  fi
else
  echo "tunnel disabled — access via http://localhost:4444"
fi
```

Apply this to whichever entrypoint is active:
- `/workspace/woltspace/container/entrypoint.sh` (platform — preferred)
- `container/entrypoint.sh` (wolt-local, if it exists and is used)

## Step 5: Test

Tell the human:

> The config is set. To activate it, the container needs to restart:
> ```
> woltspace restart
> ```
> Once it's back up, the tunnel should connect to your permanent domain. Try opening `https://<their hostname>` in a browser.

If they're running the neowolt standalone container (not woltspace), the command is:
```
./tunnel.sh --rebuild
```

After restart, verify:
```bash
cat .state/tunnel-url
```

It should show `https://<their hostname>` instead of a random trycloudflare.com URL.

## Step 6: Verify DNS

If the site doesn't load after restart, check:

1. **Cloudflare dashboard** — is the tunnel showing as "Healthy"?
2. **DNS** — does the CNAME exist? Cloudflare should have created it automatically when they routed the tunnel.
3. **Logs** — `cat .state/tunnel.log` for errors (auth failures, token issues)

Common issues:
- Token expired → regenerate in dashboard
- Wrong hostname → check the public hostname config in the tunnel settings
- DNS propagation → wait a few minutes if the domain was just added

## Reverting to ephemeral

If they want to go back:

```bash
# Comment out or remove from .env:
# CF_TUNNEL_TOKEN=...
# CF_TUNNEL_HOSTNAME=...
```

Then restart. Without those vars, the entrypoint falls back to a quick tunnel automatically.

## Summary of env vars

| Variable | Required | Description |
|----------|----------|-------------|
| `CF_TUNNEL_TOKEN` | For permanent | Token from Cloudflare dashboard (Tunnels > Install connector) |
| `CF_TUNNEL_HOSTNAME` | For permanent | Your domain (e.g. `nw.example.com`) |
| `ENABLE_TUNNEL` | No | Set to `false` to disable tunnels entirely (default: `true`) |
