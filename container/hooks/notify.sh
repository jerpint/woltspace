#!/bin/bash
# Hook: runs on Notification event from claude
# Forwards Claude's notification directly to the originating chat.
# Also checks the session inbox and delivers queued messages when Claude goes idle.

INPUT=$(cat)
SESSION_NAME="${WOLT_SESSION:-}"
if [ -z "$SESSION_NAME" ]; then
  exit 0
fi

MESSAGE=$(echo "$INPUT" | jq -r '.message // empty')

if [ -n "$MESSAGE" ] && [[ "$MESSAGE" == *"waiting for your input"* ]]; then
  # Claude is idle — check inbox for queued messages
  WOLTS_STATE_DIR="${WOLTS_STATE_DIR:-/workspace/wolts/.state}"
  INBOX_FILE="$WOLTS_STATE_DIR/inbox/${SESSION_NAME}.jsonl"
  if [ -f "$INBOX_FILE" ] && [ -s "$INBOX_FILE" ]; then
    TEXT=$(head -1 "$INBOX_FILE" | jq -r '.text // empty')
    if [ -n "$TEXT" ]; then
      # Consume the message (remove first line)
      tail -n +2 "$INBOX_FILE" > "${INBOX_FILE}.tmp" && mv "${INBOX_FILE}.tmp" "$INBOX_FILE"
      # Deliver to this session's stdin via tmux
      tmux send-keys -t "$SESSION_NAME" -l "$TEXT"
      tmux send-keys -t "$SESSION_NAME" "" Enter
    fi
  fi
elif [ -n "$MESSAGE" ]; then
  notify "$MESSAGE"
fi

exit 0
