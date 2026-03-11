#!/usr/bin/env bash
# Wrapper for claude sessions spawned by the bot.
# Runs claude, captures exit code, writes structured status.
#
# Usage: run-session.sh <session-name> <work-dir> <prompt>

set -euo pipefail

SESSION_NAME="$1"
WORK_DIR="$2"
PROMPT="$3"

# Where we write structured status
STATE_DIR="${WOLT_STATE_DIR:-/workspace/wolts/.state}"
STATUS_DIR="$STATE_DIR/sessions"
mkdir -p "$STATUS_DIR"
STATUS_FILE="$STATUS_DIR/${SESSION_NAME}.json"

cd "$WORK_DIR"
export WOLT_SESSION="$SESSION_NAME"

# Generate a short descriptive title from the prompt (first line, ~60 chars, clean)
TITLE=$(python3 -c "
import re, sys
p = sys.argv[1]
first_line = p.split('\n')[0].split('.')[0].strip()
clean = re.sub(r'[^\w\s-]', '', first_line).strip()
print(clean[:60].lower())
" "$PROMPT" 2>/dev/null || echo "")

# Write initial status (Python for safe JSON serialization — prompt may contain quotes/newlines)
python3 -c "
import json, sys, time
data = {
    'session': sys.argv[1], 'status': 'running',
    'started': int(time.time()), 'dir': sys.argv[2],
    'title': sys.argv[3], 'prompt': sys.argv[4][:500],
}
print(json.dumps(data))
" "$SESSION_NAME" "$WORK_DIR" "$TITLE" "$PROMPT" > "$STATUS_FILE"

# Read session routing to tell Claude which platform to notify
ROUTING_FILE="${WOLT_STATE_DIR:-/workspace/wolts/.state}/session-routing/${SESSION_NAME}.json"
NOTIFY_CONTEXT=""
if [ -f "$ROUTING_FILE" ]; then
  ADAPTER=$(python3 -c "import json,sys; d=json.load(open('$ROUTING_FILE')); print(d.get('adapter','telegram'))" 2>/dev/null || echo "telegram")
  case "$ADAPTER" in
    slack)    NOTIFY_PLATFORM="Slack" ;;
    telegram) NOTIFY_PLATFORM="Telegram" ;;
    *)        NOTIFY_PLATFORM="$ADAPTER" ;;
  esac
  NOTIFY_CONTEXT="
---
## How this session works

You were dispatched from $NOTIFY_PLATFORM by a developer. They're reading your updates on their phone or chat — they are NOT watching this terminal. Your \`notify\` messages are the primary way they see your work.

Think of it like messaging a dev colleague on $NOTIFY_PLATFORM: you do the work here, then message them the results directly. They may never open this session.

Use \`notify \"your message\"\` to send messages.

**When you start**: one-liner ack. \"on it — reviewing the loop\" or \"got it, digging in.\"

**When you're done**: Send a complete summary via notify — all key findings, decisions, and results. The reader should get full context without opening the session. But write it for chat, not a terminal — short paragraphs, no code blocks or formatted logs. Think \"messaging a colleague your conclusions\" not \"pasting terminal output.\" Be thorough but digestible. NEVER say \"see session\" or \"report in session.\"

Also: always print your full detailed output (code, logs, raw analysis) to this terminal too — it stays in the session for anyone who opens the live view later.

2-3 notifies max across the whole session."
fi

FULL_PROMPT="$PROMPT$NOTIFY_CONTEXT"

# Run claude — capture exit code
EXIT_CODE=0
claude --dangerously-skip-permissions "$FULL_PROMPT" || EXIT_CODE=$?

# Write final status
if [ "$EXIT_CODE" -eq 0 ]; then
    FINAL_STATUS="completed"
else
    FINAL_STATUS="failed"
fi

# Preserve title/prompt from initial write
python3 -c "
import json, sys, time
try:
    prev = json.loads(open(sys.argv[1]).read())
except Exception:
    prev = {}
data = {
    'session': sys.argv[2], 'status': sys.argv[3], 'exit_code': int(sys.argv[4]),
    'started': prev.get('started', int(time.time())), 'finished': int(time.time()),
    'dir': sys.argv[5], 'title': prev.get('title', ''), 'prompt': prev.get('prompt', ''),
}
print(json.dumps(data))
" "$STATUS_FILE" "$SESSION_NAME" "$FINAL_STATUS" "$EXIT_CODE" "$WORK_DIR" > "$STATUS_FILE.tmp" && mv "$STATUS_FILE.tmp" "$STATUS_FILE"
