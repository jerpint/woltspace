#!/bin/bash
# Run a claude session in print mode and write a result file when done.
# Usage: run-session.sh <session-name> <prompt>

SESSION_NAME="$1"
PROMPT="$2"

if [ -z "$SESSION_NAME" ] || [ -z "$PROMPT" ]; then
  echo "usage: run-session.sh <session-name> <prompt>"
  exit 1
fi

WOLTS_DIR="${WOLTS_DIR:-/workspace/wolts}"
RESULTS_DIR="$WOLTS_DIR/.state/task-results"
LOG_FILE="/tmp/${SESSION_NAME}.log"
mkdir -p "$RESULTS_DIR"

# Run claude in print mode — no TUI, no dialogs
claude -p --dangerously-skip-permissions "$PROMPT" 2>&1 | tee "$LOG_FILE"

# Write result file for the bot to pick up
OUTPUT=$(tail -50 "$LOG_FILE" | grep -v '^$' | tail -30)
python3 -c "
import json, sys
output = sys.stdin.read()
result = {
    'type': 'done',
    'session': '$SESSION_NAME',
    'output': output
}
json.dump(result, open('$RESULTS_DIR/${SESSION_NAME}.json', 'w'))
" <<< "$OUTPUT"
