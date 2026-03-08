#!/bin/bash
# Hook: runs on Notification event from claude
# Fires when claude wants to tell the user something (input needed, progress, etc.)

INPUT=$(cat)
MESSAGE=$(echo "$INPUT" | jq -r '.message // empty')

SESSION_NAME="${WOLT_SESSION:-}"
if [ -z "$SESSION_NAME" ] || [ -z "$MESSAGE" ]; then
  exit 0
fi

WOLTS_DIR="${WOLTS_DIR:-/workspace/wolts}"
RESULTS_DIR="$WOLTS_DIR/.state/task-results"
mkdir -p "$RESULTS_DIR"

# Write notification with timestamp suffix to avoid overwriting the done result
TS=$(date +%s)
cat > "$RESULTS_DIR/${SESSION_NAME}-notify-${TS}.json" << EOF
{
  "type": "notification",
  "session": "$SESSION_NAME",
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "message": $(echo "$MESSAGE" | jq -Rs .)
}
EOF

exit 0
