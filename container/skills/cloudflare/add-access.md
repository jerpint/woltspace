# Add Cloudflare Access Permission

Grant a specific email address access to one app (e.g. `blog.example.com`), the lodge, or all apps (`*.example.com`) via Cloudflare email OTP.

Users added here can visit the URL, enter their email, receive a one-time code, and get in. No password required.

This is idempotent — re-running with the same email just creates another allow-policy. Cloudflare ORs allow-policies, so duplicates don't break anything, but they clutter the dashboard. Check existing policies first if in doubt.

## Step 1: Gather info

Ask the user:
1. **Email to add** — e.g. `friend@example.com`
2. **Scope** — which domain? Examples:
   - The lodge only (e.g. `jerpint.example.com`)
   - One specific app (e.g. `blog.example.com`)
   - All apps (the wildcard, `*.example.com`)

## Step 2: Check env

```bash
source /workspace/wolts/.env
echo "API_TOKEN=${CLOUDFLARE_API_TOKEN:+SET}"
echo "ACCOUNT_ID=${CLOUDFLARE_ACCOUNT_ID:-NOT SET}"
```

If either is missing, the named tunnel hasn't been set up yet — point the user at `setup.md` first.

## Step 3: List existing Access apps

```bash
source /workspace/wolts/.env
curl -s "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/access/apps" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" | python3 -c "
import json, sys
r = json.load(sys.stdin)
for app in r['result']:
    print(f\"  {app['name']} → {app.get('domain','')}  (id: {app['id']})\")
"
```

Match the user's chosen scope to an existing app. Common cases:

- `*.<domain>` → `woltspace-apps` — all apps
- `<lodge-subdomain>.<domain>` → `woltspace-lodge` — the lodge only
- `<app>.<domain>` → an app-specific Access app (if it exists)

**If no app exists for the specific domain the user wants to scope to**, create one first (Step 3a), then add the policy (Step 4).

## Step 3a: Create a new scoped Access app (only if needed)

Skip this step if an Access app already covers the domain the user picked.

A more specific Access app (e.g. `blog.example.com`) takes precedence over the wildcard (`*.example.com`) at Cloudflare's edge. That's how you give one person access to a single app without exposing the rest.

```bash
source /workspace/wolts/.env
APP_RESULT=$(curl -s -X POST \
  "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/access/apps" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  -H "Content-Type: application/json" \
  --data "{
    \"name\": \"<app-name>\",
    \"domain\": \"<subdomain>.<domain>\",
    \"type\": \"self_hosted\",
    \"session_duration\": \"24h\"
  }")

APP_ID=$(echo "$APP_RESULT" | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['id'])")
echo "Created app: $APP_ID"
```

A freshly-created Access app has no policies, so it blocks everyone until Step 4 runs. Don't leave it half-configured.

## Step 4: Add the email policy

Replace `<APP_ID>`, `<EMAIL>`, and `<name>` with real values. `<name>` is just a label for the policy (e.g. `allow-friend-bob`).

```bash
source /workspace/wolts/.env
curl -s -X POST \
  "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/access/apps/<APP_ID>/policies" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  -H "Content-Type: application/json" \
  --data "{
    \"name\": \"allow-<name>\",
    \"decision\": \"allow\",
    \"precedence\": 1,
    \"include\": [{\"email\": {\"email\": \"<EMAIL>\"}}]
  }" | python3 -c "
import json, sys
r = json.load(sys.stdin)
if r.get('success'):
    print('Policy created:', r['result']['name'])
    print('Allows:', r['result']['include'])
else:
    print('FAILED:', r)
"
```

Per-email policies (instead of mutating one shared `allow-owner` include list) are intentional — they're easier to revoke later and easier to audit in the dashboard.

## Step 5: Confirm

Tell the user:
- What domain is now accessible to the new email
- That login works via email OTP (visit URL → enter email → check inbox for code)
- Which other apps are NOT affected (e.g. "this only grants access to `blog.example.com`, not the rest of `*.example.com`")
- The auth cookie is scoped to `.{domain}` so a single login covers every app the email has access to for the session duration

To remove access later, see `remove-access.md`.
