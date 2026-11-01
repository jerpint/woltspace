#!/bin/bash
# Check if a woltspace update is available
# No LLM — just git ls-remote + compare + notify
#
# Designed to run as a wolf cron job (e.g., daily).
# Stores last-known version in .space/platform/woltspace-version.
# If the remote branch has moved ahead, notifies the user.
# Reads .space/platform/woltspace-branch to know which branch to check (default: main).

set -e

WOLTSPACE_REPO="https://github.com/jerpint/woltspace.git"
WOLTS_DIR="${WOLTSPACE_WOLTS_DIR:-${WOLTS_DIR:-/workspace/wolts}}"
PLATFORM_DIR="$WOLTS_DIR/.space/platform"
VERSION_FILE="$PLATFORM_DIR/woltspace-version"
BRANCH_FILE="$PLATFORM_DIR/woltspace-branch"

# Read which branch we built from (default: main)
BUILD_BRANCH="main"
if [ -f "$BRANCH_FILE" ]; then
  BUILD_BRANCH=$(cat "$BRANCH_FILE")
fi

# Get the newest final version tag from the remote (semver-sorted). Release
# candidates and the TUI's tags are never announced as an update.
REMOTE_TAGS=$(git ls-remote --tags "$WOLTSPACE_REPO" 2>/dev/null | grep -v '\^{}' | awk '{print $2}' | sed 's|refs/tags/||' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V || true)

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
  mkdir -p "$PLATFORM_DIR"
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
printf '%s' "$MESSAGE" | notify

# Stamp the new remote tag so we only notify once per release
echo "$LATEST_TAG" > "$VERSION_FILE"

echo "[update-check] notified: update available ($LOCAL_VERSION -> $LATEST_TAG)"
