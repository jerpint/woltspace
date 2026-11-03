#!/bin/bash
# Check if a woltspace update is available
# No LLM — just git ls-remote + compare + notify
#
# Designed to run as a wolf cron job (e.g., daily).
# Compares the installed release (the .version the image build wrote into
# the install root) with the newest final version tags, and tells a patch on
# the installed X.Y line apart from a newer release line, which is a
# migration. Each notice is sent once: the last-notified patch is stamped in
# .space/platform/woltspace-version, the last-notified newer line in
# .space/platform/woltspace-newer-line. Nothing is installed.

set -e

WOLTSPACE_REPO="https://github.com/jerpint/woltspace.git"
WOLTS_DIR="${WOLTSPACE_WOLTS_DIR:-${WOLTS_DIR:-/workspace/wolts}}"
PLATFORM_DIR="$WOLTS_DIR/.space/platform"
VERSION_FILE="$PLATFORM_DIR/woltspace-version"
LINE_FILE="$PLATFORM_DIR/woltspace-newer-line"

# Get the final version tags from the remote (semver-sorted). Release
# candidates and the TUI's tags are never announced as an update.
REMOTE_TAGS=$(git ls-remote --tags "$WOLTSPACE_REPO" 2>/dev/null | grep -v '\^{}' | awk '{print $2}' | sed 's|refs/tags/||' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V || true)

if [ -z "$REMOTE_TAGS" ]; then
  echo "[update-check] no tags found on remote — nothing to compare"
  exit 0
fi

LATEST_TAG=$(echo "$REMOTE_TAGS" | tail -1)
INSTALLED=$(tr -d '[:space:]' < "${WOLTSPACE_DIR:-/workspace/woltspace}/.version" 2>/dev/null || true)

# Notify once per stamp value: _announce <stamp file> <tag> <message>
_announce() {
  [ "$(cat "$1" 2>/dev/null || true)" = "$2" ] && return 0
  printf '%s' "$3" | notify
  mkdir -p "$PLATFORM_DIR"
  echo "$2" > "$1"
  echo "[update-check] notified: $3"
}

if ! printf '%s' "$INSTALLED" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+[0-9a-z.]*$'; then
  _announce "$VERSION_FILE" "$LATEST_TAG" "woltspace $LATEST_TAG is out, but the installed release is unknown, so it may not be a safe patch. ask your wolt: \"can you check the woltspace update?\""
  exit 0
fi

LINE=$(printf '%s' "$INSTALLED" | cut -d. -f1,2)
BASE=$(printf '%s' "$INSTALLED" | sed -E 's/^([0-9]+\.[0-9]+\.[0-9]+).*/\1/')
PATCH_TAG=""
NEWER_TAG=""
for tag in $REMOTE_TAGS; do
  tag_line=$(printf '%s' "${tag#v}" | cut -d. -f1,2)
  if [ "$tag_line" = "$LINE" ]; then
    PATCH_TAG="$tag"
  elif [ "$(printf '%s\n%s\n' "$LINE" "$tag_line" | sort -V | tail -n 1)" = "$tag_line" ]; then
    NEWER_TAG="$tag"
  fi
done

# A final release past the installed one on its own line (an installed rc
# counts as before its final).
if [ -n "$PATCH_TAG" ] && { [ "${PATCH_TAG#v}" != "$BASE" ] || [ "$INSTALLED" != "$BASE" ]; } \
  && [ "$(printf '%s\n%s\n' "$BASE" "${PATCH_TAG#v}" | sort -V | tail -n 1)" = "${PATCH_TAG#v}" ]; then
  _announce "$VERSION_FILE" "$PATCH_TAG" "a woltspace update is available ($INSTALLED -> ${PATCH_TAG#v}). to find out what changed, ask: \"can you update woltspace?\""
fi
if [ -n "$NEWER_TAG" ]; then
  _announce "$LINE_FILE" "$NEWER_TAG" "woltspace ${NEWER_TAG#v} is a new release line (installed $INSTALLED): it needs a migration, not a plain update. ask your wolt to read its notes first: \"can you check the woltspace ${NEWER_TAG#v} migration?\""
fi
echo "[update-check] installed $INSTALLED; newest on its line ${PATCH_TAG:-none}; newer line ${NEWER_TAG:-none}"
