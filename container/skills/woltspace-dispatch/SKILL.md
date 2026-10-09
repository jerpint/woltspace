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

Use atomic paste-buffer delivery. From Python (recommended):

```python
import subprocess
text = "Your full task description here, all on one line."
# Flatten newlines, add trailing \n for Enter
flat = text.replace("\n", " ") + "\n"
subprocess.run(["tmux", "set-buffer", flat], timeout=10)
subprocess.run(["tmux", "paste-buffer", "-t", SESSION_NAME], timeout=10)
```

From shell:

```bash
# Use printf to get a real trailing newline (shell $'...' or printf)
printf '%s\n' "Your full task description here." | tmux load-buffer -
tmux paste-buffer -t SESSION_NAME
```

**Important:** Send one message with everything. Multi-message dispatch risks the wolt
starting after the first message before seeing the full spec.

**Note:** The `_tmux_paste()` helper in `container/lib/sessions.py` implements this
pattern with a 10-second timeout, used by the bot for Telegram message delivery.

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
printf '%s\n' "Correction: also update X because Y" | tmux load-buffer -
tmux paste-buffer -t SESSION_NAME
```

### Step 6: Review and push

When the worker is done, review the diff:
```bash
git -C /workspace/wolts/projects/CLONE_DIR/ show --stat HEAD
git -C /workspace/wolts/projects/CLONE_DIR/ diff HEAD~1
```

Tell them to push:
```bash
printf '%s\n' "Looks good. Push it. Use gh-app-token for auth. Push to BRANCH on jerpint/woltspace. Clean up the remote URL after." | tmux load-buffer -
tmux paste-buffer -t SESSION_NAME
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

**Wolt ignores the spec:** It may have been in the middle of its own boot flow. Wait for the
`❯` prompt before sending.

**Worker refuses dispatch:** Workers may reject tasks from other AI agents if they involve
pushing to shared repos. This is a known IWCL friction point — workers can't verify the
human authorization chain. Workaround: have the orchestrator do the push, or have the
human confirm directly in the worker's session.

**OOM kills:** Too many opus processes. Check `ps aux | grep claude | wc -l`. Kill idle sessions
before spawning new ones.
