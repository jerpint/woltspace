#!/bin/bash
# Check if a woltspace update is available
# No LLM — just git ls-remote + compare + notify
#
# Designed to run as a wolf cron job (e.g., daily).
# Stores last-known version in .state/woltspace-version.
# If remote main has moved ahead, notifies the user.

set -e

WOLTSPACE_REPO="https://github.com/jerpint/woltspace.git"
WOLT_DIR="${WOLT_DIR:-/workspace/wolts/${WOLT_NAME:-wolt}}"
STATE_DIR="$WOLT_DIR/.state"
VERSION_FILE="$STATE_DIR/woltspace-version"

# Get remote main HEAD (no clone needed)
REMOTE_HEAD=$(git ls-remote "$WOLTSPACE_REPO" refs/heads/main 2>/dev/null | cut -f1)

if [ -z "$REMOTE_HEAD" ]; then
  echo "[update-check] could not reach remote"
  exit 0
fi

# Read stored version
LOCAL_VERSION=""
if [ -f "$VERSION_FILE" ]; then
  LOCAL_VERSION=$(cat "$VERSION_FILE")
fi

# First run — store current version, don't notify
if [ -z "$LOCAL_VERSION" ]; then
  mkdir -p "$STATE_DIR"
  echo "$REMOTE_HEAD" > "$VERSION_FILE"
  echo "[update-check] initialized version tracking ($(echo "$REMOTE_HEAD" | cut -c1-7))"
  exit 0
fi

# Compare
if [ "$LOCAL_VERSION" = "$REMOTE_HEAD" ]; then
  echo "[update-check] up to date ($(echo "$REMOTE_HEAD" | cut -c1-7))"
  exit 0
fi

# Update available — notify
LOCAL_SHORT=$(echo "$LOCAL_VERSION" | cut -c1-7)
REMOTE_SHORT=$(echo "$REMOTE_HEAD" | cut -c1-7)

notify "a woltspace update is available ($LOCAL_SHORT -> $REMOTE_SHORT). to update, ask a beaver or raccoon: \"can you update woltspace?\"" 2>/dev/null || \
  echo "[update-check] update available ($LOCAL_SHORT -> $REMOTE_SHORT) — notify failed"

echo "[update-check] notified: update available ($LOCAL_SHORT -> $REMOTE_SHORT)"
