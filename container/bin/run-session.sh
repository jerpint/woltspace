#!/usr/bin/env bash
# Wrapper for claude sessions spawned by the bot.
# Runs claude, captures exit code, writes structured status.
#
# Usage: run-session.sh <session-name> <work-dir> <prompt>

set -euo pipefail

SESSION_NAME="$1"
WORK_DIR="$2"
PROMPT="$3"

# Where we write structured status
STATE_DIR="${WOLT_STATE_DIR:-/workspace/wolts/.state}"
STATUS_DIR="$STATE_DIR/sessions"
mkdir -p "$STATUS_DIR"
STATUS_FILE="$STATUS_DIR/${SESSION_NAME}.json"

cd "$WORK_DIR"
export WOLT_SESSION="$SESSION_NAME"

# Write initial status
cat > "$STATUS_FILE" <<EOJSON
{"session": "$SESSION_NAME", "status": "running", "started": $(date +%s), "dir": "$WORK_DIR"}
EOJSON

# Read session routing to tell Claude which platform to notify
ROUTING_FILE="${WOLT_STATE_DIR:-/workspace/wolts/.state}/session-routing/${SESSION_NAME}.json"
NOTIFY_CONTEXT=""
if [ -f "$ROUTING_FILE" ]; then
  ADAPTER=$(python3 -c "import json,sys; d=json.load(open('$ROUTING_FILE')); print(d.get('adapter','telegram'))" 2>/dev/null || echo "telegram")
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

2-3 notifies max across the whole session."
fi

FULL_PROMPT="$PROMPT$NOTIFY_CONTEXT"

# Run claude — capture exit code
EXIT_CODE=0
claude --dangerously-skip-permissions "$FULL_PROMPT" || EXIT_CODE=$?

# Write final status
if [ "$EXIT_CODE" -eq 0 ]; then
    FINAL_STATUS="completed"
else
    FINAL_STATUS="failed"
fi

cat > "$STATUS_FILE" <<EOJSON
{"session": "$SESSION_NAME", "status": "$FINAL_STATUS", "exit_code": $EXIT_CODE, "started": $(date +%s), "finished": $(date +%s), "dir": "$WORK_DIR"}
EOJSON
