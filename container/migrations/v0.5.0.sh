#!/usr/bin/env bash
# Migration: v0.4.x → v0.5.0
# Moves wolts/projects/ contents into wolts/apps/
#
# OPTIONAL — the platform discovers apps from both wolts/apps/ and wolts/projects/.
# Run this to consolidate into the new directory. Everything works without it.
#
# This script is idempotent — safe to run multiple times.
#
# Usage: bash migrations/v0.5.0.sh [WOLTS_DIR]
#   Default: ~/.woltspace/wolts (host path — pass /workspace/wolts if running inside container)

set -euo pipefail

WOLTS_DIR="${1:-$HOME/.woltspace/wolts}"

if [ ! -d "$WOLTS_DIR" ]; then
  echo "error: wolts directory not found: $WOLTS_DIR"
  echo "usage: $0 [WOLTS_DIR]  (default: ~/.woltspace/wolts)"
  exit 1
fi

echo "=== v0.5.0 migration: projects → apps ==="
echo "wolts dir: $WOLTS_DIR"

# 1. Move wolts/projects/ contents into wolts/apps/
if [ -d "$WOLTS_DIR/projects" ] && [ ! -d "$WOLTS_DIR/apps" ]; then
  echo "Moving $WOLTS_DIR/projects/ → $WOLTS_DIR/apps/"
  mv "$WOLTS_DIR/projects" "$WOLTS_DIR/apps"
  echo "  done"
elif [ -d "$WOLTS_DIR/projects" ] && [ -d "$WOLTS_DIR/apps" ]; then
  echo "Both projects/ and apps/ exist — merging projects/ into apps/"
  cp -rn "$WOLTS_DIR/projects/"* "$WOLTS_DIR/apps/" 2>/dev/null || true
  echo "  merged. Old projects/ kept as backup — remove manually when ready."
elif [ -d "$WOLTS_DIR/apps" ]; then
  echo "apps/ already exists, projects/ not found — already migrated"
else
  echo "Neither projects/ nor apps/ found — creating apps/"
  mkdir -p "$WOLTS_DIR/apps"
fi

# 2. Clean up .space/projects/ if it exists (running state, will be recreated)
SPACE_DIR="$WOLTS_DIR/.space"
if [ -d "$SPACE_DIR/projects" ]; then
  echo "Cleaning up old .space/projects/ (running state will be recreated as .space/apps/)"
  rm -rf "$SPACE_DIR/projects"
  echo "  done"
fi

# 3. Update session JSON files: rename "project" field to "app"
echo "Updating session JSON files (project → app field)..."
UPDATED=0
for session_file in "$WOLTS_DIR"/*/".state/sessions/"*.json; do
  [ -f "$session_file" ] || continue
  if grep -q '"project"' "$session_file" 2>/dev/null; then
    python3 -c "
import json, sys
f = sys.argv[1]
data = json.loads(open(f).read())
if 'project' in data:
    data['app'] = data.pop('project')
    open(f, 'w').write(json.dumps(data, indent=2) + '\n')
    print(f'  updated {f}')
" "$session_file" && UPDATED=$((UPDATED + 1))
  fi
done
echo "  $UPDATED session file(s) updated"

echo ""
echo "Migration complete."
echo "Note: wolts/projects/ is still supported — apps there will be discovered automatically."
echo "New API routes: /apps, /apps/{name}/start, /apps/{name}/stop, /app/{name}/"
