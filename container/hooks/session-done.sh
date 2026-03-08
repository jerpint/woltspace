#!/bin/bash
# Hook: runs on SessionEnd for task sessions
# Captures terminal output and writes a result file the bot can pick up

INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty')
CWD=$(echo "$INPUT" | jq -r '.cwd // empty')

# Only care about task sessions (spawned by bot)
WOLTS_DIR="${WOLTS_DIR:-/workspace/wolts}"
RESULTS_DIR="$WOLTS_DIR/.state/task-results"
mkdir -p "$RESULTS_DIR"

# Try to figure out the tmux session name from cwd
# Task sessions are named <wolt>-<id>
WOLT_NAME=$(basename "$CWD" 2>/dev/null)

# Find the most recent task session for this wolt
TASK_SESSION=$(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep "^${WOLT_NAME}-" | tail -1)

if [ -z "$TASK_SESSION" ]; then
  # Try any task session
  TASK_SESSION=$(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep -v '^main$' | tail -1)
fi

if [ -z "$TASK_SESSION" ]; then
  exit 0
fi

# Capture the last 50 lines of output
OUTPUT=$(tmux capture-pane -t "$TASK_SESSION" -p -l 50 2>/dev/null | grep -v '^$' | tail -30)

# Write result file
cat > "$RESULTS_DIR/${TASK_SESSION}.json" << EOF
{
  "session": "$TASK_SESSION",
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "cwd": "$CWD",
  "output": $(echo "$OUTPUT" | jq -Rs .)
}
EOF

exit 0
