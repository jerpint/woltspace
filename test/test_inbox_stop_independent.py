"""A manual stop protects that conversation; mail still brings the wolt up."""
import pytest

import inbox_wake
import sessions
from test_inbox_wake import lodge, wake, mail  # noqa: F401


@pytest.mark.parametrize("human", [True, False])
def test_mail_after_a_manual_stop_starts_a_new_session(lodge, wake, monkeypatch, human):
    lodge.registry.create("pal-manual", wolt="pal")
    lodge.registry.update("pal-manual", harness_session_id="conv-stopped")
    sessions.stop_session("pal-manual")
    resumed = []
    monkeypatch.setattr(sessions, "resume_session", lambda *a, **kw: resumed.append(a))
    mail(lodge, human=human)
    inbox_wake._wake(str(lodge.root), "pal")
    assert len(wake.starts) == 1 and wake.starts[0][0]["name"] != "pal-manual"
    assert resumed == []
    stopped = lodge.registry.get("pal-manual", check_alive=False)
    assert stopped["status"] == "stopped" and stopped["wake_inhibit"]
