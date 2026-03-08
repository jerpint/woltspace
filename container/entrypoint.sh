#!/bin/bash
# Entrypoint: configure identity, start server + cloudflared tunnel
#
# Auth: CLAUDE_CODE_OAUTH_TOKEN passed via env
# Wolt repo: mounted at /workspace/wolt
# Skills: baked into image at /app/skills, copied to ~/.claude/skills/

set -e

WOLT_DIR="${WOLT_DIR:-/workspace/wolt}"
WOLT_NAME="${WOLT_NAME:-wolt}"

# Copy skills so Claude auto-discovers them
# Platform defaults first, then wolt-specific overrides win
mkdir -p /home/node/.claude/skills
if [ -d /app/skills ]; then
  cp -r /app/skills/. /home/node/.claude/skills/ 2>/dev/null || true
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

# Skip first-run onboarding + trust the workspace
cat > /home/node/.claude.json << 'CJEOF'
{"hasCompletedOnboarding":true,"projects":{"/workspace/wolt":{"hasTrustDialogAccepted":true,"hasCompletedProjectOnboarding":true}}}
CJEOF

# Configure git user from env
git config --global user.name "${WOLT_NAME}"
git config --global user.email "${WOLT_NAME}@woltspace.com"

# Mark the wolt mount as safe (owned by different uid on host)
git config --global --add safe.directory "$WOLT_DIR"

# Create a default tmux session (survives browser disconnects + server restarts)
tmux new-session -d -s main -c "$WOLT_DIR" 2>/dev/null || true
# Auto-start claude in tmux
if [ -f /home/node/.claude/.first-run ]; then
  rm /home/node/.claude/.first-run
  tmux send-keys -t main "claude --dangerously-skip-permissions /create-wolt" Enter
else
  tmux send-keys -t main "claude --dangerously-skip-permissions \"hey ${WOLT_NAME}\"" Enter
fi

# ESM ignores NODE_PATH, so symlink /app/node_modules at /workspace/ level
# so ESM's directory walk from wolt dir finds container-installed packages
ln -sf /app/node_modules /workspace/node_modules

# Start the server (baked into image, reads wolt content from mount)
node --watch /app/server.js &
SERVER_PID=$!

sleep 1

# Start cloudflared tunnel
echo "opening tunnel..."
mkdir -p "$WOLT_DIR/.state"
rm -f "$WOLT_DIR/.state/tunnel-url"
TUNNEL_LOG="$WOLT_DIR/.state/tunnel.log"

cloudflared tunnel --url http://localhost:3000 > "$TUNNEL_LOG" 2>&1 &
TUNNEL_PID=$!

# Wait for tunnel URL (blocks until found, max 30s)
for i in $(seq 1 30); do
  URL=$(grep -o 'https://[^ ]*trycloudflare.com' "$TUNNEL_LOG" 2>/dev/null | head -1)
  if [ -n "$URL" ]; then
    echo "$URL" > "$WOLT_DIR/.state/tunnel-url"
    echo "tunnel ready: $URL"
    break
  fi
  sleep 1
done

# Start Telegram bot if enabled (backgrounded, not tracked by wait -n)
if [ "${ENABLE_TELEGRAM_BOT:-}" = "true" ] && [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
  # Prefer wolt's own bot code, fall back to platform default (/app/bot)
  if [ -f "$WOLT_DIR/wolt/bot/telegram_adapter.py" ]; then
    BOT_DIR="$WOLT_DIR"
    BOT_MODULE="wolt.bot.telegram_adapter"
  else
    BOT_DIR="/app"
    BOT_MODULE="bot.telegram_adapter"
  fi
  echo "starting telegram bot ($BOT_DIR)..."
  (cd "$BOT_DIR" && uv run --project bot/pyproject.toml python -m "$BOT_MODULE") &
  disown
fi

# Cleanup on exit — only kill the critical processes (server + tunnel)
cleanup() {
  kill $SERVER_PID $TUNNEL_PID 2>/dev/null
}
trap cleanup EXIT

wait -n
cleanup
