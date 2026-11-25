"""Read Claude request usage, including subagents under their parent session."""

ROOT = ".claude/projects"


def session_of(relative_path):
    """<project>/<session>.jsonl, or <project>/<session>/subagents/... folded into its parent."""
    parts = relative_path.parts
    return relative_path.stem if len(parts) == 2 else parts[1]


def identity(relative_path, events):
    """(session id, working folder recorded at the start) for native attribution."""
    cwd = next((event["cwd"] for event in events if isinstance(event.get("cwd"), str)), None)
    return session_of(relative_path), cwd


def read(relative_path, events, seen, usage, limits, gap):
    session = session_of(relative_path)
    for event in events:
        message = event.get("message") or {}
        counts = message.get("usage")
        if not counts or message.get("model") == "<synthetic>":
            continue
        day = usage.day(event)
        if day is None:
            continue
        message_id = message.get("id")
        if not isinstance(message_id, str) or not message_id:
            gap("missing_message_id", day)
            continue
        usage.cover(day)
        metrics = {
            "input_fresh": counts.get("input_tokens", 0),
            "cache_read": counts.get("cache_read_input_tokens", 0),
            "cache_write": counts.get("cache_creation_input_tokens", 0),
            "output": counts.get("output_tokens", 0),
        }
        if not usage.valid(metrics, day):
            continue
        if message_id not in seen:
            seen[message_id] = (usage, day, session, message.get("model"), metrics)
            continue
        retained = seen[message_id][-1]
        for metric, value in metrics.items():
            retained[metric] = max(retained[metric], value)


def finish(seen):
    for usage, day, session, model, metrics in seen.values():
        usage.add(day, session, model, metrics)
