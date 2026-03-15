---
name: update
description: "Update woltspace platform — review incoming changes, warn about breaking changes, and pull with user consent. Use when asked to update woltspace."
user_invocable: true
---

# Update Woltspace

You are the lodge's gatekeeper for platform updates. Your job: check what's arriving from upstream, translate it into plain language (no git jargon), flag anything that could disrupt the lodge, and only open the gates when the human says go.

**Never pull without explicit user confirmation. This is the safety gate — updates can crash the app.**

`/workspace/woltspace` is volume-mounted from the host, so `git pull` updates files live — no rebuild needed in most cases.

## Step 1: Determine what's incoming

```bash
WOLT_DIR="${WOLT_DIR:-/workspace/wolts/${WOLT_NAME:-wolt}}"
BRANCH=$(cat "$WOLT_DIR/.state/woltspace-branch" 2>/dev/null || echo "staging")

cd /workspace/woltspace
git fetch origin "$BRANCH"
```

Check if there's actually anything new:
```bash
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")
```

If they match, tell the user they're already up to date and stop.

## Step 2: Review the changes

Look at what's incoming:
```bash
git log --oneline HEAD..origin/$BRANCH
git diff --stat HEAD..origin/$BRANCH
```

Read the actual diff for anything that looks like it could break existing behavior. Focus on:

- **Config/env changes** — new required env vars, renamed vars, changed defaults
- **Removed or renamed files** — skills, cron scripts, bot tools that wolts might depend on
- **CLAUDE.md changes** — new instructions that change how sessions behave
- **entrypoint.sh changes** — startup behavior, process management
- **woltspace CLI changes** — flag changes, new required args
- **Database/state format changes** — .state files, woltspace.json schema changes
- **Bot behavior changes** — tool renames, removed capabilities, changed routing

If a merge PR exists for the incoming commits, read it for context. Check if there's a changelog or migration notes.

## Step 3: Report to the user

**Tone: lore-flavored, brief by default.** Lead with what's cool and new — speak like you're reporting back to the lodge, not filing a ticket. Only go technical if the user asks, or if something could break.

Send a notify with your assessment. Structure it as:

**If safe (no breaking changes):**
> [1-2 sentence lore-flavored summary of what's new — e.g. "the wolf learned to read the stars" or "dog now barks before thinking"]. Good to pull — no side effects. Want me to bring it in?

**If there are concerns:**
> [lore summary of what's new]. One thing to know first: [plain-English description of the specific risk — e.g. "needs a new OPENAI_API_KEY in .env" or "wolf skill was renamed, existing schedules may need a look"]. Want details, or proceed?

Rules:
- Lead with the cool stuff, not the risk
- Name the risk clearly but briefly — don't bury it in lore
- Never say "breaking changes" without saying exactly what breaks and what the user needs to do
- Offer "want more details?" rather than front-loading everything

## Step 4: Confirm before pulling

**Use `AskUserQuestion` to wait for the user's explicit yes/no.** Do not skip this. Do not assume consent. A raw "what's new?" message is not a pull request.

Accept any of: "yes", "pull it", "go ahead", "do it", "yes pull", "/update confirm".

Once confirmed:

```bash
cd /workspace/woltspace && git pull origin "$BRANCH"

# Stamp version
NEW_VERSION=$(git rev-parse HEAD)
mkdir -p "$WOLT_DIR/.state"
echo "$NEW_VERSION" > "$WOLT_DIR/.state/woltspace-version"
echo "$BRANCH" > "$WOLT_DIR/.state/woltspace-branch"
```

## Step 5: Verify and report

```bash
git log --oneline ORIG_HEAD..HEAD
```

Notify the user: what landed, the short hash, and any action items.

Action items to flag:
- **Bot code changed** → "the bot needs a restart to pick this up — run `woltspace restart` from host"
- **entrypoint.sh changed** → "container restart needed for this to take full effect"
- **New env vars** → "add `VAR_NAME` to `~/wolts/.env` before restarting"
- **server.js changed** → "server auto-reloads via --watch, should be live already"

## Notes

- `woltspace rebuild` is a **host-only** command — no docker CLI inside the container
- `server.js` runs with `--watch` so JS changes auto-apply without restart
- The Telegram bot does NOT auto-restart — if bot code changed, flag it explicitly
- When in doubt about whether a change is breaking, err on the side of flagging it
- If the user invokes `/update confirm` or already said "yes, pull it", skip straight to Step 4
