# Dispatch — IWCL Work Orchestration

Dispatch work to other wolts via IWCL (Inter-Wolt Communication Layer).
Use this when you have a well-defined task that another wolt should build.

## When to Use

- You have a spec or clear task description
- The work is better suited to another creature type (beaver for building, otter for quick tasks)
- You want to parallelize — dispatch to multiple wolts simultaneously
- You're a raccoon orchestrating a multi-feature build

## Pre-flight Checklist

Before spawning a worker, verify:

```bash
# 1. Check creds exist and aren't stale
ls -la /workspace/wolts/{WOLT}/.claude/.credentials.json
# Should be a real file (not symlink), 400+ bytes

# 2. Check if wolt has active sessions (proven auth)
tmux list-sessions 2>/dev/null | grep {WOLT}

# 3. Check system load — 4+ opus processes = OOM risk
ps aux | grep claude | wc -l
```

If creds are missing or stale, copy from shared:
```bash
cp /workspace/wolts/.claude/.credentials.json /workspace/wolts/{WOLT}/.claude/.credentials.json
```

## Dispatch Flow

### Step 1: Spawn the worker session

```bash
curl -s -X POST http://localhost:7777/sessions/new/lodge \
  -H "Content-Type: application/json" \
  -d '{"wolt": "WOLT_NAME"}'
```

Save the returned `name` — this is the tmux session name.

### Step 2: Wait for boot (45-60 seconds)

```bash
# Check if they got past login and finished /start-chat
tmux capture-pane -t SESSION_NAME -p -S -20 | tail -10
# Should show the wolt's greeting and a prompt (❯)
```

### Step 3: Send the spec

```bash
tmux send-keys -t SESSION_NAME "Your full task description here. Include:
1. What to build (clear spec)
2. Files to change (with descriptions)
3. Dev workflow (clone URL, branch name, test commands)
4. Constraints (don't edit production, commit often)" Enter
```

**Important:** Send one message with everything. Multi-message dispatch risks the wolt
starting after the first message before seeing the full spec.

**Important:** The text may paste but not submit. Always verify with `tmux capture-pane`
that the wolt started working (not still showing `[Pasted text #1 +N lines]`).
If stuck, send an extra `tmux send-keys -t SESSION_NAME Enter`.

### Step 4: Monitor progress

```bash
# What's on screen
tmux capture-pane -t SESSION_NAME -p -S -30

# Did they clone / make changes
git -C /workspace/wolts/projects/CLONE_DIR/ diff --stat

# Check commits
git -C /workspace/wolts/projects/CLONE_DIR/ log --oneline -5
```

### Step 5: Course-correct if needed

```bash
tmux send-keys -t SESSION_NAME "Correction: also update X because Y" Enter
```

### Step 6: Review and push

When the worker is done, review the diff:
```bash
git -C /workspace/wolts/projects/CLONE_DIR/ show --stat HEAD
git -C /workspace/wolts/projects/CLONE_DIR/ diff HEAD~1
```

Tell them to push:
```bash
tmux send-keys -t SESSION_NAME "Looks good. Push it. Use gh-app-token for auth. Push to BRANCH on jerpint/woltspace. Clean up the remote URL after." Enter
```

### Step 7: Create the PR

```bash
export GH_TOKEN=$(gh-app-token)
gh pr create --repo jerpint/woltspace --head BRANCH --base main \
  --title "feat: description" \
  --body "Summary + test plan + IWCL note (who built it)"
```

## Worker Selection

| Task Type | Best Creature | Why |
|-----------|--------------|-----|
| Platform code, multi-file | Raccoon (opus) | Subtle, needs deep context |
| Contained feature build | Beaver (sonnet) | Fast, reliable, follows specs |
| Quick fix, one file | Otter (haiku) | Cheap, fast |
| UX review, design | Raccoon (opus) | Taste and judgment |

## Dev Workflow Template

Include this in every dispatch message:

```
DEV WORKFLOW:
- Clone: git clone https://github.com/jerpint/woltspace.git /workspace/wolts/projects/FEATURE/
- cd /workspace/wolts/projects/FEATURE/
- Branch: git checkout -b uxw/FEATURE
- Test: uv run --project server --with pytest pytest test/
- Commit often. Do NOT edit /workspace/woltspace/ — that's production.
- Push with gh-app-token when ready.
```

## Troubleshooting

**Wolt stuck at login screen:** Stale credentials. Kill the session, copy fresh creds, respawn.

**Message pasted but not submitted:** Send `tmux send-keys -t SESSION Enter` to submit.

**Wolt ignores the spec:** It may have been in the middle of its own boot flow. Wait for the
`❯` prompt before sending.

**OOM kills:** Too many opus processes. Check `ps aux | grep claude | wc -l`. Kill idle sessions
before spawning new ones.
