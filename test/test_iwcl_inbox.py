"""`woltspace session send` with the lodge inbox on: mail for the target's wolt,
never a paste. The real CLI against the inbox routes on loopback, and the lodge's
paste routes on the real app. Dummy roots, the wake recorded at its port."""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from inbox_support import limits, lose_confirmation, reader, rows, session, wolt

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import inbox  # noqa: E402
import inbox_wake  # noqa: E402
import server.app as server_app  # noqa: E402
from server import inbox_routes  # noqa: E402


def enable(root):
    (Path(root) / "woltspace.json").write_text(json.dumps({"inbox": {"enabled": True}}))


@pytest.fixture
def lodge(tmp_path, monkeypatch):
    """The inbox routes on loopback; any other path is recorded, never served."""
    monkeypatch.setattr(inbox_routes, "WOLTS_DIR", tmp_path)
    enable(tmp_path)
    wolt(tmp_path, "nunu")
    woken, strays = [], []
    monkeypatch.setattr(inbox_wake, "notify_admitted", lambda root, status: woken.append(status))
    app = FastAPI()
    app.include_router(inbox_routes.router)

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    async def stray(path: str, request: Request):
        strays.append(path)
        return {"ok": True, "status": "delivered", "session": path}

    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not srv.started:
        assert time.monotonic() < deadline and thread.is_alive()
        time.sleep(0.02)
    store = inbox.Store(tmp_path, limits())
    yield {"api": f"http://127.0.0.1:{srv.servers[0].sockets[0].getsockname()[1]}", "root": tmp_path,
           "token": reader(store, "pal-a-b-a1b2c3"), "woken": woken, "strays": strays}
    srv.should_exit = True
    thread.join(5)


def cli(lodge, *args, text="hello nunu"):
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("TMPDIR", "/tmp"),
           "WOLTSPACE_API": lodge["api"], "WOLTSPACE_INBOX_TOKEN": lodge["token"],
           "WOLTSPACE_WOLT_NAME": "pal", "WOLTSPACE_WOLT_SESSION": "pal-a-b-a1b2c3"}
    return subprocess.run([sys.executable, str(ROOT / "container" / "bin" / "woltspace"), *args],
                          input=text, env=env, capture_output=True, text=True, timeout=30)


def send(lodge, target):
    return cli(lodge, "session", "send", target)


def mail(root):
    return rows(root, "SELECT m.seq, b.wolt, m.priority, m.sender_name, m.body FROM inbox_messages m"
                      " JOIN inbox_mailboxes b ON b.id = m.mailbox ORDER BY m.seq")


def test_a_wolt_with_no_session_gets_mail_and_its_wake(lodge):
    done = send(lodge, "nunu")
    [(seq, to, priority, sender, body)] = mail(lodge["root"])
    assert (done.returncode, done.stdout) == (0, f"queued → nunu (seq {seq})\n")
    assert (to, priority, sender) == ("nunu", inbox.AGENT, "pal")
    assert body.decode().startswith("[message from pal, session=pal-a-b-a1b2c3]\nhello nunu\n")
    assert "woltspace session send pal-a-b-a1b2c3 <<'WOLTSPACE_IWCL_" in body.decode()
    [status] = lodge["woken"]
    assert (status.seq, status.to, status.priority, status.state) == (seq, "nunu", inbox.AGENT, "queued")
    assert lodge["strays"] == []


def test_a_session_target_is_mail_for_its_wolt_and_its_first_choice(lodge):
    assert send(lodge, "nunu-brave-oak-000001").returncode == 0
    assert [r[1] for r in mail(lodge["root"])] == ["nunu"]
    assert lodge["strays"] == []
    prefer = inbox.Store(lodge["root"], limits()).wake_facts("nunu")["wake"]["prefer"]
    assert (prefer["session"], prefer["seq"]) == ("nunu-brave-oak-000001", mail(lodge["root"])[0][0])
    assert send(lodge, "nunu").returncode == 0  # a wolt target names no session: the first choice stays
    assert inbox.Store(lodge["root"], limits()).wake_facts("nunu")["wake"]["prefer"] == prefer


def test_an_enrolled_send_with_a_key_is_the_same_message_when_re_run(lodge):
    for _ in range(2):
        assert cli(lodge, "session", "send", "nunu", "--key", "k-once").returncode == 0
    [(seq, to, *_)] = mail(lodge["root"])  # one message, under the sender's own key
    assert to == "nunu" and rows(lodge["root"], "SELECT key FROM inbox_messages") == [("k-once",)]


def test_a_refusal_is_not_queued_and_falls_back_to_nothing(lodge):
    done = send(lodge, "ghost")
    assert (done.returncode, done.stderr) == (1, "not queued (no_mailbox)\n")
    assert mail(lodge["root"]) == [] and lodge["woken"] == [] and lodge["strays"] == []


def test_an_unknown_outcome_names_its_key_and_is_never_resent(lodge, monkeypatch):
    lose_confirmation(monkeypatch, 1)  # the send commits; its caller sees store_failed
    done = send(lodge, "nunu")
    key = done.stderr.split("check key ")[1].split()[0]
    assert done.returncode == 1 and done.stderr.startswith("not queued (store_failed); check key iwcl-")
    assert f"`woltspace inbox status --key {key}`" in done.stderr
    assert len(mail(lodge["root"])) == 1 and lodge["strays"] == []
    assert json.loads(cli(lodge, "inbox", "status", "--key", key).stdout)["state"] == "queued"


@pytest.fixture
def terminal(tmp_path, monkeypatch):
    """The real app's legacy send routes with the inbox on; the waker records what it is
    asked and answers with the outcome the test sets."""
    monkeypatch.setattr(server_app, "WOLTS_DIR", tmp_path)
    monkeypatch.setattr(server_app, "OUTCOME_WAIT", 1.0)
    monkeypatch.setattr(inbox, "LIMITS", limits())
    enable(tmp_path)
    for name in ("pal", "nunu"):
        wolt(tmp_path, name)
    session(tmp_path, "pal-a-b-a1b2c3", "pal")
    session(tmp_path, "pal-c-d-e5f6a7", "pal", status="stopped")
    seen = {"prefer": [], "outcome": {"how": "hinted", "session": "pal-a-b-a1b2c3"}}

    def wake(root, status):
        store = inbox.Store(root, limits())
        seen["prefer"].append((store.wake_facts(status.to)["wake"].get("prefer") or {}).get("session"))
        ring = ((store.wake_facts(status.to) or {}).get("wake") or {}).get("outcomes", [])
        if seen["outcome"]:
            store.set_wake(status.to, {"outcomes": ring + [{"seq": status.seq, **seen["outcome"]}]})
    monkeypatch.setattr(inbox_wake, "notify_admitted", wake)
    monkeypatch.setattr(server_app, "deliver_message", lambda *a, **k: pytest.fail("pasted"))
    return TestClient(server_app.app, base_url="http://localhost:7777"), tmp_path, seen


def terminal_mail(root):
    return rows(root, "SELECT m.sender, m.priority, b.wolt, m.body FROM inbox_messages m"
                      " JOIN inbox_mailboxes b ON b.id = m.mailbox ORDER BY m.seq")


@pytest.mark.parametrize("path, prefer", [("/sessions/pal-a-b-a1b2c3/message", "pal-a-b-a1b2c3"),
                                          ("/sessions/pal-c-d-e5f6a7/message", None),  # stopped: never the choice
                                          ("/wolts/pal/message", None)])
def test_a_tokenless_send_is_terminal_mail_answered_with_its_own_outcome(terminal, path, prefer):
    client, root, seen = terminal
    answer = client.post(path, json={"text": "hi pal", "from_wolt": "nunu", "from_session": "human", "key": "k1"})
    assert (answer.status_code, answer.json()["status"], answer.json()["session"]) == (200, "notified", "pal-a-b-a1b2c3")
    [(sender, priority, to, body)] = terminal_mail(root)
    assert (sender, priority, to) == ("provider:terminal", inbox.AGENT, "pal")
    assert body.decode().startswith("[message from nunu, session=human]\nhi pal\n")
    assert seen["prefer"] == [prefer]
    again = client.post(path, json={"text": "hi pal", "from_wolt": "nunu", "from_session": "human", "key": "k1"})
    assert again.json()["seq"] == answer.json()["seq"] and len(terminal_mail(root)) == 1  # a re-run is the same message


@pytest.mark.parametrize("token", ["", "not-a-reader"])
def test_a_reader_on_a_legacy_route_is_refused_never_downgraded(terminal, token):
    client, root, _ = terminal
    answer = client.post("/wolts/pal/message", json={"text": "hi"}, headers={"X-Woltspace-Inbox-Token": token})
    assert (answer.status_code, answer.json()["error"]) == (409, "use_inbox_send")
    assert terminal_mail(root) == []


def test_the_sender_hears_cant_wake_or_pending_never_delivered(terminal):
    client, root, seen = terminal
    seen["outcome"] = {"how": "blocked", "reason": "launch_unknown"}
    blocked = client.post("/wolts/pal/message", json={"text": "one", "key": "k1"})
    assert (blocked.status_code, blocked.json()["status"], blocked.json()["reason"]) == (409, "cant_wake", "launch_unknown")
    seen["outcome"] = None  # the wake has not decided within the wait; the earlier mail's outcome is not this one's
    pending = client.post("/wolts/pal/message", json={"text": "two", "key": "k2"})
    assert (pending.status_code, pending.json()["status"], pending.json()["key"]) == (200, "pending", "k2")


def test_the_cli_prints_the_outcome_and_on_no_answer_the_key(tmp_path):
    import socket
    hang = socket.socket()
    hang.bind(("127.0.0.1", 0))
    hang.listen()  # accepts, never answers
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path),
           "WOLTSPACE_API": f"http://127.0.0.1:{hang.getsockname()[1]}", "WOLTSPACE_WOLT_NAME": "nunu"}
    done = subprocess.run([sys.executable, str(ROOT / "container" / "bin" / "woltspace"), "session", "send", "pal",
                           "--key", "k9"], input="hi", env=env, capture_output=True, text=True, timeout=60)
    hang.close()
    assert done.returncode == 1 and "re-run with --key k9 to see its outcome" in done.stderr


@pytest.mark.parametrize("outcome, rc, line", [
    ({"how": "resumed", "session": "pal-a-b-a1b2c3"}, 0, "notified pal-a-b-a1b2c3\n"),
    ({"how": "busy", "session": "pal-a-b-a1b2c3"}, 0,
     "pal-a-b-a1b2c3 is busy; it gets your message when its current turn ends\n"),
    ({"how": "blocked", "reason": "launch_unknown"}, 1, "pal can't wake: launch_unknown\n"),
    (None, 0, "queued for pal; outcome pending: re-run with --key k7 to see it\n")])
def test_a_terminal_send_prints_this_message_s_own_outcome(terminal, outcome, rc, line):
    _, root, seen = terminal
    seen["outcome"] = outcome
    srv = uvicorn.Server(uvicorn.Config(server_app.app, host="127.0.0.1", port=0, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    while not srv.started:
        time.sleep(0.02)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(root), "WOLTSPACE_WOLT_NAME": "nunu",
           "WOLTSPACE_API": f"http://127.0.0.1:{srv.servers[0].sockets[0].getsockname()[1]}"}
    try:
        done = subprocess.run([sys.executable, str(ROOT / "container" / "bin" / "woltspace"), "session", "send", "pal",
                               "--key", "k7"], input="hi", env=env, capture_output=True, text=True, timeout=60)
    finally:
        srv.should_exit = True
        thread.join(5)
    assert (done.returncode, done.stdout + done.stderr) == (rc, line)
