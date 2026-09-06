"""Strip the retired woltspace claude hooks from existing wolts.

The platform once wrote a Stop hook (session-done.sh) and a Notification hook
(notify.sh) into every wolt's .claude/settings.json with an absolute path
snapshotted at creation. Both are gone now — the Stop hook was a no-op and the
Notification hook never fired natively — but the baked paths linger in wolts
made before the change and spam "No such file" on every event.

This runs at start (beside the skill sync) and removes those specific hook
entries — but only when the command points *into a platform hooks directory*.
The basename alone is not enough: "notify.sh" is an obvious name for a hook a
wolt writes itself, and a sweep matching on the name would delete the wolt's
own work. So the parent directory has to be one the platform ever shipped:
`<anything>/container/hooks` (the current layout, in every form it was baked —
"/workspace/woltspace/...", a mac checkout's absolute path, or an unexpanded
"$WOLTSPACE_DIR/...") or the pre-container "/app/hooks". A hook named notify.sh
living in a wolt's own directory survives.

Every other key, and any hook a wolt added itself, is left untouched. A
settings file with no woltspace hooks is left byte-identical.
"""

import json
import os
from pathlib import Path, PurePosixPath

WOLTSPACE_HOOK_BASENAMES = frozenset({"session-done.sh", "notify.sh"})

# Directory suffixes the platform's own hooks have always lived under. Matched
# as a trailing path segment, so every prefix the writers ever produced —
# absolute install path or literal $WOLTSPACE_DIR — resolves the same way.
PLATFORM_HOOK_DIRS = ("container/hooks", "app/hooks")


def _points_into_platform_hooks(command: str) -> bool:
    path = PurePosixPath(command.strip().strip("'\""))
    if path.name not in WOLTSPACE_HOOK_BASENAMES:
        return False
    parent = path.parent.as_posix()
    return any(parent == d or parent.endswith("/" + d) for d in PLATFORM_HOOK_DIRS)


def _is_woltspace_hook(entry) -> bool:
    if not isinstance(entry, dict):
        return False
    command = entry.get("command")
    if not isinstance(command, str):
        return False
    return _points_into_platform_hooks(command)


def strip_woltspace_hooks(settings: dict) -> bool:
    """Remove woltspace hook entries from a settings dict in place.

    Returns True when something was actually removed, False when the settings
    held no woltspace hooks (so the caller can leave the file untouched).
    """
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False

    changed = False
    for event in list(hooks.keys()):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        new_groups = []
        for group in groups:
            if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                kept = [h for h in group["hooks"] if not _is_woltspace_hook(h)]
                if len(kept) != len(group["hooks"]):
                    changed = True
                    if not kept:
                        continue  # drop a group emptied of its only hooks
                    group = {**group, "hooks": kept}
            new_groups.append(group)
        if not new_groups:
            del hooks[event]
        else:
            hooks[event] = new_groups

    if changed and not hooks:
        del settings["hooks"]
    return changed


def normalize_settings_file(settings_path: Path) -> None:
    """Strip woltspace hooks from one settings.json, atomically, non-fatally."""
    if not settings_path.is_file():
        return
    try:
        settings = json.loads(settings_path.read_text())
    except (ValueError, OSError):
        return  # malformed or unreadable — never our place to rewrite it
    if not isinstance(settings, dict):
        return
    if not strip_woltspace_hooks(settings):
        return  # no woltspace hooks — leave the file byte-identical

    tmp = settings_path.with_name(settings_path.name + ".tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n")
    os.replace(tmp, settings_path)


def normalize_all_wolt_hooks(wolts_dir: Path) -> None:
    """Strip retired woltspace hooks from every wolt's .claude/settings.json."""
    wolts_dir = Path(wolts_dir)
    if not wolts_dir.is_dir():
        return
    for wolt in sorted(wolts_dir.iterdir()):
        if not wolt.is_dir() or wolt.name.startswith("."):
            continue
        normalize_settings_file(wolt / ".claude" / "settings.json")
