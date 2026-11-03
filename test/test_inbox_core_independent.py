"""Independent real-transaction negatives for the pull mailbox core."""
import hashlib
import traceback

import pytest

import inbox
import lodge_db
from inbox_support import failing, limits, lose_confirmation, reader, rows, session


@pytest.fixture
def box(tmp_path):
    store = inbox.Store(tmp_path, limits())
    token = reader(store, "s1")
    return store, token


def snapshot(store):
    return [rows(store.root, f"SELECT * FROM {table} ORDER BY 1") for table in (
        "inbox_mailboxes", "inbox_readers", "inbox_messages", "inbox_usage", "inbox_events")]


@pytest.mark.parametrize("table", ["inbox_readers", "inbox_usage"])
def test_ack_counter_failure_preserves_body_claim_usage_and_events(box, table):
    store, token = box
    store.send(token, "pal", "k", "custody")
    got = store.fetch(token)
    before = snapshot(store)
    with failing(store.root, table, "UPDATE"):
        with pytest.raises(inbox.Unavailable):
            store.ack(token, got.seq, got.claim)
    assert snapshot(store) == before
    assert store.fetch(token).repeated is True
    store.ack(token, got.seq, got.claim)
    assert rows(store.root, "SELECT acks FROM inbox_readers") == [(1,)]
    assert rows(store.root, "SELECT body FROM inbox_messages") == [(None,)]


@pytest.mark.parametrize("op", ["fetch", "ack"])
def test_committed_lost_reply_retries_keep_one_claim_and_one_ack(box, monkeypatch, op):
    store, token = box
    sent = store.send(token, "pal", "k", "custody")
    got = store.fetch(token) if op == "ack" else None
    lose_confirmation(monkeypatch, 1)
    with pytest.raises(inbox.Unavailable):
        store.fetch(token) if op == "fetch" else store.ack(token, got.seq, got.claim)
    if op == "fetch":
        got = store.fetch(token)
        assert (got.seq, got.claim, got.repeated) == (sent.seq, 1, True)
    else:
        assert store.status(token, "k").state == "acknowledged"
    store.ack(token, got.seq, got.claim)
    assert store.ack(token, got.seq, got.claim).state == "acknowledged"
    assert store.fetch(token) is None
    assert rows(store.root, "SELECT acks FROM inbox_readers") == [(1,)]
    assert rows(store.root, "SELECT count(*) FROM inbox_events WHERE state='acknowledged'") == [(1,)]


@pytest.mark.parametrize("op", ["fetch", "ack", "release", "send", "status"])
def test_reader_session_agent_must_still_match_mailbox_in_every_call(box, op):
    store, token = box
    store.send(token, "pal", "k", "custody")
    got = store.fetch(token)
    session(store.root, "s1", "nunu")
    before = snapshot(store)
    calls = dict(fetch=lambda: store.fetch(token), ack=lambda: store.ack(token, got.seq, got.claim),
                 release=lambda: store.release(token, got.seq, got.claim),
                 send=lambda: store.send(token, "pal", "other", "x"), status=lambda: store.status(token))
    with pytest.raises(inbox.Refused, match="reader_invalid"):
        calls[op]()
    assert snapshot(store) == before
    other = reader(store, "s2")
    assert store.status(other)["held"] == 1


def test_raw_conflict_and_sql_failure_tracebacks_reveal_no_private_values(box):
    store, token = box
    private = "NEUTRAL_PRIVATE_PAYLOAD"
    store.send(token, "pal", private, private)
    with pytest.raises(inbox.Conflict) as conflict:
        store.send(token, "pal", private, "changed")
    assert str(conflict.value) == "conflict"
    assert private not in "".join(traceback.format_exception(conflict.value))
    with lodge_db.write(store.root) as conn:
        conn.execute("CREATE TRIGGER private_failure BEFORE INSERT ON inbox_events BEGIN "
                     "SELECT RAISE(ABORT, 'NEUTRAL_PRIVATE_PAYLOAD'); END")
    with pytest.raises(inbox.Unavailable) as failed:
        store.send(token, "pal", "next", private)
    rendered = "".join(traceback.format_exception(failed.value))
    assert private not in rendered and token not in rendered
    assert failed.value.__suppress_context__ is True
    assert rows(store.root, "SELECT count(*) FROM inbox_messages") == [(1,)]


def test_prune_failure_rolls_back_all_retention_counters_and_preserves_held_mail(box, monkeypatch):
    store, token = box
    clock = [1000.0]
    monkeypatch.setattr(inbox.time, "time", lambda: clock[0])
    store.send(token, "pal", "old", "x")
    got = store.fetch(token)
    store.ack(token, got.seq, got.claim)
    store.send(token, "pal", "held", "y")
    held = store.fetch(token)
    fresh = store.grant("s1", "pal")
    clock[0] += inbox.RETAIN_SECONDS - 1
    assert store.status(fresh, "old").state == "acknowledged"
    clock[0] += 2
    before = snapshot(store)
    with failing(store.root, "inbox_messages", "DELETE"):
        with pytest.raises(inbox.Unavailable):
            store.send(fresh, "pal", "new", "z")
    assert snapshot(store) == before
    store.send(fresh, "pal", "new", "z")
    assert store.status(fresh, "old") is None
    assert store.status(fresh, "held").state == "held"
    assert rows(store.root, "SELECT body FROM inbox_messages WHERE seq=?", held.seq) == [(b"y",)]
    assert rows(store.root, "SELECT acks FROM inbox_readers WHERE hash=?",
                hashlib.sha256(token.encode()).digest()) == [(1,)]
    with pytest.raises(inbox.Refused):
        store.ack(fresh, got.seq, got.claim)


def test_launcher_reader_grant_failure_must_not_start_agent(tmp_path):
    import os
    import shutil
    import subprocess
    from pathlib import Path
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    source = Path(__file__).resolve().parents[1] / "container/bin/run-session.sh"
    shutil.copy2(source, bin_dir)
    marker = tmp_path / "agent-started"
    (bin_dir / "session-reg").write_text(
        "#!/bin/bash\ncase $1 in inbox-grant) exit 1;; prepare) "
        f"echo \"touch '{marker}'\";; *) exit 0;; esac\n")
    (bin_dir / "session-reg").chmod(0o755)
    env = {"PATH": os.environ["PATH"], "WOLTSPACE_WOLTS_DIR": str(tmp_path)}
    run = subprocess.run(["bash", str(bin_dir / "run-session.sh"), "s1"], env=env,
                         capture_output=True, text=True, timeout=30)
    assert run.returncode != 0 and not marker.exists()
