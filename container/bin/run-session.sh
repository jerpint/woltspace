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
This session was started from $NOTIFY_PLATFORM. Push messages to the user with \`notify \"your message\"\`.

IMPORTANT: The user reads your notify messages on $NOTIFY_PLATFORM. They should NOT have to open the session to see your work. Your final notify IS the deliverable.

Notify rules:
1. **Start**: one-liner ack. \"on it — building the playlist\" or \"got it, digging in\".
2. **Done**: Include the FULL substance of what you did or found. If they asked a question, answer it. If you reviewed code, include the findings. If you built something, describe it and link it. Write it like a message to a friend — not a formal report, but complete. Several paragraphs are fine. NEVER say \"see session\" or \"full report in session\" — put it HERE.
3 messages max. The done message is the important one — make it count."
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
