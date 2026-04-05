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

# Generate a UUID for Claude's session ID (--session-id requires a valid UUID).
# Store it in the registry so resume_session() can use --resume with the UUID.
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

# Build adapter-specific context so the wolt knows how to communicate from the start
ADAPTER_CONTEXT=""
if [ "$ADAPTER" = "slack" ]; then
    CHANNEL=$($SESSION_REG get-field "$SESSION_NAME" chat_id 2>/dev/null || echo "")
    THREAD_TS=$($SESSION_REG get-field "$SESSION_NAME" thread_ts 2>/dev/null || echo "")
    if [ -n "$CHANNEL" ] && [ -n "$THREAD_TS" ]; then
        ADAPTER_CONTEXT="
This session was started from Slack. Send messages with: notify --slack $CHANNEL $THREAD_TS \"your message\"
Session link: $($SESSION_REG get-field "$SESSION_NAME" session_url 2>/dev/null || echo "")"
    fi
elif [ "$ADAPTER" = "telegram" ]; then
    CHAT_ID=$($SESSION_REG get-field "$SESSION_NAME" chat_id 2>/dev/null || echo "")
    if [ -n "$CHAT_ID" ]; then
        ADAPTER_CONTEXT="
This session was started from Telegram. Send messages with: notify --telegram $CHAT_ID \"your message\"
Session link: $($SESSION_REG get-field "$SESSION_NAME" session_url 2>/dev/null || echo "")"
    fi
fi

# Build the full prompt: user's task + adapter context + /woltspace-start-chat for session context
FULL_PROMPT="$PROMPT${ADAPTER_CONTEXT} /woltspace-start-chat $ADAPTER $WOLT_NAME"

# Run claude — capture exit code
EXIT_CODE=0
MODEL_FLAG=""
if [ -n "$MODEL" ]; then
    MODEL_FLAG="--model $MODEL"
fi
wclaude --dangerously-skip-permissions --session-id "$CLAUDE_SESSION_ID" --name "$SESSION_NAME" $MODEL_FLAG "$FULL_PROMPT" || EXIT_CODE=$?

# Update registry with final status
$SESSION_REG finish "$SESSION_NAME" "$EXIT_CODE" > /dev/null 2>&1 || true

# Reset viewport to placeholder so it doesn't show "not found" for dead content
$SESSION_REG update "$SESSION_NAME" "viewport_url=/placeholder.html" > /dev/null 2>&1 || true
