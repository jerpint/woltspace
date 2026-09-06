#!/bin/bash
# Container boot. Runs as node (dropped from root by entrypoint.sh via gosu).
#
# Everything here is container-specific by design; the platform itself is the
# installed `woltspace` package, and this script ends by handing the process
# over to it. What stays:
#
#   - env/secret ingestion and the derived values bash needs (entrypoint_setup)
#   - harness credential/trust seeding into the per-wolt HOMEs (containers seed
#     creds; native reuses the host login and never copies anything)
#   - the workspace dirs the mount shadows at build time
#   - WOLTSPACE_ISOLATION=external + WOLTSPACE_ENTRYPOINT, the two facts only
#     this process knows
#   - the tmux window the human lands in
#
# What left: skills sync, hook normalization, session adoption and the tunnel
# all belong to the control plane now, which does them the same way natively.
set -e

WOLTSPACE_DIR="${WOLTSPACE_DIR:-/workspace/woltspace}"   # the installed bundle
WOLTS_DIR="${WOLTS_DIR:-/workspace/wolts}"               # the one host mount
# The packaged CLI, by absolute path: `woltspace` on PATH is deliberately the
# in-container HTTP client (container/bin/woltspace), a different program.
WOLTSPACE_CLI="${WOLTSPACE_CLI:-/usr/local/bin/woltspace}"

# ── Python setup (config, identity, JSON files, env resolution) ──
ENV_FILE=$(mktemp /tmp/entrypoint-env.XXXXXX)
python3 "$WOLTSPACE_DIR/container/entrypoint_setup.py" --env-file "$ENV_FILE"
source "$ENV_FILE"
rm -f "$ENV_FILE"
export WOLT_NAME WOLT_DIR WOLTS_DIR DEV_MODE PYTHONPATH PATH
export CLAUDE_CODE_DISABLE_AUTO_MEMORY=1
export WOLTSPACE_ISOLATION=external
# This script IS the platform entrypoint. Nothing else in the container should
# claim the data root, the tunnel, or the bot token — a stray `woltspace serve`
# from a worktree inherits every variable above, so the control plane refuses to
# act as owner unless it sees this.
export WOLTSPACE_ENTRYPOINT=1

# Codex seed home — codex errors if CODEX_HOME doesn't exist, and the wolts
# mount shadows any build-time mkdir. Harmless if codex is never used.
mkdir -p "${CODEX_HOME:-$WOLTS_DIR/.codex}"

# ── tmux ──
export LANG=C.UTF-8
tmux -u new-session -d -s main -c "$WOLT_DIR" 2>/dev/null || true
tmux set -g mouse on
HAS_AUTH=false
[ -f /home/node/.claude/.credentials.json ] && HAS_AUTH=true

if [ -z "$WOLT_NAME" ] || [ "$HAS_AUTH" = "false" ]; then
  # No wolt or no auth — onboard mode: bare Claude for /login
  # Viewport falls back to /onboard via server when no session is registered
  echo "onboard mode: has_auth=$HAS_AUTH wolt_name=${WOLT_NAME:-<none>}"
  tmux send-keys -t main "wclaude /login" Enter
elif [ -f /home/node/.claude/.first-run ]; then
  rm /home/node/.claude/.first-run
  # Fresh container — clear node_modules for all apps so installs run clean
  # Prevents binary/native dep corruption across container rebuilds (e.g. Node version changes)
  echo "fresh container: clearing node_modules in all apps..."
  find "$WOLTS_DIR/apps" "$WOLTS_DIR/projects" -maxdepth 2 -name "node_modules" -type d 2>/dev/null | while read -r nm; do
    echo "  removing $nm"
    rm -rf "$nm"
  done
  # Pre-load viewport with the wolt site URL — starter site is scaffolded at creation
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
#
# Telegram, the wolf scheduler and the TUI pty bridge are all supervised
# children of the control plane (ChannelConnector — see src/woltspace/channels.py).
# Starting any of them here would give the colony two of it: two pollers on one
# bot token, two schedulers firing every cron twice.
# Inspect them with:  curl -s localhost:7777/health | jq .connectors
# (NOT `woltspace status` — inside the container that name resolves to
#  container/bin/woltspace, a different CLI which knows nothing about connectors.)

# Slack has no connector yet, so it is still launched by hand. It runs on the
# installed interpreter, which owns slack-bolt via the `connectors` extra.
if [ "${ENABLE_SLACK_BOT:-}" = "true" ] && [ -n "${SLACK_BOT_TOKEN:-}" ] && [ -n "${SLACK_APP_TOKEN:-}" ]; then
  echo "starting slack bot ($SLACK_BOT_DIR, dev=$DEV_MODE)..."
  if [ "$DEV_MODE" = "true" ]; then
    (cd "$SLACK_BOT_DIR" && BOT_ADAPTER=slack PYTHONPATH="$SLACK_BOT_DIR:$PYTHONPATH" \
      woltspace-python -m watchfiles --filter python "python -m $SLACK_BOT_MODULE" bot/) &
  else
    (cd "$SLACK_BOT_DIR" && BOT_ADAPTER=slack PYTHONPATH="$SLACK_BOT_DIR:$PYTHONPATH" \
      woltspace-python -m "$SLACK_BOT_MODULE") &
  fi
  disown
fi

# Tunnel — owned by the control plane (server-side lifecycle). Report the URL
# once it lands, in the background, so the log still shows it without anyone
# standing between docker and the supervisor.
TUNNEL_STATE_FILE="$WOLTS_DIR/.space/platform/tunnel.json"
if [ "${WOLTSPACE_PUBLIC_TUNNEL:-true}" = "true" ]; then
  (
    echo "waiting for tunnel..."
    for _ in $(seq 1 45); do
      if [ -f "$TUNNEL_STATE_FILE" ]; then
        URL=$(grep -o '"url": *"[^"]*"' "$TUNNEL_STATE_FILE" 2>/dev/null | sed 's/"url": *"\(.*\)"/\1/')
        [ -n "$URL" ] && echo "tunnel ready: $URL" && exit 0
      fi
      sleep 1
    done
    echo "warning: tunnel URL not available yet (server will keep trying)"
  ) &
  disown
else
  echo "tunnel disabled — access via http://localhost:7777"
fi

# ── The control plane ──
# The same supervisor a native user runs, from the same installed package.
# `exec` so it becomes the container's foreground process: docker's SIGTERM
# reaches the owner of the connectors instead of a shell that would leave them
# orphaned.
exec "$WOLTSPACE_CLI" serve \
  --host 0.0.0.0 --port 7777 --isolation external --no-doctor
