---
name: update-2026-03-13
description: "Migrate session metadata to the new centralized registry (.state/registry/). Run once after pulling this update."
user_invocable: true
---

# Update 2026-03-13: Session Registry Migration

This update replaces the scattered session metadata system with a centralized registry.

**What changed:**
- Session status (`sessions/*.json`) and routing (`session-routing/*.json`) are now unified in `.state/registry/*.json`
- One file per session with all metadata: status, routing, creature, model, timestamps, viewport
- New CLI tool: `session-reg` (create/update/get/list/reconcile)
- `core.py`, `run-session.sh`, `notify`, and `server.js` all read/write the new format

**What to do:**

Run this migration to convert existing session data to the new format. Old files are left in place (they won't be read anymore, but kept for safety).

## Migration Steps

1. First, read and run the migration script below
2. Verify it worked by running `session-reg list`

## Migration Script

```bash
#!/bin/bash
# Migrate old session files to the new registry format
STATE_DIR="${WOLT_STATE_DIR:-/workspace/wolts/.state}"
REGISTRY_DIR="$STATE_DIR/registry"
mkdir -p "$REGISTRY_DIR"

SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
LIB_DIR="/workspace/woltspace/container/lib"

python3 - "$STATE_DIR" "$REGISTRY_DIR" <<'PYEOF'
import json, sys, os, time
from pathlib import Path

state_dir = Path(sys.argv[1])
registry_dir = Path(sys.argv[2])

sessions_dir = state_dir / "sessions"
routing_dir = state_dir / "session-routing"

migrated = 0
skipped = 0

# Gather all session names from both sources
names = set()
if sessions_dir.exists():
    for f in sessions_dir.glob("*.json"):
        names.add(f.stem)
if routing_dir.exists():
    for f in routing_dir.glob("*.json"):
        names.add(f.stem)

for name in sorted(names):
    # Skip if already in registry
    reg_path = registry_dir / f"{name}.json"
    if reg_path.exists():
        skipped += 1
        continue

    # Read old status file
    status_data = {}
    status_path = sessions_dir / f"{name}.json"
    if status_path.exists():
        try:
            status_data = json.loads(status_path.read_text())
        except Exception:
            pass

    # Read old routing file
    routing_data = {}
    routing_path = routing_dir / f"{name}.json"
    if routing_path.exists():
        try:
            routing_data = json.loads(routing_path.read_text())
        except Exception:
            pass

    # Build unified registry entry
    entry = {
        "name": name,
        "wolt": routing_data.get("wolt", name.split("-")[0] if "-" in name else ""),
        "creature": routing_data.get("creature", ""),
        "model": "",
        "status": status_data.get("status", "unknown"),
        "created_at": status_data.get("started", routing_data.get("ts", int(time.time()))),
        "finished_at": status_data.get("finished", None),
        "exit_code": status_data.get("exit_code", None),
        "dir": status_data.get("dir", ""),
        "title": status_data.get("title", ""),
        "prompt": status_data.get("prompt", ""),
        "last_activity": status_data.get("finished", status_data.get("started", int(time.time()))),
        "adapter": routing_data.get("adapter", ""),
        "chat_id": str(routing_data.get("chat_id", "")),
        "user_id": str(routing_data.get("user_id", "")),
        "thread_ts": str(routing_data.get("thread_ts", "")),
        "viewport_url": "",
        "session_url": "",
    }

    # Write to registry
    tmp = reg_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(entry, indent=2) + "\n")
    tmp.rename(reg_path)
    migrated += 1

print(f"migrated {migrated} sessions, skipped {skipped} (already in registry)")
PYEOF
```

Run this script in bash, then verify with:
```bash
session-reg list | python3 -m json.tool | head -40
session-reg reconcile
```

That's it. New sessions will automatically use the registry. Old `sessions/` and `session-routing/` directories can be deleted after confirming everything works.
