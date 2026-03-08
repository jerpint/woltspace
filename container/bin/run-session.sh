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

# Run claude — capture exit code
EXIT_CODE=0
claude --dangerously-skip-permissions "$PROMPT" || EXIT_CODE=$?

# Write final status
if [ "$EXIT_CODE" -eq 0 ]; then
    FINAL_STATUS="completed"
else
    FINAL_STATUS="failed"
fi

cat > "$STATUS_FILE" <<EOJSON
{"session": "$SESSION_NAME", "status": "$FINAL_STATUS", "exit_code": $EXIT_CODE, "started": $(date +%s), "finished": $(date +%s), "dir": "$WORK_DIR"}
EOJSON
