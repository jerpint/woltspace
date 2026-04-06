#!/usr/bin/env bash
# Migration: v0.4.x → v0.5.0
# Renames wolts/projects/ to wolts/apps/
#
# Why: v0.5.0 renames "projects" to "apps" across the entire platform.
# The code now looks for apps in wolts/apps/ instead of wolts/projects/.
# Without this migration, no apps will be discovered.
#
# Also renames .space/projects/ to .space/apps/ (running state).
#
# This script is idempotent — safe to run multiple times.

set -euo pipefail

WOLTS_DIR="${WOLTS_DIR:-/workspace/wolts}"

echo "=== v0.5.0 migration: projects → apps ==="

# 1. Rename wolts/projects/ → wolts/apps/
if [ -d "$WOLTS_DIR/projects" ] && [ ! -d "$WOLTS_DIR/apps" ]; then
  echo "Renaming $WOLTS_DIR/projects/ → $WOLTS_DIR/apps/"
  mv "$WOLTS_DIR/projects" "$WOLTS_DIR/apps"
  echo "  done"
elif [ -d "$WOLTS_DIR/projects" ] && [ -d "$WOLTS_DIR/apps" ]; then
  echo "Both projects/ and apps/ exist — merging projects/ into apps/"
  cp -rn "$WOLTS_DIR/projects/"* "$WOLTS_DIR/apps/" 2>/dev/null || true
  echo "  merged. Old projects/ kept as backup — remove manually when ready."
elif [ -d "$WOLTS_DIR/apps" ]; then
  echo "apps/ already exists, projects/ not found — already migrated"
else
  echo "Neither projects/ nor apps/ found — nothing to migrate"
fi

# 2. Rename .space/projects/ → .space/apps/ (running state)
SPACE_DIR="$WOLTS_DIR/.space"
if [ -d "$SPACE_DIR/projects" ] && [ ! -d "$SPACE_DIR/apps" ]; then
  echo "Renaming $SPACE_DIR/projects/ → $SPACE_DIR/apps/"
  mv "$SPACE_DIR/projects" "$SPACE_DIR/apps"
  echo "  done"
elif [ -d "$SPACE_DIR/projects" ]; then
  echo "Cleaning up old .space/projects/ (state will be recreated)"
  rm -rf "$SPACE_DIR/projects"
  echo "  done"
fi

# 3. Update session JSON files: rename "project" field to "app"
echo "Updating session JSON files (project → app field)..."
UPDATED=0
for session_file in "$WOLTS_DIR"/*/".state/sessions/"*.json; do
  [ -f "$session_file" ] || continue
  if grep -q '"project"' "$session_file" 2>/dev/null; then
    # Use python for safe JSON manipulation
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
echo "Migration complete. The server will auto-reload with the new routes."
echo "New API routes: /apps, /apps/{name}/start, /apps/{name}/stop, /app/{name}/"
