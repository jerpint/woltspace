"""Read Codex request deltas and compare them with the final file counters."""

ROOT = ".codex/sessions"
COUNTERS = (
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
    "output_tokens", "reasoning_output_tokens", "total_tokens",
)


def identity(relative_path, events):
    """(session id, working folder recorded at the start) for native attribution."""
    session, cwd = relative_path.stem, None
    for event in events:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        if event.get("type") == "session_meta":
            session = payload.get("id") or payload.get("session_id") or session
            cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else cwd
            break
        if event.get("type") == "turn_context" and isinstance(payload.get("cwd"), str):
            cwd = cwd or payload["cwd"]
    return session, cwd


def read(relative_path, events, seen, usage, limits, gap):
    session, model = relative_path.stem, None
    summed = dict.fromkeys(COUNTERS, 0)
    final, days = None, set()
    for event in events:
        payload = event.get("payload") or {}
        kind = event.get("type")
        if kind == "session_meta":
            session = payload.get("id") or payload.get("session_id") or session
            model = payload.get("model") or model
        elif kind == "turn_context":
            model = payload.get("model") or model
        elif kind == "event_msg" and payload.get("type") == "token_count":
            limits(event.get("timestamp"), payload.get("rate_limits"))
            info = payload.get("info") or {}
            total = info.get("total_token_usage")
            if total and total == final:
                continue
            final = total or final
            delta = info.get("last_token_usage")
            if delta is None:
                continue
            day = usage.day(event)
            if day:
                days.add(day)
                usage.cover(day)
            values = {key: delta.get(key, 0) for key in COUNTERS}
            if not all(type(value) is int and value >= 0 for value in values.values()):
                gap("invalid_token_counts", day)
                continue
            for key, value in values.items():
                summed[key] += value
            request_model = payload.get("model") or model
            if day and request_model != "<synthetic>":
                cached = values["cached_input_tokens"]
                usage.add(day, session, request_model, {
                    "input_fresh": values["input_tokens"] - cached,
                    "cache_read": cached,
                    "cache_write": values["cache_write_input_tokens"],
                    "output": values["output_tokens"],
                })
    if (final is None and (days or any(summed.values()))) or (
        final is not None and any(final.get(key, 0) != value for key, value in summed.items())
    ):
        for day in sorted(days) or [None]:
            gap("codex_total_mismatch", day)
