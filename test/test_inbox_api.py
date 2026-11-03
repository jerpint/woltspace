"""The lodge inbox's edges: the HTTP routes, `woltspace inbox`, the launcher's
grant, and archive/delete. Real SQL and a real loopback test server, dummy roots."""

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from starlette.testclient import TestClient

from inbox_support import acked, limits, reader, rows, session, wolt

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import inbox  # noqa: E402
import wolt_archive  # noqa: E402
from server import inbox_routes  # noqa: E402

BODY, TOKEN_HEADER = "DUMMY_BODY_MARKER", inbox_routes.TOKEN_HEADER


def enable(root):
    (Path(root) / "woltspace.json").write_text(json.dumps({"inbox": {"enabled": True}}))


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(inbox_routes, "WOLTS_DIR", tmp_path)
    for name in ("pal", "nunu"):
        wolt(tmp_path, name)
    return inbox.Store(tmp_path, limits())


@pytest.fixture
def client(store):
    app = FastAPI()
    app.include_router(inbox_routes.router)
    return TestClient(app, base_url="http://localhost:7777")


@pytest.fixture
def server(client):
    """The same routes, served on loopback for a real CLI process."""
    srv = uvicorn.Server(uvicorn.Config(client.app, host="127.0.0.1", port=0, log_level="info"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not srv.started:
        assert time.monotonic() < deadline and thread.is_alive()
        time.sleep(0.02)
    yield f"http://127.0.0.1:{srv.servers[0].sockets[0].getsockname()[1]}"
    srv.should_exit = True
    thread.join(5)


def call(client, verb, token="", **body):
    reply = client.post(f"/inbox/{verb}", json=body, headers={TOKEN_HEADER: token} if token else {})
    return reply.status_code, reply.json()


def test_the_lodge_serves_the_inbox_only_when_enabled(tmp_path, monkeypatch):
    import server.app as server_app
    monkeypatch.setattr(inbox_routes, "WOLTS_DIR", tmp_path)
    lodge = TestClient(server_app.app, base_url="http://localhost:7777")
    assert lodge.post("/inbox/fetch").json() == {"error": "inbox_disabled"}
    enable(tmp_path)
    assert lodge.post("/inbox/fetch").status_code == 401
    assert not (tmp_path / ".space").exists()  # neither answer opened lodge.db


def test_routes_take_only_a_reader_and_their_own_fields(tmp_path, store, client):
    enable(tmp_path)
    token = reader(store, "s1")
    for verb, token_, body, expect in (
            ("fetch", "", {}, (401, "reader_invalid")),
            ("fetch", "t" * 43, {}, (401, "reader_invalid")),
            ("send", token, dict(to="nunu", key="k", text=BODY, human=True), (400, "invalid")),
            ("send", token, dict(to="nunu", key="k", text=BODY, priority=0), (400, "invalid")),
            ("send", token, dict(to="nunu", key="k", text=BODY, sender="owner"), (400, "invalid")),
            ("ack", token, dict(seq="1", claim=1), (400, "invalid")),
            ("ack", token, dict(seq=1, claim=1), (409, "stale_claim")),
            ("status", token, dict(key=7), (400, "invalid")),
            ("purge", token, {}, (404, "unsupported"))):
        status, answer = call(client, verb, token_, **body)
        assert (status, answer) == (expect[0], {"error": expect[1]})
    reply = client.post("/inbox/send", content=b"{not json " + BODY.encode(), headers={TOKEN_HEADER: token})
    assert (reply.status_code, reply.json()) == (400, {"error": "invalid"})
    assert rows(tmp_path, "SELECT count(*) FROM inbox_messages") == [(0,)]
    status, sent = call(client, "send", token, to="nunu", key="k", text=BODY)
    assert sent["result"]["priority"] == inbox.AGENT and sent["result"]["sender"] == "pal"
    status, answer = call(client, "send", token, to="nunu", key="k", text="changed")
    assert (status, answer) == (409, {"error": "conflict"})


def _cli(api, *args, token=None, stdin=""):
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("TMPDIR", "/tmp"), "WOLTSPACE_API": api}
    if token:
        env["WOLTSPACE_INBOX_TOKEN"] = token
    return subprocess.run([sys.executable, str(ROOT / "container" / "bin" / "woltspace"), "inbox", *args],
                          input=stdin, env=env, capture_output=True, text=True, timeout=30)


def test_the_cli_sends_fetches_and_acks_through_a_real_server(tmp_path, store, server, caplog):
    enable(tmp_path)
    caplog.set_level(logging.DEBUG)
    pal, nunu = reader(store, "s1"), reader(store, "s2", "nunu")
    assert _cli(server, "send", "nunu", "--key", "k1", token=pal, stdin=BODY).returncode == 0
    first = json.loads(_cli(server, "fetch", token=nunu).stdout)
    again = json.loads(_cli(server, "fetch", token=nunu).stdout)  # the reply was lost: same claim
    assert (first["body"], first["sender"], again["repeated"]) == (BODY, "pal", True)
    assert (again["seq"], again["claim"]) == (first["seq"], first["claim"])
    ack = _cli(server, "ack", str(first["seq"]), str(first["claim"]), token=nunu)
    assert json.loads(ack.stdout)["state"] == "acknowledged"
    assert json.loads(_cli(server, "status", "--key", "k1", token=pal).stdout)["state"] == "acknowledged"
    assert json.loads(_cli(server, "fetch", token=nunu).stdout) is None
    refused = _cli(server, "ack", str(first["seq"]), str(first["claim"]), token=pal)
    assert (refused.returncode, refused.stderr.strip()) == (1, "woltspace inbox ack: stale_claim")
    empty = _cli(server, "status", "--key", "", token=pal)  # a supplied key is checked, even empty
    assert (empty.returncode, empty.stderr.strip()) == (1, "woltspace inbox status: invalid")
    missing = _cli(server, "fetch")
    assert (missing.returncode, missing.stderr.strip()) == (2, "woltspace: this session has no inbox reader")
    logged = caplog.text
    assert "/inbox/fetch" in logged and BODY not in logged and pal not in logged and nunu not in logged


def _session_reg(root, *args):
    env = {"PATH": os.environ.get("PATH", ""), "WOLTSPACE_WOLTS_DIR": str(root), "WOLTSPACE_PYTHON": sys.executable}
    return subprocess.run([str(ROOT / "container" / "bin" / "session-reg"), *args], env=env,
                          capture_output=True, text=True, timeout=30)


def test_the_launchers_grant_is_fresh_per_session_and_absent_while_off(tmp_path, store):
    session(tmp_path, "s1")
    assert _session_reg(tmp_path, "inbox-grant", "s1").stdout == ""  # off: nothing is made
    assert rows(tmp_path, "SELECT count(*) FROM inbox_readers") == [(0,)]
    enable(tmp_path)
    first = _session_reg(tmp_path, "inbox-grant", "s1").stdout.strip()
    second = _session_reg(tmp_path, "inbox-grant", "s1").stdout.strip()
    assert first and second and first != second
    store.admit("ops", "k", "pal", BODY)
    with pytest.raises(inbox.Refused, match="reader_invalid"):
        store.fetch(first)  # the relaunch revoked it
    assert store.fetch(second).body == BODY
    session(tmp_path, "s1", wolt="ghost")  # now a wolt with no manifest: no mailbox, launch goes on
    assert _session_reg(tmp_path, "inbox-grant", "s1").stdout == ""
    assert rows(tmp_path, "SELECT count(*) FROM inbox_readers WHERE session='s1' AND revoked=0") == [(0,)]
    assert rows(tmp_path, "SELECT count(*) FROM inbox_readers WHERE hash=?", first.encode()) == [(0,)]
    missing = _session_reg(tmp_path, "inbox-grant", "nope")  # on: no row is a failure, not "off"
    assert (missing.returncode, missing.stdout, missing.stderr) == (1, "", "inbox-grant: no_session\n")


def test_the_launcher_keeps_the_token_out_of_argv_logs_and_traces(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shutil.copy(ROOT / "container" / "bin" / "run-session.sh", bin_dir)
    token = "DUMMY_TOKEN_" + "x" * 30
    (bin_dir / "session-reg").write_text(  # a stand-in registry: grants the token, runs printenv
        "#!/bin/bash\ncase $1 in inbox-grant) echo " + token + ";;\n"
        "prepare) echo 'echo \"${WOLTSPACE_INBOX_TOKEN-unset}\" > \"$OUT\"; ps -o args= -p $$ $PPID >> \"$OUT\"';;\n"
        "*) exit 0;;\nesac\n")
    (bin_dir / "session-reg").chmod(0o755)
    env = {"PATH": os.environ.get("PATH", ""), "WOLTSPACE_WOLTS_DIR": str(tmp_path), "OUT": str(tmp_path / "out"),
           "SHELLOPTS": "xtrace", "WOLTSPACE_INBOX_TOKEN": "inherited"}
    run = subprocess.run(["bash", "-x", str(bin_dir / "run-session.sh"), "s1"], env=env,
                         capture_output=True, text=True, timeout=30)
    seen = (tmp_path / "out").read_text().splitlines()
    assert seen[0] == token and all(token not in line for line in seen[1:])
    log = (tmp_path / ".space" / "logs" / "session-boot.log").read_text()
    assert token not in run.stderr + run.stdout + log
    (bin_dir / "session-reg").write_text((bin_dir / "session-reg").read_text().replace("echo " + token, "true"))
    subprocess.run(["bash", str(bin_dir / "run-session.sh"), "s1"], env=env, timeout=30, check=True)
    assert (tmp_path / "out").read_text().splitlines()[0] == "unset"  # off: an inherited one is dropped
    (bin_dir / "session-reg").write_text((bin_dir / "session-reg").read_text().replace(
        "inbox-grant) true", "inbox-grant) echo 'inbox-grant: store_failed' >&2; exit 1"))
    failed = subprocess.run(["bash", str(bin_dir / "run-session.sh"), "s1"], env=env, timeout=30)
    log = (tmp_path / ".space" / "logs" / "session-boot.log").read_text()
    assert failed.returncode != 0 and "inbox-grant: store_failed" in log  # stopped, and says why


def test_archive_blocks_the_mailbox_and_delete_waits_for_custody(tmp_path, store, live_runtime):
    token = reader(store, "s1")
    store.admit("ops", "k", "pal", BODY)
    wolt_archive.archive_wolt("pal", wolts_dir=tmp_path)
    assert rows(tmp_path, "SELECT state FROM inbox_mailboxes") == [("archived",)]
    with pytest.raises(inbox.Refused, match="mailbox_archived"):
        store.admit("ops", "k2", "pal", BODY)
    with pytest.raises(wolt_archive.ArchiveError) as refused:
        wolt_archive.delete_archived_wolt("pal", wolts_dir=tmp_path)
    assert refused.value.status == 409 and wolt_archive.in_archive("pal", wolts_dir=tmp_path)
    wolt_archive.unarchive_wolt("pal", wolts_dir=tmp_path)
    session(tmp_path, "s1")  # archive stopped it; a new run of the same session
    acked(store, store.grant("s1", "pal"))
    wolt_archive.archive_wolt("pal", wolts_dir=tmp_path)
    wolt_archive.delete_archived_wolt("pal", wolts_dir=tmp_path)
    assert rows(tmp_path, "SELECT id, state FROM inbox_mailboxes") == [(1, "closed")]
    wolt(tmp_path, "pal")
    assert reader(store, "s9") and rows(tmp_path, "SELECT id, state FROM inbox_mailboxes") == [
        (1, "closed"), (2, "open")]
    with pytest.raises(inbox.Refused, match="reader_invalid"):
        store.fetch(token)


def test_an_archive_that_cannot_end_a_session_leaves_the_mailbox_open(tmp_path, store, live_runtime, monkeypatch):
    store.admit("ops", "k", "pal", BODY)

    def refuse(*a, **k):
        raise wolt_archive.ArchiveError(500, "session pal-s could not be stopped")
    monkeypatch.setattr(wolt_archive, "_end_live", refuse)
    with pytest.raises(wolt_archive.ArchiveError):
        wolt_archive.archive_wolt("pal", wolts_dir=tmp_path)
    assert rows(tmp_path, "SELECT state FROM inbox_mailboxes") == [("open",)]  # the wolt is still live
    store.admit("ops", "k2", "pal", BODY)


def test_archive_and_delete_without_a_mailbox_change_nothing_of_the_inbox(tmp_path, store, live_runtime):
    wolt_archive.archive_wolt("pal", wolts_dir=tmp_path)
    wolt_archive.delete_archived_wolt("pal", wolts_dir=tmp_path)
    assert rows(tmp_path, "SELECT count(*) FROM inbox_mailboxes") == [(0,)]


def test_ready_takes_an_optional_kind_and_nothing_else(tmp_path, store, client, monkeypatch):
    enable(tmp_path)
    import inbox_wake
    seen = []
    monkeypatch.setattr(inbox_wake, "ready", lambda root, token, session, attempt, kind: seen.append(kind) or True)
    for body, expect in (({"session": "s1", "attempt": "a1"}, 200),
                         ({"session": "s1", "attempt": "a1", "kind": "turn_end"}, 200),
                         ({"session": "s1", "attempt": "a1", "kind": ""}, 400),
                         ({"session": "s1", "attempt": "a1", "other": "x"}, 400)):
        assert client.post("/inbox/ready", json=body).status_code == expect
    assert seen == ["ready", "turn_end"]
