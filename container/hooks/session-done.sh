#!/bin/bash
# Hook: runs on Stop event for claude sessions
# Reads last_assistant_message from Claude Code and writes a result file

INPUT=$(cat)

# WOLT_SESSION is set by the bot when spawning task sessions
SESSION_NAME="${WOLT_SESSION:-}"
if [ -z "$SESSION_NAME" ]; then
  exit 0  # not a bot-spawned session
fi

WOLTS_DIR="${WOLTS_DIR:-/workspace/wolts}"
RESULTS_DIR="$WOLTS_DIR/.state/task-results"
mkdir -p "$RESULTS_DIR"

# Use Claude's own final message — already a natural summary
MESSAGE=$(echo "$INPUT" | jq -r '.last_assistant_message // empty')

python3 -c "
import json, sys
result = {
    'type': 'done',
    'session': '$SESSION_NAME',
    'message': sys.stdin.read()
}
json.dump(result, open('$RESULTS_DIR/${SESSION_NAME}.json', 'w'))
" <<< "$MESSAGE"

exit 0
