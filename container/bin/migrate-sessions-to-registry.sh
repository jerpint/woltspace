#!/bin/bash
# Migrate old session files (sessions/*.json + session-routing/*.json) to the new registry format.
# Safe to run multiple times — skips sessions already in the registry.
# Old files are left in place.

set -euo pipefail

STATE_DIR="${WOLT_STATE_DIR:-/workspace/wolts/.state}"
REGISTRY_DIR="$STATE_DIR/registry"
mkdir -p "$REGISTRY_DIR"

python3 - "$STATE_DIR" "$REGISTRY_DIR" <<'PYEOF'
import json, sys, time
from pathlib import Path

state_dir = Path(sys.argv[1])
registry_dir = Path(sys.argv[2])

sessions_dir = state_dir / "sessions"
routing_dir = state_dir / "session-routing"

migrated = 0
skipped = 0

names = set()
if sessions_dir.exists():
    for f in sessions_dir.glob("*.json"):
        names.add(f.stem)
if routing_dir.exists():
    for f in routing_dir.glob("*.json"):
        names.add(f.stem)

for name in sorted(names):
    reg_path = registry_dir / f"{name}.json"
    if reg_path.exists():
        skipped += 1
        continue

    status_data = {}
    status_path = sessions_dir / f"{name}.json"
    if status_path.exists():
        try:
            status_data = json.loads(status_path.read_text())
        except Exception:
            pass

    routing_data = {}
    routing_path = routing_dir / f"{name}.json"
    if routing_path.exists():
        try:
            routing_data = json.loads(routing_path.read_text())
        except Exception:
            pass

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

    tmp = reg_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(entry, indent=2) + "\n")
    tmp.rename(reg_path)
    migrated += 1

print(f"migrated {migrated} sessions, skipped {skipped} (already in registry)")
PYEOF
