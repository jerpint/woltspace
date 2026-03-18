---
name: update
description: "Update woltspace platform — review incoming changes, warn about breaking changes, and pull with user consent. Use when asked to update woltspace."
user_invocable: true
---

# Update Woltspace

You are the lodge's gatekeeper for platform updates. Your job: check what's arriving from upstream, translate it into plain language (no git jargon), flag anything that could disrupt the lodge, and only open the gates when the human says go.

**Never pull without explicit user confirmation. This is the safety gate — updates can crash the running app.**

## Step 0: Check for local drift

Before anything else, check if the platform code has been modified inside the container. Wolts or Claude sessions sometimes accidentally edit files in `/workspace/woltspace`. If there's drift, a pull will fail with merge conflicts.

```bash
cd /workspace/woltspace
DRIFT=$(git status --short)
```

**If drift is found:**

1. Show the user what's changed:
```bash
git status --short
git diff --stat
```

2. Categorize the risk:
   - `container/entrypoint.sh`, `server.js`, `server/`, `woltspace` → **HIGH** — platform infrastructure
   - `container/bot/`, `container/creatures/` → **MEDIUM** — bot/creature code
   - `container/skills/`, `CLAUDE.md`, `*.md` → **LOW** — skills/docs, likely auto-updated

3. Notify the user about the drift and ask how to proceed:
   - **If LOW risk only:** "found some skill/doc changes in the platform dir — safe to discard. want me to reset and pull?"
   - **If MEDIUM/HIGH risk:** "found modifications to platform code — [list files]. these will be lost on pull. want me to save them to a branch first, or discard and proceed?"

4. If the user wants to save:
```bash
cd /workspace/woltspace
git checkout -b backup/pre-update-$(date +%Y%m%d-%H%M%S)
git add -A
git commit -m "pre-update snapshot — local modifications"
git checkout -  # return to previous branch
```

5. Reset to clean state before proceeding:
```bash
cd /workspace/woltspace
git checkout -- .
git clean -fd
```

**If clean (no drift):** proceed to Step 1.

## Step 1: Determine what's incoming

```bash
WOLT_DIR="${WOLT_DIR:-/workspace/wolts/${WOLT_NAME:-wolt}}"
BRANCH=$(cat "$WOLT_DIR/.state/woltspace-branch" 2>/dev/null || echo "main")

cd /workspace/woltspace
git fetch origin "$BRANCH" --tags
```

Check if there's actually anything new:
```bash
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")
```

If they match, tell the user they're already up to date and stop.

## Step 2: Determine the version bump type

```bash
CURRENT=$(cat /workspace/woltspace/.version 2>/dev/null || echo "v0.0.0")
LATEST_TAG=$(git describe --tags --abbrev=0 origin/$BRANCH 2>/dev/null || echo "untagged")
```

Parse the version components to determine the bump type:

```bash
# Parse current version (strip leading 'v')
CUR="${CURRENT#v}"
CUR_MAJOR=$(echo "$CUR" | cut -d. -f1)
CUR_MINOR=$(echo "$CUR" | cut -d. -f2)
CUR_PATCH=$(echo "$CUR" | cut -d. -f3)

# Parse latest tag
LAT="${LATEST_TAG#v}"
LAT_MAJOR=$(echo "$LAT" | cut -d. -f1)
LAT_MINOR=$(echo "$LAT" | cut -d. -f2)
LAT_PATCH=$(echo "$LAT" | cut -d. -f3)

# Determine bump type
if [ "$LAT_MAJOR" != "$CUR_MAJOR" ]; then
  BUMP="major"
elif [ "$LAT_MINOR" != "$CUR_MINOR" ]; then
  BUMP="minor"
else
  BUMP="patch"
fi
echo "Bump type: $BUMP ($CURRENT → $LATEST_TAG)"
```

Also check if a migration script exists for the target version:
```bash
MIGRATION="/workspace/woltspace/migrations/${LATEST_TAG}.sh"
# We'll check after pulling — the migration ships with the new code
```

## Step 3: Review the changes

Look at what's incoming:
```bash
git log --oneline HEAD..origin/$BRANCH
git diff --stat HEAD..origin/$BRANCH
```

Read the actual diff for anything that looks like it could break existing behavior. Also read the CHANGELOG.md from the incoming code if it exists:
```bash
git show origin/$BRANCH:CHANGELOG.md 2>/dev/null || echo "no changelog"
```

Focus on:
- **Config/env changes** — new required env vars, renamed vars, changed defaults
- **Removed or renamed files** — skills, cron scripts, bot tools that wolts might depend on
- **CLAUDE.md changes** — new instructions that change how sessions behave
- **entrypoint.sh / entrypoint_setup.py changes** — startup behavior, process management
- **Database/state format changes** — .state files, woltspace.json schema changes
- **Bot behavior changes** — tool renames, removed capabilities, changed routing

## Step 4: Report to the user

**Tone: lore-flavored, brief by default.** Lead with what's cool and new. Only go technical if the user asks or if something could break.

### Patch bump (safe)
> 🟢 **Patch update available** ($CURRENT → $LATEST_TAG)
> [1-2 sentence lore-flavored summary]. Safe to pull — no migration needed. Want me to bring it in?

### Minor bump (migration required)
> 🟡 **Minor update available** ($CURRENT → $LATEST_TAG) — migration required
> [summary of what's new]. This one needs a migration step: [plain-English description of what changes and what the user needs to do]. Want details, or proceed?

### Major bump (platform overhaul)
> 🔴 **Major update available** ($CURRENT → $LATEST_TAG) — significant changes
> [summary]. This is a big one: [description of what's changing]. I'd recommend reading the full changelog before pulling. Want me to walk through it?

Rules:
- Lead with the cool stuff, not the risk
- Name the risk clearly but briefly — don't bury it in lore
- Never say "breaking changes" without saying exactly what breaks and what the user needs to do
- Offer "want more details?" rather than front-loading everything

## Step 5: Confirm before pulling

**Use `AskUserQuestion` to wait for the user's explicit yes/no.** Do not skip this. Do not assume consent.

Accept any of: "yes", "pull it", "go ahead", "do it", "yes pull", "/update confirm".

Once confirmed:

```bash
cd /workspace/woltspace && git pull origin "$BRANCH"

# Stamp version (prefer tag if HEAD is tagged)
NEW_VERSION=$(git describe --tags --exact-match 2>/dev/null || git rev-parse --short HEAD)
mkdir -p "$WOLT_DIR/.state"
echo "$NEW_VERSION" > "$WOLT_DIR/.state/woltspace-version"
echo "$BRANCH" > "$WOLT_DIR/.state/woltspace-branch"
# Also update the .version file used at image build time
echo "$NEW_VERSION" > /workspace/woltspace/.version
```

## Step 5.5: Sync skills

After pulling, always re-copy skills so new sessions pick up the updated versions immediately. This is critical — skills are copied to `~/.claude/skills/` at container startup and not auto-updated on pull without this step.

```bash
cp -rT /workspace/woltspace/container/skills/ /home/node/.claude/skills/
echo "skills synced"
```

## Step 5.6: Sync dependencies

After pulling, always sync Python dependencies. New code may import packages that aren't installed yet — skipping this will crash the server or bot on next reload.

```bash
cd /workspace/woltspace
uv sync --project server 2>&1
uv sync --project container/bot 2>&1
```

This is fast (no-ops if deps haven't changed) and safe to run every time.

## Step 6: Run migration (minor/major bumps only)

After pulling, check for a migration script:

```bash
MIGRATION="/workspace/woltspace/migrations/${NEW_VERSION}.sh"
if [ -f "$MIGRATION" ]; then
  echo "Migration script found: $MIGRATION"
  cat "$MIGRATION"
fi
```

**For patch bumps:** skip migration entirely — just do a quick sanity check:
- Did the pull succeed cleanly?
- Are the key processes still running? (`curl -s http://localhost:7777/health` or similar)
- Report success.

**For minor/major bumps:** if a migration script exists, show it to the user and run it with their confirmation. The script handles the mechanical parts (moving files, updating configs). Flag anything that needs manual action (new env vars, etc.).

## Step 7: Verify and report

```bash
git log --oneline ORIG_HEAD..HEAD
```

Notify the user: what landed, the version, and any action items.

Action items to flag:
- **Bot code changed** → "the bot auto-reloads via watchfiles — should pick this up shortly"
- **entrypoint.sh or entrypoint_setup.py changed** → "container restart needed for this to take full effect"
- **New env vars** → "add `VAR_NAME` to your `.env` before restarting"
- **Server code changed** → "server auto-reloads via uvicorn --reload, should be live already"
- **Skills changed** → "skills re-synced — new sessions will get the updated versions. existing sessions keep the old version until they restart"

## Notes

- The platform code at `/workspace/woltspace` is a git clone inside the container — `git pull` works
- After pulling, the running image is now stale vs the container's filesystem. A `woltspace rebuild` from the host will re-bake the image for future cold starts, but isn't required for the current session
- The uvicorn server runs with `--reload` so Python server changes auto-apply
- Bot code auto-reloads via watchfiles
- When in doubt about whether a change is breaking, err on the side of flagging it
- If the user invokes `/update confirm` or already said "yes, pull it", skip straight to Step 5
