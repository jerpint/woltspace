---
name: woltspace-access
description: Turn on multi-user auth, manage which users can access which wolts, and troubleshoot access. Use when the operator asks to enable per-user permissions, add a user, grant or revoke wolt access, audit who has access, or debug "I see no wolts". Wraps the `access` CLI (edits wolts/.space/auth/users.json) and the Cloudflare Access layer.
---

# Access — Multi-User Permissions

By default a woltspace container is single-tenant: anyone past the Cloudflare tunnel sees and controls every wolt. This skill is for the opt-in mode where each user only sees the wolts in their personal allow-list.

**Two layers, both required:**
1. **Cloudflare Access** — controls *who can reach the lodge at all* (the front door). Gated at the edge by email OTP.
2. **users.json** — controls *what each authenticated user sees inside* (per-wolt allow-list).

A user needs to be in BOTH: the CF Access policy (to get through the tunnel) and `users.json` (to see any wolts).

## ⚠️ Honesty note

This skill is a convenience layer, **not a security boundary**. Any wolt session can edit `wolts/.space/auth/users.json` directly via the shell, with or without this skill. The skill makes admin tasks ergonomic; it does not enforce who can perform them. Real OS-level enforcement is tracked in issue #354. Until that lands, trust the people you let into the container.

---

## How it works (mental model)

- Every request through the tunnel (`yourname.woltspace.com`) carries a Cloudflare-signed JWT with the user's email. The server validates it and looks the email up in `users.json`.
- The wildcard `"*"` in a user's `wolts` list means "every wolt." It's a convenience, not a role — there is no admin concept in this MVP.
- Apps inherit access from their **keeper wolt** (the wolt that owns them). See an app iff you can see its keeper.
- Requests that didn't traverse Cloudflare (direct `localhost`) have no JWT — see the localhost section below.
- Config is read from `wolts/.env` live, so editing it + reloading the server (no full container restart) is enough.

---

## Flow 1 — First-time auth setup

When the operator says "turn on multi-user auth" / "enable per-user permissions":

### 1. Seed the operator into users.json FIRST

Before flipping the toggle, add the operator so they don't lock themselves out:

```bash
access add OPERATOR_EMAIL '*'
```

`'*'` = every wolt. Single-quote it so the shell doesn't glob.

### 2. Find the Cloudflare config values

You need the team domain and the lodge app's AUD tag. If `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID` are in `.env`, look them up directly:

```bash
# team domain — the redirect target on an unauthenticated request
curl -sI https://YOURNAME.woltspace.com | grep -i location
# → .../cdn-cgi/access/login/...  the host is your team domain
#   (e.g. jerpint.cloudflareaccess.com)

# AUD tag for the lodge app
. /workspace/wolts/.env && curl -s -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/access/apps" \
  | python3 -c "import sys,json; [print(a['name'], a['aud'], a['domain']) for a in json.load(sys.stdin)['result']]"
# → pick the row whose domain is your lodge (e.g. jerpint.woltspace.com)
```

Or read both from the Cloudflare Zero Trust dashboard: Settings → Custom Pages shows the team domain; Access → Applications → your lodge app → Overview shows the AUD tag.

### 3. Configure env vars

Add to `wolts/.env`:

```bash
WOLTSPACE_AUTH=cloudflare
WOLTSPACE_CF_TEAM_DOMAIN=yourteam.cloudflareaccess.com
WOLTSPACE_CF_AUD=<application-audience-tag>
```

### 4. Reload the server

Config is read from `.env` live, but the JWKS cache + module state want a clean reload. Touch the server entry so uvicorn reloads:

```bash
touch /workspace/woltspace/server/app.py
```

(Or restart the container. A full rebuild is only needed if `PyJWT` isn't installed yet — see Troubleshooting.)

### 5. Verify with /auth/debug

From inside the container (loopback only):

```bash
curl -s http://localhost:7777/auth/debug | python3 -m json.tool
```

Confirm: `auth_mode: cloudflare`, `pyjwt_installed: true`, `team_domain` set, `aud_set: true`, and your `users_emails` listed. `last_auth_error` should be `null`.

### 6. Log in

Visit the lodge URL. Cloudflare OTP-logs you in, the JWT reaches the server, you see your wolts. A CF-authenticated user who isn't in `users.json` gets a 403 "pending approval" — that's why step 1 matters.

### 7. (Optional) localhost browser access

With auth on, `localhost:7777` shows no wolts: Docker's `-p 7777:7777` presents your host browser as the bridge gateway (e.g. `172.17.0.1`), not `127.0.0.1`, and it carries no JWT. To allow it:

```bash
WOLTSPACE_AUTH_TRUST_LOCAL=true
```

> ⚠️ The port binds `0.0.0.0`, so it's reachable from your whole LAN, and every such caller looks like the same private address inside the container. With this flag on, **anyone on your network who can reach `your-machine-ip:7777` gets unauthenticated full access.** Only enable on a trusted network. Default OFF. Genuine in-container loopback (notify, access, push-view CLIs) is always trusted regardless of this flag; remote users always go through the tunnel + CF Access.

---

## Flow 2 — Day-to-day user management

### Add a new user (two steps)

**Step A — Cloudflare Access** (lets them reach the lodge). Add their email to the lodge app's policy. Via dashboard: Zero Trust → Access → Applications → your lodge app → Policies → add email. Via API (if your token has `Access: Apps and Policies: Edit`):

```bash
. /workspace/wolts/.env
APP_AUD=<lodge-app-id>   # the app id, same as its AUD tag
# Create a small dedicated policy (works even with create-only token scope):
curl -s -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" -H "Content-Type: application/json" \
  "https://api.cloudflare.com/client/v4/accounts/$CLOUDFLARE_ACCOUNT_ID/access/apps/$APP_AUD/policies" \
  -d '{"name":"allow-NAME","decision":"allow","precedence":2,"include":[{"email":{"email":"bob@example.com"}}]}'
```

> Token-scope gotcha: a token with only create scope returns `10405 "method not allowed"` on PUT/DELETE. You can still CREATE new policies (multiple allow-policies stack — any match lets the user in), but can't edit/delete existing ones. To get full control, add **Account → Access: Apps and Policies → Edit** to the token at dash.cloudflare.com/profile/api-tokens.

**Step B — users.json** (controls what they see inside):

```bash
access add bob@example.com bloggo
```

Without Step A, Bob can't reach the lodge. Without Step B, he reaches it but sees nothing (403 pending-approval).

### Other commands

```bash
access grant bob@example.com shared-wolt corework   # add wolts
access revoke bob@example.com corework              # remove specific wolts
access add alice@example.com '*'                    # wildcard (sees everything)
access remove bob@example.com                       # delete the user entry
access list                                         # show all users + allow-lists
access check bob@example.com bloggo                 # exit 0 = allowed, 2 = denied
```

There's no "set" command by design — to switch a user to wildcard, `remove` + re-`add`, or hand-edit the JSON.

---

## Self-onboarding (browser-driven)

When auth is on and a user creates a wolt through the lodge UI, the server auto-appends that new wolt to the creator's allow-list. So a user with `wolts: []` can create their first wolt and immediately use it — no manual grant needed.

To onboard a collaborator who'll have their own wolts: `access add them@email.com` with an empty allow-list (still do the CF Access step), and let them create what they need. To share an *existing* wolt, use `grant`.

---

## Troubleshooting

**"I see no wolts" (operator or user):** run `curl -s http://localhost:7777/auth/debug | python3 -m json.tool` from inside the container and check, in order:

- `pyjwt_installed: false` → the server venv is missing PyJWT. Rebuild the container (`woltspace rebuild`) so `uv sync` installs it, or as a stopgap `uv pip install --python /workspace/woltspace/server/.venv/bin/python "PyJWT[crypto]"`. This is the #1 cause — a missing decoder makes every request anonymous.
- `auth_mode` not `cloudflare` → env var not picked up; check `wolts/.env` and reload.
- `team_domain: null` or `aud_set: false` → missing `WOLTSPACE_CF_TEAM_DOMAIN` / `WOLTSPACE_CF_AUD`.
- `last_auth_error` non-null → it tells you exactly what failed (wrong AUD, kid not in JWKS, JWKS unreachable, etc).
- On localhost specifically, `request_email` will be `__local__` only if loopback/trust-local applies — otherwise it's `null` and you see nothing (set `WOLTSPACE_AUTH_TRUST_LOCAL=true`, step 7).

**User stuck at Cloudflare login / can't reach lodge:** they're not in the CF Access policy (Step A), independent of users.json.

**Rolling back entirely:** set `WOLTSPACE_AUTH=none` in `wolts/.env` and reload. Middleware becomes a no-op; everyone sees everything again. `users.json` stays on disk, dormant.

---

## Confirm before destructive ops

Always confirm with the operator before `remove`, revoking a user's last wolt, or deleting a Cloudflare policy — these can lock people out of their own work.
