#!/bin/bash
# Entrypoint: install deps (dev mode), run Python setup, start services.
set -e

WOLTSPACE_DIR="/workspace/woltspace"
WOLTS_DIR="${WOLTS_DIR:-/workspace/wolts}"
WOLT_NAME="${WOLT_NAME:-wolt}"

# ── Dev-mode dep installs (mounts wipe node_modules/.venv) ──
[ ! -d "$WOLTSPACE_DIR/node_modules" ] && echo "dev: node deps..." && (cd "$WOLTSPACE_DIR" && npm install && npm install ws node-pty)
[ ! -d "$WOLTSPACE_DIR/container/bot/.venv" ] && echo "dev: bot deps..." && (cd "$WOLTSPACE_DIR" && uv sync --project container/bot)
[ ! -d "$WOLTSPACE_DIR/server/.venv" ] && echo "dev: server deps..." && (cd "$WOLTSPACE_DIR" && uv sync --project server)
[ -d /home/node/worktui ] && [ ! -d /home/node/worktui/node_modules ] && echo "dev: worktui deps..." && (cd /home/node/worktui && bun install)

# ── Python setup (config, identity, JSON files, env resolution) ──
ENV_FILE=$(mktemp /tmp/entrypoint-env.XXXXXX)
python3 "$WOLTSPACE_DIR/container/entrypoint_setup.py" --env-file "$ENV_FILE"
source "$ENV_FILE"
rm -f "$ENV_FILE"
export WOLT_DIR DEV_MODE WOLF_CONFIG PYTHONPATH PATH

# ── tmux ──
tmux new-session -d -s main -c "$WOLT_DIR" 2>/dev/null || true
if [ -f /home/node/.claude/.first-run ]; then
  rm /home/node/.claude/.first-run
  tmux send-keys -t main "claude --dangerously-skip-permissions /create-wolt" Enter
else
  tmux send-keys -t main "claude --dangerously-skip-permissions \"hey ${WOLT_NAME}\"" Enter
fi

# ── Services ──

# TUI pty service
TUI_PORT=3001 WOLT_DIR="$WOLT_DIR" node "$WOLTSPACE_DIR/server/tui-service.js" &
TUI_PID=$!

# Python server (FastAPI)
(cd "$WOLTSPACE_DIR" && uv run --project server uvicorn server.app:app --host 0.0.0.0 --port 3000 --reload) &
SERVER_PID=$!

sleep 2

# Tunnel
mkdir -p "$WOLTS_DIR/.state" "$WOLT_DIR/.state"
rm -f "$WOLTS_DIR/.state/tunnel-url" "$WOLT_DIR/.state/tunnel-url"
if [ "${ENABLE_TUNNEL:-true}" != "false" ]; then
  echo "opening tunnel..."
  TUNNEL_LOG="$WOLTS_DIR/.state/tunnel.log"
  cloudflared tunnel --url http://localhost:3000 > "$TUNNEL_LOG" 2>&1 &
  disown
  for i in $(seq 1 30); do
    URL=$(grep -o 'https://[^ ]*trycloudflare.com' "$TUNNEL_LOG" 2>/dev/null | head -1)
    if [ -n "$URL" ]; then
      echo "$URL" > "$WOLTS_DIR/.state/tunnel-url"
      echo "$URL" > "$WOLT_DIR/.state/tunnel-url"
      echo "tunnel ready: $URL"
      break
    fi
    sleep 1
  done
else
  echo "tunnel disabled — access via http://localhost:4444"
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
