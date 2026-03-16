#!/bin/bash
# Tests for install.sh and entrypoint fixes
# Run inside the container: bash tests/test-install.sh
set -e

PASS=0
FAIL=0
_pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
_fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

echo "=== install.sh and entrypoint tests ==="
echo ""

# --- install.sh tests ---

INSTALL_SH="$(cd "$(dirname "$0")/.." && pwd)/site/install.sh"

echo "-- install.sh --"

# Test: install.sh must NOT export PATH before calling init
if grep -q 'export PATH=.*woltspace.*\$PATH' "$INSTALL_SH"; then
  _fail "install.sh exports PATH before init (breaks PATH persistence in shell rc)"
else
  _pass "install.sh does not pre-export PATH"
fi

# Test: install.sh must call woltspace by absolute path
if grep -q '"$PWD/woltspace/woltspace" init' "$INSTALL_SH"; then
  _pass "install.sh calls woltspace by absolute path"
else
  _fail "install.sh does not call woltspace by absolute path"
fi

# Test: WOLTS_DIR must default to $HOME/wolts (not $PWD)
if grep -q 'WOLTS_DIR:-\$HOME/wolts' "$INSTALL_SH"; then
  _pass "WOLTS_DIR defaults to \$HOME/wolts"
elif grep -q 'WOLTS_DIR:-\$PWD' "$INSTALL_SH"; then
  _fail "WOLTS_DIR defaults to \$PWD (should be \$HOME/wolts)"
else
  _fail "WOLTS_DIR default not found"
fi

echo ""

# --- entrypoint.sh tests ---

ENTRYPOINT_SH="$(cd "$(dirname "$0")/.." && pwd)/container/entrypoint.sh"

echo "-- entrypoint.sh --"

# Test: entrypoint must NOT create node_modules symlink in WOLTS_DIR (host-mounted)
if grep -q 'ln.*WOLTS_DIR.*node_modules' "$ENTRYPOINT_SH"; then
  _fail "entrypoint.sh symlinks node_modules into \$WOLTS_DIR (leaks to host via bind mount)"
else
  _pass "entrypoint.sh does not symlink node_modules into \$WOLTS_DIR"
fi

# Test: entrypoint must create node_modules symlink at /workspace/ level
if grep -q 'ln.*node_modules.*/workspace/node_modules' "$ENTRYPOINT_SH" || \
   grep -q 'ln.*/workspace/node_modules' "$ENTRYPOINT_SH"; then
  _pass "/workspace/node_modules symlink is created"
else
  _fail "/workspace/node_modules symlink not created"
fi

echo ""

# --- Runtime tests (only if running inside container) ---

if [ -d "/workspace/woltspace" ]; then
  echo "-- runtime (inside container) --"

  # Test: no node_modules symlink in /workspace/wolts/
  if [ -L "/workspace/wolts/node_modules" ]; then
    _fail "/workspace/wolts/node_modules symlink exists (should not)"
  else
    _pass "/workspace/wolts/node_modules does not exist"
  fi

  # Test: /workspace/node_modules resolves correctly
  if [ -L "/workspace/node_modules" ] && [ -d "/workspace/node_modules" ]; then
    _pass "/workspace/node_modules symlink resolves"
  else
    _fail "/workspace/node_modules missing or broken"
  fi

  # Test: node can resolve packages from a wolt dir via /workspace/node_modules
  RESOLVE_TEST=$(cd /workspace/wolts 2>/dev/null && node -e "
    try { require('ws'); console.log('ok'); }
    catch(e) { console.log('fail: ' + e.message); }
  " 2>&1)
  if [ "$RESOLVE_TEST" = "ok" ]; then
    _pass "node resolves 'ws' from /workspace/wolts/ via /workspace/node_modules"
  else
    _fail "node cannot resolve 'ws' from /workspace/wolts/: $RESOLVE_TEST"
  fi
fi

echo ""
echo "=== $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
