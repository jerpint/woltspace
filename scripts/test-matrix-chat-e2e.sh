#!/usr/bin/env bash
set -euo pipefail

IMAGE="matrixdotorg/synapse:v1.161.0"
CONTAINER="woltspace-matrix-e2e-$$"
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/woltspace-matrix-e2e.XXXXXX")"
DATA="$TMP_ROOT/synapse"
STATE="$TMP_ROOT/client-state"

cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  case "$TMP_ROOT" in
    "${TMPDIR:-/tmp}"/woltspace-matrix-e2e.*) rm -rf "$TMP_ROOT" ;;
    *) echo "refusing unsafe cleanup target: $TMP_ROOT" >&2 ;;
  esac
}
trap cleanup EXIT INT TERM

command -v docker >/dev/null || { echo "docker is required" >&2; exit 2; }
docker info >/dev/null 2>&1 || { echo "docker is not running" >&2; exit 2; }
mkdir -p "$DATA" "$STATE"

docker run --rm \
  -e SYNAPSE_SERVER_NAME=localhost \
  -e SYNAPSE_REPORT_STATS=no \
  -v "$DATA:/data" \
  "$IMAGE" generate >/dev/null
sed -i.bak '/^report_stats:/a\
enable_registration: true\
enable_registration_without_verification: true' "$DATA/homeserver.yaml"
rm "$DATA/homeserver.yaml.bak"

PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
docker run -d --name "$CONTAINER" \
  -p "127.0.0.1:$PORT:8008" \
  -v "$DATA:/data" \
  "$IMAGE" >/dev/null

for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$PORT/_matrix/client/versions" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done
curl -fsS "http://127.0.0.1:$PORT/_matrix/client/versions" >/dev/null

MATRIX_TEST_URL="http://127.0.0.1:$PORT" \
MATRIX_TEST_STATE="$STATE" \
uv run --extra matrix python scripts/matrix_chat_e2e.py

docker stop "$CONTAINER" >/dev/null
if grep -R -a -F \
  -e 'matrix-e2e-owner-to-wolt-4d8346' \
  -e 'matrix-e2e-wolt-to-owner-751ca9' \
  -e 'matrix-e2e-after-restart-25df80' \
  "$DATA" >/dev/null 2>&1; then
  echo "FAIL: plaintext message marker found in Synapse state" >&2
  exit 1
fi
echo "PASS: Synapse state contains no plaintext proof markers"
