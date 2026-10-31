"""Claude Code asyncRewake listener (SessionStart and Stop): wake an idle session with a body-free inbox hint.

Claude runs this in the background at session start and after every turn. The
newest listener records itself as the attempt's ready data, waits for the
hint file, claims it with one rename and exits 2: Claude then wakes the model
with stderr as a system notification. With `--busy` it is the UserPromptSubmit
hook: a turn running marks the attempt busy, and the listener claims no hint until
the Stop clears it. With `--tool` it is the PostToolUse hook: a hint placed during
the turn is handed to the model as added context after its next tool step. A
newer listener, a retired attempt or a gone parent ends an older one quietly
(exit 0). Never reads mail.
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from native_adapters import (CLAUDE_BUSY_TTL, HINT, HOOK_TIMEOUT, KINDS, LISTENER, RETIRED,  # noqa: E402
                             announce_ready, is_busy, write_ready, write_state)

directory, attempt = Path(sys.argv[1]), sys.argv[2]
event = json.load(sys.stdin)
if sys.argv[3:] == ["--busy"]:  # a human turn starts
    write_state(directory, True, ttl=CLAUDE_BUSY_TTL)
    sys.exit(0)
if sys.argv[3:] == ["--tool"]:  # PostToolUse: a hint placed while the turn runs joins it as context, no new turn
    claimed = directory / f".claimed.tool.{os.getpid()}"
    try:
        os.rename(directory / HINT, claimed)
    except FileNotFoundError:
        sys.exit(0)
    text = claimed.read_text().strip()
    claimed.unlink()
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}))
    sys.exit(0)
me, parent, armed = str(os.getpid()), os.getppid(), time.time()
(directory / f".{LISTENER}.{me}").write_text(me)
os.replace(directory / f".{LISTENER}.{me}", directory / LISTENER)  # supersede first: the old one stays gated
ended = event.get("hook_event_name") in ("Stop", "StopFailure")  # StopFailure: a turn ended on an API error
if ended:  # a hint no tool step of this turn took: the turn_end
    (directory / HINT).unlink(missing_ok=True)  # wake below hints again if mail is still queued
    write_state(directory, False)  # only a turn's end says idle: a start may already run its opening prompt
if not write_ready(directory, {"kind": KINDS["claude"], "attempt": attempt, "pid": int(me), "armed_at": armed,
                               "session_id": event.get("session_id")},
                   announce="turn_end" if ended else "ready"):
    sys.exit(0)
waited = False
while time.time() < armed + HOOK_TIMEOUT - 60:  # leave before Claude's own timeout ends us
    if (directory / RETIRED).exists() or os.getppid() != parent or (directory / LISTENER).read_text() != me:
        sys.exit(0)
    if is_busy(directory):
        waited = True
        time.sleep(0.2)
        continue
    if waited:  # the busy ended with no Stop superseding us (Esc, aged out): a turn ended all the same
        waited = False
        announce_ready(attempt, "turn_end")
    try:
        os.rename(directory / HINT, directory / f".claimed.{me}")
    except FileNotFoundError:
        time.sleep(0.2)
        continue
    text = (directory / f".claimed.{me}").read_text().strip()
    os.unlink(directory / f".claimed.{me}")
    write_state(directory, True, ttl=CLAUDE_BUSY_TTL)  # this wake is a turn; its Stop clears it
    print(text, file=sys.stderr)
    sys.exit(2)
