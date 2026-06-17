---
name: woltspace-access
description: Manage which users can access which wolts. Use when the admin asks to add a user, grant or revoke wolt access, or audit who has access to what. Wraps the `access` CLI that edits wolts/.space/auth/users.json.
---

# Access — Manage Wolt Permissions

The lodge supports per-user wolt permissions when `WOLTSPACE_AUTH=cloudflare`. This skill is how you (or any wolt) manages who's allowed to see what.

## How it works

The source of truth is `wolts/.space/auth/users.json` — a JSON file mapping emails to a list of wolts they can access. Use the `access` CLI to edit it cleanly. The server re-reads the file on every request — no restart needed.

## ⚠️ Honesty note

**This skill is a convenience layer, not a security boundary.** Any wolt session can edit users.json directly via the shell, with or without this skill. The skill exists to make admin tasks ergonomic, not to enforce who can perform them. Real OS-level enforcement is tracked in issue #354 (filesystem isolation). Until that ships, trust your wolts.

## Commands

```bash
access list                          # show all users + their allow-lists
access add EMAIL [WOLTS...]          # add a new user (optionally with allow-list)
access grant EMAIL WOLTS...          # add wolts to an existing user's allow-list
access revoke EMAIL WOLTS...         # remove wolts from a user's allow-list
access promote EMAIL                 # make admin (wolts=["*"])
access demote EMAIL                  # remove admin (wolts=[]) — keeps the user
access remove EMAIL                  # delete the user entry entirely
access check EMAIL WOLT              # does EMAIL have access to WOLT?
```

## Typical flows

**Add a new collaborator with access to one wolt:**
```bash
access add bob@example.com bloggo
```

**Grant access to additional wolts:**
```bash
access grant bob@example.com shared-wolt corework
```

**Audit:**
```bash
access list
access check bob@example.com bloggo
```

**Revoke without removing the user:**
```bash
access revoke bob@example.com corework
```

**Promote to admin (sees everything):**
```bash
access promote alice@example.com
```

## Two-step invite

Adding someone to users.json doesn't get them past Cloudflare Access — that's separate. The full flow:

1. Admin adds the new email to the Cloudflare Access policy (Zero Trust dashboard → Access → Applications → policies → emails). Gates the tunnel.
2. Admin runs `access add bob@example.com wolt-name` to grant in-app access.

Without step 1, the new user can't reach the lodge. Without step 2, they reach it but see "pending approval" (403).

## When to use this skill

- The admin asks to add or remove a user
- The admin asks who has access to a wolt
- A user reports "pending approval" — they need to be added to users.json
- A wolt should be made accessible to a specific person

## When NOT to use this skill

- The user isn't authenticated via Cloudflare Access (`WOLTSPACE_AUTH=none` mode) — there are no users to manage
- The change is to Cloudflare Access policies themselves — use the `woltspace-cloudflare` skill for that

## Confirm before destructive ops

Always confirm with the admin before running `remove`, `demote`, or revoking a user's last wolt. These can lock people out.
