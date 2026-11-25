"""Neutral usage fixtures: transcripts of the wolts alpha and beta, written under tmp_path.

Every count here passes `bases` explicitly, so no test reads the real operator home.
"""

import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

LIB_DIR = str(Path(__file__).resolve().parent.parent / "container" / "lib")
if LIB_DIR not in sys.path:
    sys.path.insert(0, LIB_DIR)

ZONE = ZoneInfo("America/New_York")
STAMP = "2026-11-24T05:10:00Z"
SID = "00000000-0000-0000-0000-000000000001"


def emit(path, *events):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    return path


def request(id, stamp=STAMP, model="claude-example", fresh=5, cwd=None):
    event = {"timestamp": stamp, "message": {
        "id": id, "model": model, "content": "never export this text",
        "usage": {"input_tokens": fresh, "cache_read_input_tokens": 3,
                  "cache_creation_input_tokens": 2, "output_tokens": 7},
    }}
    if cwd is not None:
        event["cwd"] = str(cwd)
    return event


def token(delta=None, total=None, stamp=STAMP, limits=None):
    return {"type": "event_msg", "timestamp": stamp, "payload": {
        "type": "token_count", "rate_limits": limits,
        "info": {"last_token_usage": delta, "total_token_usage": total},
    }}


def wolt(tmp, name):
    """A wolt folder the lodge knows (it holds wolt/wolt.json)."""
    path = tmp / "wolts" / name
    emit(path / "wolt/wolt.json", {"name": name})
    return path


def claude(tmp, suffix="", name="alpha", sid=SID):
    """A Claude transcript in a wolt's own folder (the wolt is created if needed)."""
    wolt(tmp, name)
    return tmp / f"wolts/{name}/.claude/projects/project/{sid}{suffix}.jsonl"


def codex(tmp, name="alpha"):
    """A Codex rollout in a wolt's own folder (the wolt is created if needed)."""
    wolt(tmp, name)
    return tmp / f"wolts/{name}/.codex/sessions/2026/11/24/rollout.jsonl"


def no_host(tmp):
    """Shared engine homes that do not exist: the container layout."""
    return {"claude": tmp / "no-home/.claude/projects", "codex": tmp / "no-home/.codex/sessions"}


def host(tmp):
    """The operator's shared engine homes of a native lodge."""
    return {"claude": tmp / "home/.claude/projects", "codex": tmp / "home/.codex/sessions"}


def save(tmp, now=None, bases=None):
    import usage_collector
    return usage_collector.save(tmp / "wolts", tmp / "history.json", tmp / "claude-limits.json",
                                zone=ZONE, now=now, bases=bases or no_host(tmp))


def count(tmp, now=None, bases=None):
    import usage_collector
    return usage_collector.count(tmp / "wolts", tmp / "history.json", tmp / "claude-limits.json",
                                 zone=ZONE, now=now, bases=bases or no_host(tmp))
