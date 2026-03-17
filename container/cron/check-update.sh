#!/bin/bash
# Check if a woltspace update is available
# No LLM — just git ls-remote + compare + notify
#
# Designed to run as a wolf cron job (e.g., daily).
# Stores last-known version in .state/woltspace-version.
# If the remote branch has moved ahead, notifies the user.
# Reads .state/woltspace-branch to know which branch to check (default: main).

set -e

WOLTSPACE_REPO="https://github.com/jerpint/woltspace.git"
WOLT_DIR="${WOLT_DIR:-/workspace/wolts/${WOLT_NAME:-wolt}}"
STATE_DIR="$WOLT_DIR/.state"
VERSION_FILE="$STATE_DIR/woltspace-version"
BRANCH_FILE="$STATE_DIR/woltspace-branch"

# Read which branch we built from (default: main)
BUILD_BRANCH="main"
if [ -f "$BRANCH_FILE" ]; then
  BUILD_BRANCH=$(cat "$BRANCH_FILE")
fi

# Get the latest tag from the remote (semver-sorted)
REMOTE_TAGS=$(git ls-remote --tags "$WOLTSPACE_REPO" 2>/dev/null | grep -v '\^{}' | awk '{print $2}' | sed 's|refs/tags/||' | sort -V)

if [ -z "$REMOTE_TAGS" ]; then
  echo "[update-check] no tags found on remote — nothing to compare"
  exit 0
fi

LATEST_TAG=$(echo "$REMOTE_TAGS" | tail -1)

# Read stored version (could be a tag like "v0.1.0" or a commit hash from older installs)
LOCAL_VERSION=""
if [ -f "$VERSION_FILE" ]; then
  LOCAL_VERSION=$(cat "$VERSION_FILE")
fi

# First run — store current version, don't notify
if [ -z "$LOCAL_VERSION" ]; then
  mkdir -p "$STATE_DIR"
  echo "$LATEST_TAG" > "$VERSION_FILE"
  echo "[update-check] initialized version tracking ($LATEST_TAG)"
  exit 0
fi

# Compare
if [ "$LOCAL_VERSION" = "$LATEST_TAG" ]; then
  echo "[update-check] up to date ($LATEST_TAG)"
  exit 0
fi

# Update available — notify once, then stamp the new version so we don't spam
MESSAGE="a woltspace update is available ($LOCAL_VERSION -> $LATEST_TAG). to find out what changed, ask: \"can you update woltspace?\""
notify "$MESSAGE"

# Stamp the new remote tag so we only notify once per release
echo "$LATEST_TAG" > "$VERSION_FILE"

echo "[update-check] notified: update available ($LOCAL_VERSION -> $LATEST_TAG)"
