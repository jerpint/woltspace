# IWCL — Inter-Wolt Communication Layer

IWCL lets wolts communicate with each other. Uses `tmux set-buffer` + `paste-buffer` to deliver
messages into running Claude Code sessions. Atomic, reliable, no new infrastructure needed.

## How It Works

```
Orchestrator (raccoon)
    │
    ├── spawn worker via API ──→ POST /sessions/new/lodge
    │
    ├── send spec via tmux ────→ set-buffer + paste-buffer (atomic paste)
    │
    ├── monitor progress ──────→ tmux capture-pane + filesystem checks
    │
    └── review + push ─────────→ git diff, then tell worker to push
```

The orchestrator (typically a raccoon) breaks work into tasks, spawns worker wolts,
dispatches specs, monitors progress, and reviews output.

## Message Transport

**Current:** `tmux set-buffer` + `tmux paste-buffer -t SESSION_NAME`

Text is loaded into a tmux buffer and pasted atomically into the target pane.
A trailing `\n` in the buffer is converted to a carriage return (Enter), so the
message is submitted in one operation. The wolt receives it as a normal user message.

The `_tmux_paste()` helper in `container/lib/sessions.py` implements this pattern
with a 10-second timeout to prevent stuck tmux calls from blocking the bot event loop.

**Why not `send-keys`?** The previous approach (`tmux send-keys -t SESSION -l "text"`)
sent each character as an individual keystroke. On long messages, the pane input buffer
backed up, blocking the entire process (and the bot event loop with it). One stuck
`send-keys` could freeze all Telegram routing. PR #304 replaced this with atomic
paste-buffer delivery.

**Constraints:**
- Target session must be a live tmux session
- Claude must be at a prompt (not in the middle of a tool call)
- Text must be pre-flattened (newlines replaced with spaces) — only the trailing `\n` triggers Enter
- No authentication — any wolt can message any other wolt

## Dispatch Protocol

See the `/woltspace-dispatch` skill for the full step-by-step protocol.

Quick version:
1. Write a spec
2. Verify worker's credentials (pre-flight check)
3. Spawn worker session via API
4. Wait for boot (~60 seconds)
5. Send spec via paste-buffer
6. Monitor with capture-pane and filesystem checks
7. Review diff, tell worker to push
8. Create PR

## Proven Patterns

### Multi-wolt parallel build (Session 31, 2026-04-04)

UXWolt (raccoon/opus orchestrator) dispatched two features simultaneously:
- **nunu** (beaver/sonnet) → Projects as Direct Ports → PR #259
- **clouseauw** (raccoon/opus) → Session Resume → PR #258

Both completed independently in ~20 minutes. Specs were self-contained, workers
cloned repos into `/workspace/wolts/projects/`, worked on feature branches, and pushed.

### Direct wolt-to-wolt message (Session 27, 2026-03-30)

UXWolt sent viewport fix instructions directly to nunu's running session.
She received it, processed it, and pushed an updated viewport.

### Authorization friction (Session 47, 2026-04-12)

UXWolt dispatched nunu to update docs on PR #260. Nunu refused to push to a shared
repo on an AI-to-AI authorization chain — she wanted to hear from the human directly.
This is a real IWCL design gap: workers have no way to verify that the orchestrator
is acting on human authority. Future phases should address this (signed dispatch
messages, audit trail, or human-in-the-loop confirmation for destructive actions).

## Future Design

### Phase 1: Manual dispatch (current)
- `set-buffer` + `paste-buffer` for transport
- `/woltspace-dispatch` skill documents the protocol
- Orchestrator manages everything manually

### Phase 2: Inbox model
- `wolts/.state/{wolt}/inbox/` — persistent message queue
- JSON files: `{timestamp}-{from}-{subject}.json`
- `/start-chat` checks inbox on boot: "you have 2 unread messages"
- Survives session death — messages wait for next boot

### Phase 3: Bidirectional status
- Workers report progress back to orchestrator's inbox
- Structured status updates: `{"type": "progress", "percent": 80, "message": "tests passing"}`
- Orchestrator polls or gets notified

### Phase 4: Autonomous orchestration
- Wolf cron triggers raccoon orchestrator
- Raccoon reads GitHub issues, breaks into tasks
- Dispatches to beavers, reviews output
- Opens PRs for human review
- Full autonomous development loop
