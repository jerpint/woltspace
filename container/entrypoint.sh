#!/bin/bash
# Entrypoint: run Python setup, start services.
set -e

WOLTSPACE_DIR="/workspace/woltspace"  # mount point inside the container
WOLTS_DIR="${WOLTS_DIR:-/workspace/wolts}"

# ── Python setup (config, identity, JSON files, env resolution) ──
ENV_FILE=$(mktemp /tmp/entrypoint-env.XXXXXX)
python3 "$WOLTSPACE_DIR/container/entrypoint_setup.py" --env-file "$ENV_FILE"
source "$ENV_FILE"
rm -f "$ENV_FILE"
export WOLT_NAME WOLT_DIR DEV_MODE WOLF_CONFIG PYTHONPATH PATH
export CLAUDE_CODE_DISABLE_AUTO_MEMORY=1

# ── tmux ──
tmux new-session -d -s main -c "$WOLT_DIR" 2>/dev/null || true
if [ -f /home/node/.claude/.first-run ]; then
  rm /home/node/.claude/.first-run
  # Fresh container — clear node_modules for all apps so installs run clean
  # Prevents binary/native dep corruption across container rebuilds (e.g. Node version changes)
  echo "fresh container: clearing node_modules in all apps..."
  find "$WOLTS_DIR/apps" -maxdepth 2 -name "node_modules" -type d 2>/dev/null | while read -r nm; do
    echo "  removing $nm"
    rm -rf "$nm"
  done
  # Pre-load viewport with wakeup page — user sees it instantly while wolt boots
  mkdir -p "$WOLT_DIR/.state"
  python3 -c "
import json, time, sys
wolt_name, state_dir = sys.argv[1], sys.argv[2]
url = f'/wolt/{wolt_name}/site/'
data = json.dumps({'url': url, 'port': 7777, 'updated': int(time.time() * 1000)})
open(f'{state_dir}/current-url-main.json', 'w').write(data)
print(f'[viewport:main] → {url}')
" "$WOLT_NAME" "$WOLT_DIR/.state"
  tmux send-keys -t main "export WOLT_SESSION=main && wclaude --dangerously-skip-permissions /woltspace-create-wolt" Enter
else
  # TODO: replace with a /wake skill — check for recent sessions, offer resume or fresh start
  tmux send-keys -t main "wclaude --dangerously-skip-permissions \"hey ${WOLT_NAME}\"" Enter
fi

# ── Services ──

# TUI pty service
TUI_PORT=3001 WOLT_DIR="$WOLT_DIR" node "$WOLTSPACE_DIR/server/tui-service.js" &
TUI_PID=$!

# Python server (FastAPI)
(cd "$WOLTSPACE_DIR" && uv run --project server uvicorn server.app:app --host 0.0.0.0 --port 7777 --reload --reload-dir server --timeout-graceful-shutdown 1) &
SERVER_PID=$!

sleep 2

# Tunnel
mkdir -p "$WOLTS_DIR/.space/platform" "$WOLT_DIR/.state"
rm -f "$WOLTS_DIR/.space/platform/tunnel-url" "$WOLTS_DIR/.state/tunnel-url" "$WOLT_DIR/.state/tunnel-url"
if [ "${WOLTSPACE_PUBLIC_TUNNEL:-true}" = "true" ]; then
  echo "opening tunnel..."
  TUNNEL_LOG="$WOLTS_DIR/.space/platform/tunnel.log"
  cloudflared tunnel --url http://localhost:7777 > "$TUNNEL_LOG" 2>&1 &
  disown
  for i in $(seq 1 30); do
    URL=$(grep -o 'https://[^ ]*trycloudflare.com' "$TUNNEL_LOG" 2>/dev/null | head -1)
    if [ -n "$URL" ]; then
      echo "$URL" > "$WOLTS_DIR/.space/platform/tunnel-url"
      echo "$URL" > "$WOLTS_DIR/.state/tunnel-url"  # backwards compat: host CLI reads this
      echo "tunnel ready: $URL"
      break
    fi
    sleep 1
  done
else
  echo "tunnel disabled — access via http://localhost:7777"
fi

# Telegram bot
if [ "${ENABLE_TELEGRAM_BOT:-}" = "true" ] && [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
  echo "starting telegram bot ($TELEGRAM_BOT_DIR, dev=$DEV_MODE)..."
  if [ "$DEV_MODE" = "true" ]; then
    (cd "$TELEGRAM_BOT_DIR" && uv run --project bot watchfiles --filter python "python -m $TELEGRAM_BOT_MODULE" bot/) &
  else
    (cd "$TELEGRAM_BOT_DIR" && uv run --project bot python -m "$TELEGRAM_BOT_MODULE") &
  fi
  disown
fi

# Slack bot
if [ "${ENABLE_SLACK_BOT:-}" = "true" ] && [ -n "${SLACK_BOT_TOKEN:-}" ] && [ -n "${SLACK_APP_TOKEN:-}" ]; then
  echo "starting slack bot ($SLACK_BOT_DIR, dev=$DEV_MODE)..."
  if [ "$DEV_MODE" = "true" ]; then
    (cd "$SLACK_BOT_DIR" && BOT_ADAPTER=slack uv run --project bot watchfiles --filter python "python -m $SLACK_BOT_MODULE" bot/) &
  else
    (cd "$SLACK_BOT_DIR" && BOT_ADAPTER=slack uv run --project bot python -m "$SLACK_BOT_MODULE") &
  fi
  disown
fi

# Vulture reaper
echo "starting vulture reaper..."
(cd "$WOLTSPACE_DIR/container" && python3 -m creatures.vulture --once) 2>/dev/null || true
(cd "$WOLTSPACE_DIR/container" && python3 -m creatures.vulture) &
disown

# Wolf scheduler
if [ -n "$WOLF_CONFIG" ]; then
  echo "starting wolf scheduler (config: $WOLF_CONFIG)..."
  (cd "$WOLTSPACE_DIR/container" && uv run --project bot watchfiles --filter python "python -m creatures.wolf" creatures/) &
  disown
fi

# ── Cleanup ──
cleanup() { kill $TUI_PID $SERVER_PID 2>/dev/null; }
trap cleanup EXIT
wait -n
cleanup
