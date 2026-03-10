#!/bin/bash
# Hook: runs on Notification event from claude
# Forwards Claude's notification directly to the originating chat.

INPUT=$(cat)
SESSION_NAME="${WOLT_SESSION:-}"
if [ -z "$SESSION_NAME" ]; then
  exit 0
fi

MESSAGE=$(echo "$INPUT" | jq -r '.message // empty')
# Filter out Claude's built-in idle/waiting notifications
if [ -n "$MESSAGE" ] && [[ "$MESSAGE" != *"waiting for your input"* ]]; then
  notify "$MESSAGE"
fi

exit 0
