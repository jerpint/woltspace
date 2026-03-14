#!/usr/bin/env bash
# Wrapper for claude sessions spawned by the bot.
# Runs claude, captures exit code, updates the session registry.
#
# Usage: run-session.sh <session-name> <work-dir> <prompt> [model]

set -euo pipefail

SESSION_NAME="$1"
WORK_DIR="$2"
PROMPT="$3"
MODEL="${4:-}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SESSION_REG="$SCRIPT_DIR/session-reg"

cd "$WORK_DIR"
export WOLT_SESSION="$SESSION_NAME"

# Generate a stable Claude session ID so we can --resume the right conversation later
CLAUDE_SESSION_ID=$(python3 -c "import uuid; print(uuid.uuid4())")
$SESSION_REG update "$SESSION_NAME" "claude_session_id=$CLAUDE_SESSION_ID" > /dev/null 2>&1 || true

# Generate a short descriptive title from the prompt (first line, ~60 chars, clean)
TITLE=$(python3 -c "
import re, sys
p = sys.argv[1]
first_line = p.split('\n')[0].split('.')[0].strip()
clean = re.sub(r'[^\w\s-]', '', first_line).strip()
print(clean[:60].lower())
" "$PROMPT" 2>/dev/null || echo "")

# Update registry with title (session was already created by core.py with routing info)
$SESSION_REG update "$SESSION_NAME" "title=$TITLE" > /dev/null 2>&1 || true

# Read adapter from registry to tell Claude which platform to notify
ADAPTER=$($SESSION_REG get-field "$SESSION_NAME" adapter 2>/dev/null || echo "telegram")
NOTIFY_CONTEXT=""
if [ -n "$ADAPTER" ] && [ "$ADAPTER" != "" ]; then
  case "$ADAPTER" in
    slack)    NOTIFY_PLATFORM="Slack" ;;
    telegram) NOTIFY_PLATFORM="Telegram" ;;
    *)        NOTIFY_PLATFORM="$ADAPTER" ;;
  esac
  NOTIFY_CONTEXT="
---
## How this session works

You were dispatched from $NOTIFY_PLATFORM by a developer. They're reading your updates on their phone or chat — they are NOT watching this terminal. Your \`notify\` messages are the primary way they see your work.

Think of it like messaging a dev colleague on $NOTIFY_PLATFORM: you do the work here, then message them the results directly. They may never open this session.

Use \`notify \"your message\"\` to send messages.

**When you start**: one-liner ack. \"on it — reviewing the loop\" or \"got it, digging in.\"

**When you're done**: Send a complete summary via notify — all key findings, decisions, and results. The reader should get full context without opening the session. But write it for chat, not a terminal — short paragraphs, no code blocks or formatted logs. Think \"messaging a colleague your conclusions\" not \"pasting terminal output.\" Be thorough but digestible. NEVER say \"see session\" or \"report in session.\"

Also: always print your full detailed output (code, logs, raw analysis) to this terminal too — it stays in the session for anyone who opens the live view later.

2-3 notifies max across the whole session.

## Viewport — show your work

You're running inside a split view: terminal on the left, viewport (iframe) on the right. The developer can see whatever you push to the viewport. Use the \`/viewport\` skill whenever you produce something visual — HTML pages, dashboards, diagrams, reports, apps. Don't just write the file; push it so they can see it live.

Any file you write to \`wolt/site/\` is served at the root (e.g. \`wolt/site/foo.html\` → \`/foo.html\`). After writing it, push to the viewport (this is ONLY for the viewport — do NOT use curl for sending messages):
\`\`\`bash
curl -s -X POST \"http://localhost:3000/current?session=\$(tmux display-message -p '#S')\" \\
  -H 'Content-Type: application/json' \\
  -d '{\"url\": \"/foo.html\"}'
\`\`\`

Rule of thumb: if you created an artifact someone would want to look at, push it to the viewport.

**IMPORTANT**: To send a message to the developer, ALWAYS use \`notify \"your message\"\`. Never call /notify via curl directly — the notify script handles session routing, emoji prefix, and Telegram delivery correctly.

## CRITICAL: Do not touch infrastructure

**NEVER restart, kill, or modify server.js (port 3000)** — it runs the tunnel, split view, and all session routing. Restarting it breaks everything for everyone. If something seems wrong with the server, notify the developer and stop. Do not attempt to fix infrastructure yourself."
fi

FULL_PROMPT="$PROMPT$NOTIFY_CONTEXT"

# Run claude — capture exit code
EXIT_CODE=0
MODEL_FLAG=""
if [ -n "$MODEL" ]; then
    MODEL_FLAG="--model $MODEL"
fi
claude --dangerously-skip-permissions --session-id "$CLAUDE_SESSION_ID" $MODEL_FLAG "$FULL_PROMPT" || EXIT_CODE=$?

# Update registry with final status
$SESSION_REG finish "$SESSION_NAME" "$EXIT_CODE" > /dev/null 2>&1 || true

# Reset viewport to placeholder so it doesn't show "not found" for dead content
$SESSION_REG update "$SESSION_NAME" "viewport_url=/placeholder.html" > /dev/null 2>&1 || true
