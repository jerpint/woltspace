---
name: migrate-to-projects
description: Help a user who has accidentally edited platform code in /workspace/woltspace/. Detects drift, extracts their work into wolt/projects/, and resets the platform to clean state.
---

# Migrate to Projects

Your human (or a previous session) edited files in `/workspace/woltspace/` — the platform code. This causes problems: when the platform updates, those changes get overwritten or cause merge conflicts. Your job is to safely extract their work into `wolt/projects/` and restore the platform to a clean state.

**Be careful. This is a recovery operation. Don't lose anyone's work.**

## Step 1: Assess the damage

Check what's been modified in the platform directory:

```bash
cd /workspace/woltspace
git status
git diff --stat
```

Categorize the changes:
- **User code** — new files, new features, apps they built (these need to be saved)
- **Config tweaks** — changes to server.js, entrypoint, bot code (these need to be understood, then discarded)
- **Untracked files** — new files that aren't part of the platform (likely user work)

Tell the human what you found. Be specific: "You have 3 modified files and 5 new files in the platform directory. Here's what I see..."

## Step 2: Extract user work

For each piece of user work you find:

1. Create a project directory: `mkdir -p wolt/projects/{name}`
2. Copy the files there: `cp -r /workspace/woltspace/{path} wolt/projects/{name}/`
3. If it's an app that was running on a port, note the port in project.json
4. If it depends on platform code (imports from server.js, etc.), refactor those dependencies out — the project should be self-contained

Example extractions:
- Custom API endpoint added to server.js → extract into its own FastAPI/Express server in a project
- Modified bot behavior → document what they wanted in a memory file, suggest filing an issue
- New HTML pages in public/ → move to wolt/site/ or a project
- New scripts in container/bin/ → move to a project

## Step 3: Document config changes

Some changes are config tweaks rather than new code. For these:
- Note what was changed and why (ask the human if unclear)
- Check if there's a proper way to achieve the same thing (env vars, wolt-specific overrides, skill customization)
- If it's a legitimate feature request, suggest filing an issue on the woltspace repo

Write a summary to `wolt/memory/archive/platform-migration.md`:

```markdown
# Platform Migration — {date}

## What was found
- {list of changes in /workspace/woltspace/}

## What was extracted
- {project name} → wolt/projects/{name}/ (was: {original location})

## Config changes discarded
- {change}: {reason it was there}, {proper alternative if any}

## Follow-up needed
- {any feature requests or issues to file}
```

## Step 4: Reset the platform

Once everything is safely extracted and the human confirms:

```bash
cd /workspace/woltspace
git stash  # safety net — can recover with git stash pop if something was missed
```

If git stash isn't enough (untracked files):

```bash
# Show what would be removed first
git clean -n -d

# Only after human confirms
git clean -f -d
git checkout .
```

**CRITICAL: Always show the human what you're about to discard before doing it. Get explicit confirmation.**

## Step 5: Verify

1. Check the platform is clean: `cd /workspace/woltspace && git status` → should be clean
2. Check extracted projects work: start their servers, verify functionality
3. Push project changes to viewport so the human can verify
4. Update wolt memory with what happened

## Step 6: Pull latest platform

If the platform was behind on updates:

```bash
cd /workspace/woltspace
git pull origin main
```

This should now work cleanly since there are no local modifications.

## Common patterns

### "I added an endpoint to server.js"
Extract the logic into a standalone server in `wolt/projects/{name}/`. Set up project.json with a port. The platform will proxy `/project/{name}/` to it automatically.

### "I modified the bot behavior"
Check if it can be done via:
- A wolt-specific skill override (`.claude/skills/`)
- Memory files that shape the bot's system prompt
- Environment variables

If not, it's a feature request for woltspace.

### "I added files to public/"
Move to `wolt/site/` (served at the root) or to a project.

### "I edited the Dockerfile or entrypoint"
These are core platform. Document what was needed and why. This is almost certainly a feature request — suggest filing an issue.

## Tone

Be empathetic. The human didn't do anything wrong — the guardrails weren't in place yet. Frame this as "let's organize your work properly" not "let's fix your mistakes." Their code is valuable; it just needs a better home.
