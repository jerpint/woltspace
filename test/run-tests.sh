#!/bin/bash
# Run the woltspace test suite.
#
# Usage:
#   ./test/run-tests.sh              # all tests
#   ./test/run-tests.sh unit          # pure-Python tests only (no server/tmux needed)
#   ./test/run-tests.sh integration   # requires running server + tmux
#   ./test/run-tests.sh closed-loop   # full chain: telegram + server + tmux + registry
#   ./test/run-tests.sh live          # requires TELEGRAM_BOT_TOKEN (hits real API)
#   ./test/run-tests.sh -k "pattern"  # pass any pytest args
#
# Environment:
#   TELEGRAM_BOT_TOKEN  — set to run live Telegram API tests
#   Server on :3000     — required for integration tests (auto-skipped if down)

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WOLTSPACE_DIR="$(dirname "$SCRIPT_DIR")"

cd "$WOLTSPACE_DIR"

# Ensure deps
if [ ! -d "container/bot/.venv" ]; then
  echo "installing python deps..."
  uv sync --project container/bot/pyproject.toml
fi

# Route to test tier
case "${1:-all}" in
  unit)
    echo "=== Unit tests (no external deps) ==="
    uv run --project container/bot/pyproject.toml pytest test/test_bot_core.py test/test_session_lifecycle.py::TestSessionRegistry -v "${@:2}"
    ;;
  integration)
    echo "=== Integration tests (requires server + tmux) ==="
    uv run --project container/bot/pyproject.toml pytest test/test_server_health.py test/test_session_lifecycle.py test/test_telegram_loop.py::TestNotifyRoundTrip -v "${@:2}"
    ;;
  closed-loop)
    echo "=== Closed-loop tests (full chain: telegram + server + tmux + registry) ==="
    uv run --project container/bot/pyproject.toml pytest test/test_closed_loop.py -v "${@:2}"
    ;;
  live)
    echo "=== Live tests (requires TELEGRAM_BOT_TOKEN) ==="
    uv run --project container/bot/pyproject.toml pytest test/test_telegram_loop.py::TestTelegramAPI -v "${@:2}"
    ;;
  all)
    echo "=== Full test suite ==="
    uv run --project container/bot/pyproject.toml pytest test/ -v "${@:2}"
    ;;
  *)
    # Pass everything as pytest args
    uv run --project container/bot/pyproject.toml pytest test/ -v "$@"
    ;;
esac
