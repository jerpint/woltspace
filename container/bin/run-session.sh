#!/usr/bin/env bash
# Wrapper for claude sessions spawned by the bot.
# Runs claude, captures exit code, updates the session registry.
#
# Usage: run-session.sh <session-name> <work-dir> <prompt> [model]

set -euo pipefail

SESSION_NAME="$1"
WORK_DIR="$2"
PROMPT="$3"
MODEL="${4:-}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SESSION_REG="$SCRIPT_DIR/session-reg"

cd "$WORK_DIR"
export WOLT_SESSION="$SESSION_NAME"

# Generate a stable Claude session ID so we can --resume the right conversation later
CLAUDE_SESSION_ID=$(python3 -c "import uuid; print(uuid.uuid4())")
$SESSION_REG update "$SESSION_NAME" "claude_session_id=$CLAUDE_SESSION_ID" > /dev/null 2>&1 || true

# Generate a short descriptive title from the prompt (first line, ~60 chars, clean)
TITLE=$(python3 -c "
import re, sys
p = sys.argv[1]
first_line = p.split('\n')[0].split('.')[0].strip()
clean = re.sub(r'[^\w\s-]', '', first_line).strip()
print(clean[:60].lower())
" "$PROMPT" 2>/dev/null || echo "")

# Update registry with title (session was already created by core.py with routing info)
$SESSION_REG update "$SESSION_NAME" "title=$TITLE" > /dev/null 2>&1 || true

# Read adapter and wolt name from registry for session context
ADAPTER=$($SESSION_REG get-field "$SESSION_NAME" adapter 2>/dev/null || echo "lodge")
WOLT_NAME=$($SESSION_REG get-field "$SESSION_NAME" wolt 2>/dev/null || echo "wolt")

# Build the full prompt: user's task + /start-chat for session context
FULL_PROMPT="$PROMPT /start-chat $ADAPTER $WOLT_NAME"

# Run claude — capture exit code
EXIT_CODE=0
MODEL_FLAG=""
if [ -n "$MODEL" ]; then
    MODEL_FLAG="--model $MODEL"
fi
wclaude --dangerously-skip-permissions --session-id "$CLAUDE_SESSION_ID" $MODEL_FLAG "$FULL_PROMPT" || EXIT_CODE=$?

# Update registry with final status
$SESSION_REG finish "$SESSION_NAME" "$EXIT_CODE" > /dev/null 2>&1 || true

# Reset viewport to placeholder so it doesn't show "not found" for dead content
$SESSION_REG update "$SESSION_NAME" "viewport_url=/placeholder.html" > /dev/null 2>&1 || true
