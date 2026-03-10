#!/bin/bash
# Hook: runs on Stop event for claude sessions
# Sends Claude's own final message directly back to the originating chat.

INPUT=$(cat)
SESSION_NAME="${WOLT_SESSION:-}"
if [ -z "$SESSION_NAME" ]; then
  exit 0  # not a bot-spawned session
fi

exit 0
