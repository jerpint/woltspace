"""A manual launch with unknown completion must block a wake's second worker."""
import inbox_wake
import sessions
from test_inbox_wake import lodge, unresolved, mail  # noqa: F401


def test_unreserved_unknown_launch_blocks_another_worker(lodge, unresolved, monkeypatch):
    monkeypatch.setattr(sessions, "_tmux_sessions", lambda: set())
    monkeypatch.setattr(sessions, "sessions_with_agent_process", lambda: set())
    monkeypatch.setattr(sessions, "start_session", lambda **kw: lodge.calls.spawned.append(kw) or {})
    mail(lodge)
    inbox_wake._wake(str(lodge.root), "pal")
    assert lodge.calls.spawned == [], "wake created another worker while a manual launch remained unknown"
    assert lodge.registry.get(unresolved, check_alive=False)["launching"]
