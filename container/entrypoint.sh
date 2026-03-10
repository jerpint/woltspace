#!/bin/bash
# Entrypoint: configure identity, start server + cloudflared tunnel
#
# Auth: CLAUDE_CODE_OAUTH_TOKEN passed via env
# Wolts dir: mounted at /workspace/wolts (all wolts)
# Active wolt: WOLT_NAME env var selects which wolt to boot
# Skills: at /workspace/woltspace/container/skills, copied to ~/.claude/skills/

set -e

WOLTSPACE_DIR="/workspace/woltspace"
WOLTS_DIR="${WOLTS_DIR:-/workspace/wolts}"
WOLT_NAME="${WOLT_NAME:-wolt}"

# Dev mode: deps wiped by mount — reinstall
if [ ! -d "$WOLTSPACE_DIR/node_modules" ]; then
  echo "dev mode: installing node deps..."
  (cd "$WOLTSPACE_DIR" && npm install && npm install ws node-pty)
fi
if [ ! -d "$WOLTSPACE_DIR/container/bot/.venv" ]; then
  echo "dev mode: installing python deps..."
  (cd "$WOLTSPACE_DIR" && uv sync --project container/bot/pyproject.toml)
fi

# Resolve active wolt directory
# If WOLTS_DIR is set (multi-wolt mode), derive WOLT_DIR from it
if [ -d "$WOLTS_DIR/$WOLT_NAME" ]; then
  WOLT_DIR="$WOLTS_DIR/$WOLT_NAME"
else
  WOLT_DIR="${WOLT_DIR:-/workspace/wolt}"
fi
export WOLT_DIR

# Copy skills so Claude auto-discovers them
# Platform defaults first, then wolt-specific overrides win
mkdir -p /home/node/.claude/skills
if [ -d "$WOLTSPACE_DIR/container/skills" ]; then
  cp -r "$WOLTSPACE_DIR/container/skills/." /home/node/.claude/skills/ 2>/dev/null || true
fi
if [ -d "$WOLT_DIR/.claude/skills" ]; then
  cp -r "$WOLT_DIR/.claude/skills/." /home/node/.claude/skills/ 2>/dev/null || true
fi

# Set up SSH for deploy key (git push)
if [ -f /home/node/.ssh/deploy-key ]; then
  mkdir -p /home/node/.ssh
  ssh-keyscan -t ed25519 github.com >> /home/node/.ssh/known_hosts 2>/dev/null
  cat > /home/node/.ssh/config <<'SSHEOF'
Host github.com
  IdentityFile /home/node/.ssh/deploy-key
  IdentitiesOnly yes
SSHEOF
  chmod 600 /home/node/.ssh/config
fi

# Add shortcut for interactive use inside the container
cat >> /home/node/.bashrc <<NWEOF
wolt() {
  cd $WOLT_DIR
  if [[ "\$1" == "--resume" ]]; then
    claude --dangerously-skip-permissions --resume
  else
    claude --dangerously-skip-permissions "hey ${WOLT_NAME}" "\$@"
  fi
}
NWEOF

# Write OAuth token to credentials file so claude CLI picks it up
if [ -n "$CLAUDE_CODE_OAUTH_TOKEN" ]; then
  mkdir -p /home/node/.claude
  printf '{"claudeAiOauth":{"accessToken":"%s","expiresAt":9999999999999}}' "$CLAUDE_CODE_OAUTH_TOKEN" > /home/node/.claude/.credentials.json
  chmod 600 /home/node/.claude/.credentials.json
fi

# Skip first-run onboarding + trust all wolt dirs + accept bypass permissions
TRUST_PROJECTS="{}"
for d in "$WOLTS_DIR"/*/; do
  [ -d "$d" ] && TRUST_PROJECTS=$(echo "$TRUST_PROJECTS" | python3 -c "
import json, sys
d = json.load(sys.stdin)
d['/workspace/wolts/$(basename "$d")'] = {'hasTrustDialogAccepted': True, 'hasCompletedProjectOnboarding': True}
json.dump(d, sys.stdout)
")
done
python3 -c "
import json
projects = json.loads('$TRUST_PROJECTS')
json.dump({'hasCompletedOnboarding': True, 'bypassPermissionsAccepted': True, 'projects': projects}, open('/home/node/.claude.json', 'w'), indent=2)
"

# Install session-done hook (notifies bot when claude sessions end)
if [ -f "$WOLTSPACE_DIR/container/hooks/session-done.sh" ]; then
  mkdir -p /home/node/.claude
  # Merge hook into settings if not already present
  SETTINGS_FILE="/home/node/.claude/settings.json"
  cat > "$SETTINGS_FILE" << HOOKEOF
{
  "skipDangerousModePermissionPrompt": true,
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "$WOLTSPACE_DIR/container/hooks/session-done.sh"
          }
        ]
      }
    ],
    "Notification": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "$WOLTSPACE_DIR/container/hooks/notify.sh"
          }
        ]
      }
    ]
  }
}
HOOKEOF
fi

# Configure git user from env
git config --global user.name "${WOLT_NAME}"
git config --global user.email "${WOLT_NAME}@woltspace.com"

# Mark mounts as safe (owned by different uid on host)
git config --global --add safe.directory "$WOLT_DIR"
git config --global --add safe.directory "$WOLTSPACE_DIR"

# Create a default tmux session (survives browser disconnects + server restarts)
tmux new-session -d -s main -c "$WOLT_DIR" 2>/dev/null || true
# Auto-start claude in tmux
if [ -f /home/node/.claude/.first-run ]; then
  rm /home/node/.claude/.first-run
  tmux send-keys -t main "claude --dangerously-skip-permissions /create-wolt" Enter
else
  tmux send-keys -t main "claude --dangerously-skip-permissions \"hey ${WOLT_NAME}\"" Enter
fi

# ESM ignores NODE_PATH, so symlink node_modules at /workspace/ level
# so ESM's directory walk from wolt dir finds container-installed packages
ln -sf "$WOLTSPACE_DIR/node_modules" /workspace/node_modules
# Also at wolts level for multi-wolt mount
[ -d "$WOLTS_DIR" ] && ln -sf "$WOLTSPACE_DIR/node_modules" "$WOLTS_DIR/node_modules" 2>/dev/null || true

# Start the server (baked into image, reads wolt content from mount)
node --watch "$WOLTSPACE_DIR/server.js" &
SERVER_PID=$!

sleep 1

# Start cloudflared tunnel (disable with ENABLE_TUNNEL=false)
mkdir -p "$WOLTS_DIR/.state" "$WOLT_DIR/.state"
rm -f "$WOLTS_DIR/.state/tunnel-url" "$WOLT_DIR/.state/tunnel-url"
TUNNEL_PID=""

if [ "${ENABLE_TUNNEL:-true}" != "false" ]; then
  echo "opening tunnel..."
  TUNNEL_LOG="$WOLTS_DIR/.state/tunnel.log"

  cloudflared tunnel --url http://localhost:3000 > "$TUNNEL_LOG" 2>&1 &
  TUNNEL_PID=$!

  # Wait for tunnel URL (blocks until found, max 30s)
  for i in $(seq 1 30); do
    URL=$(grep -o 'https://[^ ]*trycloudflare.com' "$TUNNEL_LOG" 2>/dev/null | head -1)
    if [ -n "$URL" ]; then
      echo "$URL" > "$WOLTS_DIR/.state/tunnel-url"
      # Also write to active wolt for backward compat (CLI reads it)
      echo "$URL" > "$WOLT_DIR/.state/tunnel-url"
      echo "tunnel ready: $URL"
      break
    fi
    sleep 1
  done
else
  echo "tunnel disabled — access via http://localhost:4444"
fi

# Dev mode: woltspace repo is mounted (has .git) → enable watchfiles auto-restart
# In prod the image is baked — no .git, no auto-restart (wolts could modify bot code)
if [ -d "$WOLTSPACE_DIR/.git" ]; then
  DEV_MODE=true
else
  DEV_MODE=false
fi

# Start Telegram bot if enabled (backgrounded, not tracked by wait -n)
# In dev mode, watchfiles auto-restarts on .py changes. Disabled in prod (wolts could modify bot code).
if [ "${ENABLE_TELEGRAM_BOT:-}" = "true" ] && [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
  if [ -f "$WOLT_DIR/wolt/bot/telegram_adapter.py" ]; then
    BOT_DIR="$WOLT_DIR"
    BOT_MODULE="wolt.bot.telegram_adapter"
  else
    BOT_DIR="$WOLTSPACE_DIR/container"
    BOT_MODULE="bot.telegram_adapter"
  fi
  echo "starting telegram bot ($BOT_DIR, dev=$DEV_MODE)..."
  if [ "$DEV_MODE" = "true" ]; then
    (cd "$BOT_DIR" && uv run --project bot/pyproject.toml watchfiles --filter python "python -m $BOT_MODULE" bot/) &
  else
    (cd "$BOT_DIR" && uv run --project bot/pyproject.toml python -m "$BOT_MODULE") &
  fi
  disown
fi

# Start Slack bot if enabled (backgrounded, not tracked by wait -n)
if [ "${ENABLE_SLACK_BOT:-}" = "true" ] && [ -n "${SLACK_BOT_TOKEN:-}" ] && [ -n "${SLACK_APP_TOKEN:-}" ]; then
  if [ -f "$WOLT_DIR/wolt/bot/slack_adapter.py" ]; then
    SLACK_BOT_DIR="$WOLT_DIR"
    SLACK_BOT_MODULE="wolt.bot.slack_adapter"
  else
    SLACK_BOT_DIR="$WOLTSPACE_DIR/container"
    SLACK_BOT_MODULE="bot.slack_adapter"
  fi
  echo "starting slack bot ($SLACK_BOT_DIR, dev=$DEV_MODE)..."
  if [ "$DEV_MODE" = "true" ]; then
    (cd "$SLACK_BOT_DIR" && BOT_ADAPTER=slack uv run --project bot/pyproject.toml watchfiles --filter python "python -m $SLACK_BOT_MODULE" bot/) &
  else
    (cd "$SLACK_BOT_DIR" && BOT_ADAPTER=slack uv run --project bot/pyproject.toml python -m "$SLACK_BOT_MODULE") &
  fi
  disown
fi

# Cleanup on exit — only kill the critical processes (server + tunnel)
cleanup() {
  kill $SERVER_PID ${TUNNEL_PID:-} 2>/dev/null
}
trap cleanup EXIT

wait -n
cleanup
