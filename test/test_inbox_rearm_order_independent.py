"""Rearm preserves stop marks; those conversations remain excluded from resume."""
import inbox_wake
import sessions
from test_inbox_wake import lodge, wake, mail  # noqa: F401


def test_stop_marks_before_and_after_rearm_survive_and_exclude_resume(lodge, wake, monkeypatch):
    monkeypatch.setattr(inbox_wake.time, "time", lambda: 10000)
    monkeypatch.setattr(inbox_wake, "_schedule", lambda *a, **kw: None)
    mail(lodge)
    lodge.registry.create("pal-old", wolt="pal")
    lodge.registry.update("pal-old", harness_session_id="conv-old")
    sessions.stop_session("pal-old")
    inbox_wake.rearm(lodge.root, "pal")
    assert lodge.registry.get("pal-old", check_alive=False)["wake_inhibit"]
    lodge.registry.create("pal-new", wolt="pal")
    lodge.registry.update("pal-new", harness_session_id="conv-new")
    sessions.stop_session("pal-new")
    # Even if a restored record is classified resting, its explicit stop mark wins.
    for name in ("pal-old", "pal-new"):
        lodge.registry.update(name, status="resting")
    resumed = []
    monkeypatch.setattr(sessions, "resume_session", lambda *a, **kw: resumed.append(a))
    mail(lodge, "k2")
    inbox_wake._wake(str(lodge.root), "pal")
    assert len(wake.starts) == 1 and resumed == []
    assert all(lodge.registry.get(name, check_alive=False)["wake_inhibit"]
               for name in ("pal-old", "pal-new"))
