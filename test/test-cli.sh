#!/bin/bash
# Smoke test for the woltspace CLI
# Runs a full cycle: init → start → stop → rebuild → start → stop
#
# Usage:
#   bash test/test-cli.sh              # test main branch (what users get)
#   bash test/test-cli.sh --local      # test local code
#   bash test/test-cli.sh --branch X   # test a specific branch
#
# Uses a temp WOLTS_DIR so your real wolts are untouched.

set -e

WOLTSPACE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
export WOLTS_DIR="/tmp/test-woltspace-cli/wolts"
unset WOLTSPACE_LOCAL
export WOLTSPACE_CONTAINER="woltspace-test"
export WOLTSPACE_PORT=7778
export WOLTSPACE_NONINTERACTIVE=true
export WOLTSPACE_WOLT_NAME="testwolt"
CONTAINER_NAME="$WOLTSPACE_CONTAINER"
PASS=0
FAIL=0
BUILD_FLAGS="$*"

_G=$'\033[0;32m'; _R=$'\033[0;31m'; _D=$'\033[0;33m'; _N=$'\033[0m'

RESULTS=""
pass() { PASS=$((PASS + 1)); RESULTS="$RESULTS\n    ${_G}✓${_N} $1"; printf "  ${_G}✓${_N} %s\n" "$1"; }
fail() { FAIL=$((FAIL + 1)); RESULTS="$RESULTS\n    ${_R}✗${_N} $1"; printf "  ${_R}✗${_N} %s\n" "$1"; }
step() { printf "\n  ${_D}▸ %s${_N}\n" "$1"; }
cleanup() {
  docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
  docker rmi woltspace 2>/dev/null || true
  docker rmi woltspace-backup:test-snapshot woltspace-backup:test-bundle 2>/dev/null || true
  rm -rf /tmp/test-woltspace-cli
}

echo ""
echo "  ════════════════════════════════════════"
echo "  🦫 WOLTSPACE CLI SMOKE TEST"
echo "  ════════════════════════════════════════"
echo "  ${_D}WOLTS_DIR=$WOLTS_DIR${_N}"
echo "  ${_D}container: $CONTAINER_NAME${_N}"
if echo "$BUILD_FLAGS" | grep -q "\-\-local"; then
  echo "  ${_D}source: local repo${_N}"
elif echo "$BUILD_FLAGS" | grep -q "\-\-branch"; then
  echo "  ${_D}source: branch $(echo "$BUILD_FLAGS" | sed 's/.*--branch //')${_N}"
else
  echo "  ${_D}source: branch main (default)${_N}"
fi
echo ""

# Clean slate
step "cleaning up previous test runs..."
cleanup

# ── init ──
echo ""
echo "  --- init ---"

step "running woltspace init (this includes docker build, may take a few minutes)..."
"$WOLTSPACE_DIR/woltspace" init $BUILD_FLAGS 2>&1

step "checking results..."
[ -d "$WOLTS_DIR" ] && pass "wolts dir created" || fail "wolts dir not created"
[ -f "$WOLTS_DIR/.env" ] && pass ".env created" || fail ".env not created"
docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$" && pass "container running" || fail "container not running"

step "waiting for server..."
sleep 3
STATUS=$(docker exec "$CONTAINER_NAME" curl -s -o /dev/null -w "%{http_code}" http://localhost:7777/ 2>/dev/null || echo "000")
[ "$STATUS" = "200" ] && pass "server responds (200)" || fail "server not responding ($STATUS)"

step "checking wolt scaffolding..."
docker exec "$CONTAINER_NAME" test -f /workspace/wolts/testwolt/wolt/wolt.json && pass "wolt scaffolded" || fail "wolt not scaffolded"

# ── stop ──
echo ""
echo "  --- stop ---"
step "stopping container..."
"$WOLTSPACE_DIR/woltspace" stop 2>&1

! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$" && pass "container stopped" || fail "container still running"

# ── start (resume stopped container) ──
echo ""
echo "  --- start (resume) ---"
step "starting container (should resume existing)..."
"$WOLTSPACE_DIR/woltspace" start 2>&1

docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$" && pass "container resumed" || fail "container not resumed"

step "waiting for server..."
sleep 3
STATUS=$(docker exec "$CONTAINER_NAME" curl -s -o /dev/null -w "%{http_code}" http://localhost:7777/ 2>/dev/null || echo "000")
[ "$STATUS" = "200" ] && pass "server responds after resume (200)" || fail "server not responding after resume ($STATUS)"

# ── stop again ──
step "stopping for rebuild test..."
"$WOLTSPACE_DIR/woltspace" stop 2>/dev/null

# ── rebuild ──
echo ""
echo "  --- rebuild ---"
step "rebuilding image + starting container..."
"$WOLTSPACE_DIR/woltspace" rebuild $BUILD_FLAGS 2>&1

docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$" && pass "container running after rebuild" || fail "container not running after rebuild"

step "waiting for server..."
sleep 3
STATUS=$(docker exec "$CONTAINER_NAME" curl -s -o /dev/null -w "%{http_code}" http://localhost:7777/ 2>/dev/null || echo "000")
[ "$STATUS" = "200" ] && pass "server responds after rebuild (200)" || fail "server not responding after rebuild ($STATUS)"

# ── reconcile (stale sessions cleaned on boot) ──
echo ""
echo "  --- reconcile ---"
step "injecting fake stale session..."
docker exec "$CONTAINER_NAME" bash -c '
  mkdir -p /workspace/wolts/.state/registry
  echo "{\"name\":\"fake-session\",\"status\":\"running\",\"wolt\":\"testwolt\"}" > /workspace/wolts/.state/registry/fake-session.json
'

step "restarting to trigger reconcile..."
"$WOLTSPACE_DIR/woltspace" stop 2>/dev/null
"$WOLTSPACE_DIR/woltspace" start 2>&1

sleep 3
FAKE_STATUS=$(docker exec "$CONTAINER_NAME" cat /workspace/wolts/.state/registry/fake-session.json 2>/dev/null | grep -o '"status": *"[^"]*"' | head -1)
[[ "$FAKE_STATUS" == *"orphaned"* ]] || [[ "$FAKE_STATUS" == *"reaped"* ]] && pass "stale session reconciled on boot" || fail "stale session not reconciled ($FAKE_STATUS)"

# ── backup ──
echo ""
echo "  --- backup ---"
step "running woltspace backup with custom tag..."
"$WOLTSPACE_DIR/woltspace" backup test-snapshot 2>&1

docker image inspect woltspace-backup:test-snapshot >/dev/null 2>&1 && pass "container snapshot created" || fail "container snapshot not created"
[ -d "/tmp/test-woltspace-cli/wolts-backup-test-snapshot" ] && pass "wolts backup created" || fail "wolts backup not created"

step "running woltspace backup --bundle..."
"$WOLTSPACE_DIR/woltspace" backup test-bundle --bundle 2>&1

[ -f "/tmp/test-woltspace-cli/woltspace-backup-test-bundle.zip" ] && pass "bundle zip created" || fail "bundle zip not created"

step "verifying bundle contents..."
BUNDLE_CONTENTS=$(unzip -l /tmp/test-woltspace-cli/woltspace-backup-test-bundle.zip 2>/dev/null || echo "")
echo "$BUNDLE_CONTENTS" | grep -q "image.tar" && pass "bundle contains image.tar" || fail "bundle missing image.tar"
echo "$BUNDLE_CONTENTS" | grep -q "restore.sh" && pass "bundle contains restore.sh" || fail "bundle missing restore.sh"
echo "$BUNDLE_CONTENTS" | grep -q "wolts/" && pass "bundle contains wolts/" || fail "bundle missing wolts/"
echo "$BUNDLE_CONTENTS" | grep -q ".tag" && pass "bundle contains .tag" || fail "bundle missing .tag"

step "cleaning up backup artifacts..."
docker rmi woltspace-backup:test-snapshot 2>/dev/null || true
docker rmi woltspace-backup:test-bundle 2>/dev/null || true
rm -rf /tmp/test-woltspace-cli/wolts-backup-test-snapshot
rm -rf /tmp/test-woltspace-cli/wolts-backup-test-bundle
rm -f /tmp/test-woltspace-cli/woltspace-backup-test-bundle.zip

# ── version stamp ──
echo ""
echo "  --- version ---"
step "checking version stamp..."
VERSION=$(docker exec "$CONTAINER_NAME" cat /workspace/woltspace/.version 2>/dev/null || echo "")
[ -n "$VERSION" ] && [ "$VERSION" != "" ] && pass "version stamped: $VERSION" || fail "no version stamp found"

# ── init with existing wolts (idempotent) ──
echo ""
echo "  --- init (idempotent) ---"
step "running init again with existing wolts..."
"$WOLTSPACE_DIR/woltspace" stop 2>/dev/null
"$WOLTSPACE_DIR/woltspace" init $BUILD_FLAGS 2>&1

docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$" && pass "container running (idempotent init)" || fail "container not running (idempotent init)"

# ── shell ──
echo ""
echo "  --- shell ---"
step "checking shell access..."
WHOAMI=$(docker exec "$CONTAINER_NAME" whoami 2>/dev/null || echo "")
[ "$WHOAMI" = "node" ] && pass "shell access works (user: node)" || fail "shell access failed ($WHOAMI)"

# ── cleanup ──
echo ""
echo "  --- cleanup ---"
step "removing test container and temp files..."
cleanup
pass "cleanup complete"

# ── results ──
echo ""
echo "  ════════════════════════════════════════"
TOTAL=$((PASS + FAIL))
if [ "$FAIL" -eq 0 ]; then
  printf "  ${_G}all %d tests passed 🦫${_N}\n" "$TOTAL"
else
  printf "  ${_R}%d/%d failed${_N}\n" "$FAIL" "$TOTAL"
fi
printf "$RESULTS\n"
echo "  ════════════════════════════════════════"
echo ""
exit "$FAIL"
