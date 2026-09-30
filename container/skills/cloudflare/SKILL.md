---
name: cloudflare
description: Configure Cloudflare Tunnel and Access for woltspace — initial setup, app domains, and user permissions.
user_invocable: true
---

# Woltspace Cloudflare

Single skill for everything Cloudflare-related on woltspace: the named tunnel that gives the lodge a permanent URL, the separate gateway domain that serves apps, and the Access policies that gate both surfaces.

This skill routes to a sub-doc based on what the user wants. Ask first — don't run any sub-doc by default.

## What do you want to do?

Ask the user which task they need, then read the matching sub-doc and follow it step by step.

| User intent | Sub-doc | When to use |
|-------------|---------|-------------|
| First-time tunnel + Access setup | `setup.md` | No named tunnel yet, or `CLOUDFLARE_TUNNEL_TOKEN` is unset in `/workspace/wolts/.env`. |
| Add or migrate the app domain | `app-domain.md` | The lodge already has a named tunnel, but remote apps need their own wildcard domain and gateway route. |
| Add a person to an existing app | `add-access.md` | Tunnel + Access already configured. Granting an email access to the lodge, the wildcard, or one specific subdomain. |
| Remove a person from an app | `remove-access.md` | Revoking a previously-granted email. |

Quick check for which state the user is in:

```bash
eval "$(grep -E '^CLOUDFLARE_[A-Z_]+=' "${WOLTSPACE_WOLTS_DIR:-$HOME/.woltspace/wolts}/.env" | sed 's/^/export /')"
echo "TUNNEL_TOKEN=${CLOUDFLARE_TUNNEL_TOKEN:+SET}"
echo "TUNNEL_URL=${CLOUDFLARE_TUNNEL_URL:-NOT SET}"
echo "API_TOKEN=${CLOUDFLARE_API_TOKEN:+SET}"
echo "ACCOUNT_ID=${CLOUDFLARE_ACCOUNT_ID:-NOT SET}"
```

- All four set → tunnel is up; use `app-domain.md` if remote apps do not have a separate domain, otherwise use `add-access.md` or `remove-access.md`.
- `TUNNEL_TOKEN` unset but `API_TOKEN` set → setup was started but tunnel never landed; resume `setup.md` from Step 4.
- All unset → fresh install; start at `setup.md` Step 1.

## Notes that apply to every sub-doc

- All sub-docs are **idempotent** — safe to re-run. If something is already in place, validate and skip.
- The shared `.env` lives at `/workspace/wolts/.env`. Every Cloudflare API call reads from there.
- Required token permissions for the API token: **Cloudflare Tunnel: Edit**, **DNS: Edit** (scoped to the user's zone), **Access: Apps and Policies: Edit**.
- Cloudflare Access free tier is 50 users per application. Each lodge or wildcard is one application; the limit doesn't stack across them.
- The Access auth cookie is scoped to `.{domain}`, so a user who logs into one subdomain stays logged in across every app they have access to for the session duration.
- Valid `session_duration` values: `30m`, `6h`, `12h`, `24h`, `168h`, `730h` (1 month is the max on the free plan).

## Architecture

```
Lodge browser → lodge.example.com → Cloudflare Access + Tunnel → lodge port
App browser   → app.theirapps.tld → Cloudflare Access + Tunnel → app gateway port
```

- **Auth at the edge** — unauthorized requests never reach the container.
- **Auto-reconnect** — named tunnels survive network blips, same URL persists across restarts.
- **Quick tunnels remain the default** — named tunnels only activate when env vars are set.
