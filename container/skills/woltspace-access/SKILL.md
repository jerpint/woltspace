---
name: woltspace-access
description: Turn on multi-user auth, manage which users can access which wolts. Use when the operator asks to enable per-user permissions, add a user, grant or revoke wolt access, or audit who has access to what. Wraps the `access` CLI that edits wolts/.space/auth/users.json.
---

# Access — Multi-User Permissions

By default a woltspace container is single-tenant: anyone past the Cloudflare tunnel sees and controls every wolt. This skill is for the opt-in mode where each user only sees the wolts in their personal allow-list.

There are two flows: **first-time setup** (turn auth on) and **day-to-day management** (add/grant/revoke users).

## ⚠️ Honesty note

This skill is a convenience layer, **not a security boundary**. Any wolt session can edit `wolts/.space/auth/users.json` directly via the shell, with or without this skill. The skill exists to make admin tasks ergonomic, not to enforce who can perform them. Real OS-level enforcement is tracked in issue #354. Until that lands, trust the people you let into the container.

---

## Flow 1 — First-time auth setup

When the operator says "turn on multi-user auth" or "enable per-user permissions":

### 1. Seed the first user

The very first thing — before flipping the toggle — is to put the operator into `users.json` so they don't lock themselves out:

```bash
access add OPERATOR_EMAIL '*'
```

`'*'` is the wildcard for "every wolt." The single quotes are important so the shell doesn't expand the asterisk.

### 2. Configure Cloudflare Access env vars

Add these to `.env` (or wherever the container reads its env from):

```bash
WOLTSPACE_AUTH=cloudflare
WOLTSPACE_CF_TEAM_DOMAIN=yourteam.cloudflareaccess.com
WOLTSPACE_CF_AUD=<application-audience-tag-from-cloudflare-zero-trust>
```

The team domain is the `*.cloudflareaccess.com` subdomain in your Cloudflare Zero Trust dashboard. The AUD tag is on the Access application — Settings → Application Audience (AUD) tag.

### 3. Restart the server

Restart the FastAPI server (or the whole container) so the new env vars take effect and the auth middleware loads.

### 4. Log in to the lodge

Visit the lodge URL. Cloudflare Access prompts for OTP email login. The JWT lands at the server, middleware validates it, recognizes the operator email, lets them in.

If `WOLTSPACE_AUTH=cloudflare` is set but the user isn't in `users.json`, they get a 403 "access denied" — that's why step 1 matters.

---

## Flow 2 — Day-to-day user management

### Add a new user

First add them to the Cloudflare Access policy (Zero Trust dashboard → Access → Applications → policies → emails). This gates the tunnel.

Then add them to `users.json`:

```bash
access add bob@example.com bloggo
```

Bob now has access to the "bloggo" wolt (only). Without step 1, Bob can't reach the lodge. Without step 2, he reaches it but gets 403.

### Grant additional wolts

```bash
access grant bob@example.com shared-wolt corework
```

### Revoke specific wolts

```bash
access revoke bob@example.com corework
```

### Wildcard access (sees everything)

```bash
access add alice@example.com '*'
```

Or for an existing user — `access` doesn't have a "set" command on purpose. To switch a user to wildcard, remove + re-add, or hand-edit the JSON file.

### Remove a user entirely

```bash
access remove bob@example.com
```

### Audit

```bash
access list
access check bob@example.com bloggo
```

`check` exits 0 if allowed, 2 if denied — handy for scripts.

---

## Self-onboarding (browser-driven)

When `WOLTSPACE_AUTH=cloudflare` is on and a user creates a wolt through the lodge UI, the server auto-appends that wolt to the creator's allow-list. So a user with `wolts: []` can still create their first wolt and immediately use it — no need for the operator to grant it manually.

This means: to onboard a new collaborator who'll have their own wolts, just `access add them@email.com` (with empty allow-list), and let them create what they need.

To share an existing wolt with them, use `grant`.

---

## Confirm before destructive ops

Always confirm with the operator before running `remove` or revoking a user's last wolt. These can lock people out of their own work.
