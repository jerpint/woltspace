#!/usr/bin/env bash
# Single runtime wrapper for agent sessions — fresh spawns AND resumes.
#
# Usage:
#   run-session.sh <session-name> [prompt]            fresh spawn
#   run-session.sh <session-name> --resume [prompt]   resume
#
# The session must already exist in the registry (start_session creates it).
# Everything else — wolt, dir, model, harness, adapter routing — is read from
# the registry. The agent command itself comes from `session-reg prepare`,
# the only place harness CLI syntax lives. Lifecycle (exit code, viewport
# reset) is closed out here for both modes.

set -euo pipefail

# Log session startup for debugging — capture errors if the session dies instantly
SESSION_LOG="/workspace/wolts/.space/logs/session-boot.log"
mkdir -p "$(dirname "$SESSION_LOG")"
exec 2> >(tee -a "$SESSION_LOG" >&2)

SESSION_NAME="$1"
shift
MODE="spawn"
if [ "${1:-}" = "--resume" ]; then
    MODE="resume"
    shift
fi
PROMPT="${1:-}"

echo "[$(date -Iseconds)] $MODE session: $SESSION_NAME" >> "$SESSION_LOG"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SESSION_REG="$SCRIPT_DIR/session-reg"

WORK_DIR=$($SESSION_REG get-field "$SESSION_NAME" dir 2>/dev/null || echo "")
WOLT=$($SESSION_REG get-field "$SESSION_NAME" wolt 2>/dev/null || echo "")

if [ -n "$WORK_DIR" ]; then
    cd "$WORK_DIR"
fi
export WOLT_SESSION="$SESSION_NAME"
if [ -n "$WOLT" ]; then
    export WOLT_NAME="$WOLT"
fi

# Build the agent command. On spawn this also stamps harness_session_id
# (used later for --resume) and a title into the registry.
CMD=$($SESSION_REG prepare "$SESSION_NAME" "$MODE" "$PROMPT")

# Run the agent — capture exit code
EXIT_CODE=0
eval "$CMD" || EXIT_CODE=$?

# Update registry with final status
$SESSION_REG finish "$SESSION_NAME" "$EXIT_CODE" > /dev/null 2>&1 || true

# Reset viewport to placeholder so it doesn't show "not found" for dead content
$SESSION_REG update "$SESSION_NAME" "viewport_url=/placeholder.html" > /dev/null 2>&1 || true
