#!/bin/bash
# Run the woltspace test suite.
#
# Usage:
#   ./test/run-tests.sh              # all tests
#   ./test/run-tests.sh unit          # pure-Python tests only (no server/tmux needed)
#   ./test/run-tests.sh integration   # requires running server + tmux
#   ./test/run-tests.sh closed-loop   # full chain: telegram + server + tmux + registry
#   ./test/run-tests.sh agent         # haiku in the loop (costs API tokens)
#   ./test/run-tests.sh live          # requires TELEGRAM_BOT_TOKEN (hits real API)
#   ./test/run-tests.sh -k "pattern"  # pass any pytest args
#
# Environment:
#   TELEGRAM_BOT_TOKEN  — set to run live Telegram API tests
#   Server on :3000     — required for integration tests (auto-skipped if down)
#   TEST_VERBOSE=1      — post every test result to test group (default: on)
#   TEST_VERBOSE=0      — summary only

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WOLTSPACE_DIR="$(dirname "$SCRIPT_DIR")"

cd "$WOLTSPACE_DIR"

# Source secrets for API keys (OpenRouter, Telegram, etc.)
WOLTS_DIR="${WOLTS_DIR:-/workspace/wolts}"
if [ -f "$WOLTS_DIR/.env" ]; then
  set -a && source "$WOLTS_DIR/.env" && set +a
fi

# Ensure deps
if [ ! -d "container/bot/.venv" ]; then
  echo "installing python deps..."
  uv sync --project container/bot/pyproject.toml
fi

# ---------------------------------------------------------------------------
# Telegram test group notifications
# ---------------------------------------------------------------------------
TEST_VERBOSE="${TEST_VERBOSE:-1}"

_tg_send() {
  local msg="$1"
  if [ -z "$TEST_CHAT_ID" ] || [ -z "$TELEGRAM_BOT_TOKEN" ]; then
    return 0
  fi
  # Escape JSON special chars
  msg=$(printf '%s' "$msg" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read())[1:-1])')
  curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -H "Content-Type: application/json" \
    -d "{\"chat_id\": \"${TEST_CHAT_ID}\", \"text\": \"${msg}\"}" > /dev/null 2>&1 || true
}

_run_tests() {
  local tier="$1"; shift
  local OUTPUT RC
  OUTPUT=$("$@" 2>&1) && RC=0 || RC=$?
  echo "$OUTPUT"

  if [ "$TEST_VERBOSE" = "1" ] && [ -n "$TEST_CHAT_ID" ] && [ -n "$TELEGRAM_BOT_TOKEN" ]; then
    # Send individual test results
    local test_lines
    test_lines=$(echo "$OUTPUT" | grep -E "^test/.*PASSED|^test/.*FAILED|^test/.*SKIPPED|^test/.*ERROR")
    if [ -n "$test_lines" ]; then
      # Build a single message with all results
      local msg="🧪 ${tier} tests:"$'\n'
      while IFS= read -r line; do
        if echo "$line" | grep -q "PASSED"; then
          msg+="  ✅ $(echo "$line" | sed 's/ PASSED.*//')"$'\n'
        elif echo "$line" | grep -q "FAILED"; then
          msg+="  ❌ $(echo "$line" | sed 's/ FAILED.*//')"$'\n'
        elif echo "$line" | grep -q "SKIPPED"; then
          msg+="  ⏭️ $(echo "$line" | sed 's/ SKIPPED.*//')"$'\n'
        elif echo "$line" | grep -q "ERROR"; then
          msg+="  💥 $(echo "$line" | sed 's/ ERROR.*//')"$'\n'
        fi
      done <<< "$test_lines"
      local summary
      summary=$(echo "$OUTPUT" | grep -E "=.*(passed|failed|error)" | tail -1)
      local emoji="✅"
      [ "$RC" != "0" ] && emoji="❌"
      msg+="${emoji} ${summary}"
      _tg_send "$msg"
    fi
  else
    # Summary only
    local summary
    summary=$(echo "$OUTPUT" | grep -E "passed|failed|error" | tail -1)
    local emoji="✅"
    [ "$RC" != "0" ] && emoji="❌"
    _tg_send "${emoji} ${tier} tests: ${summary}"
  fi

  return $RC
}

TIER="${1:-all}"

# Route to test tier
case "$TIER" in
  unit)
    echo "=== Unit tests (no external deps) ==="
    _run_tests "unit" uv run --project container/bot/pyproject.toml pytest test/test_bot_core.py test/test_session_lifecycle.py::TestSessionRegistry test/test_projects.py -k "Unit" -v "${@:2}"
    ;;
  integration)
    echo "=== Integration tests (requires server + tmux) ==="
    _run_tests "integration" uv run --project container/bot/pyproject.toml pytest test/test_server_health.py test/test_session_lifecycle.py test/test_telegram_loop.py::TestNotifyRoundTrip -v "${@:2}"
    ;;
  closed-loop)
    echo "=== Closed-loop tests (full chain: telegram + server + tmux + registry) ==="
    _run_tests "closed-loop" uv run --project container/bot/pyproject.toml pytest test/test_closed_loop.py -v "${@:2}"
    ;;
  agent)
    echo "=== Agent loop tests (haiku in the loop — costs API tokens) ==="
    echo "  decision: mocked tools, ~\$0.01/test"
    echo "  scenario: multi-turn convos, ~\$0.05/test"
    echo "  live: real sessions spawned, ~\$0.50/test"
    _run_tests "agent" uv run --project container/bot/pyproject.toml pytest test/test_agent_loop.py -v "${@:2}"
    ;;
  live)
    echo "=== Live tests (requires TELEGRAM_BOT_TOKEN) ==="
    _run_tests "live" uv run --project container/bot/pyproject.toml pytest test/test_telegram_loop.py::TestTelegramAPI -v "${@:2}"
    ;;
  all)
    echo "=== Full test suite ==="
    _run_tests "all" uv run --project container/bot/pyproject.toml pytest test/ -v "${@:2}"
    ;;
  *)
    # Pass everything as pytest args
    _run_tests "custom" uv run --project container/bot/pyproject.toml pytest test/ -v "$@"
    ;;
esac
