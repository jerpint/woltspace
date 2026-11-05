"""Conversation history (conversation_log.py): what inbox admission and notify archive,
the bounds, the reader-scoped pages, and how backup and restore treat it."""

import asyncio
import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from inbox_support import acked, failing, limits, reader, rows, session

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import conversation_log  # noqa: E402
import inbox  # noqa: E402
import lodge_db  # noqa: E402
from server import notify  # noqa: E402
from server.notification_types import Plugin  # noqa: E402
from woltspace import backup  # noqa: E402

SECRET = "dummy-secret-7f3a"


def lodge(root, on=True, **over):
    root.mkdir(parents=True, exist_ok=True)
    (root / "woltspace.json").write_text(json.dumps({"inbox": {"enabled": True},
                                                     "conversation_log": {"enabled": on}}))
    return inbox.Store(root, limits(**over))


def archived(root):
    return rows(root, "SELECT kind, sender_mailbox, sender_name, recipient_mailbox, recipient_name,"
                      " human, body, outcome FROM conversation_log ORDER BY id")


def counters_match(root):
    count, size = rows(root, "SELECT count(*), coalesce(sum(size), 0) FROM conversation_log")[0]
    assert rows(root, "SELECT rows, bytes FROM conversation_usage") == [(count, size)]


def test_off_by_default_captures_nothing_and_mail_flows_as_before(tmp_path):
    store = inbox.Store(tmp_path, limits())  # no woltspace.json at all
    token = reader(store, "s1")
    store.send(token, "pal", "k", "hi")
    got, status = acked(store, token)
    assert (got.body, status.state) == ("hi", "acknowledged")
    assert archived(tmp_path) == [] and store.archive is False
    assert store.history(token)["records"] == [] and store.history(token)["capture"] is False


def test_one_row_per_new_admission_kept_after_ack_and_after_turning_off(tmp_path):
    store = lodge(tmp_path)
    alpha = reader(store, "a1", "alpha")
    reader(store, "b1", "beta")
    store.send(alpha, "beta", "k", "héllo")
    store.send(alpha, "beta", "k", "héllo")  # the retry returns the first, archives nothing
    store.admit("chat", "c1", "alpha", "from the human", human=True)
    assert archived(tmp_path) == [("inbox", 1, "alpha", 2, "beta", 0, "héllo", None),
                                  ("inbox", None, "chat", 1, "alpha", 1, "from the human", None)]
    acked(store, alpha)  # the operational body goes, the archive's stays
    assert rows(tmp_path, "SELECT body FROM inbox_messages WHERE key='c1'") == [(None,)]
    off = lodge(tmp_path, on=False)
    off.send(alpha, "beta", "k2", "not kept")
    assert len(archived(tmp_path)) == 2
    assert [r["text"] for r in off.history(alpha)["records"]] == ["from the human", "héllo"]
    counters_match(tmp_path)


def test_a_failed_archive_write_refuses_the_admission_with_a_fixed_error(tmp_path):
    store = lodge(tmp_path)
    token = reader(store, "s1")
    store.send(token, "pal", "warm", "x")  # the archive tables exist and hold one row
    with failing(tmp_path, "conversation_log"):
        with pytest.raises(inbox.Unavailable) as error:
            store.send(token, "pal", "k", SECRET)
    assert SECRET not in str(error.value) and error.value.__cause__ is None
    assert rows(tmp_path, "SELECT key FROM inbox_messages") == [("warm",)]
    assert len(archived(tmp_path)) == 1
    store.send(token, "pal", "k", SECRET)  # the same key goes through once the store works
    assert len(archived(tmp_path)) == 2


def test_eviction_keeps_both_bounds_and_the_counters(tmp_path, monkeypatch):
    monkeypatch.setattr(conversation_log, "LIMITS", conversation_log.Limits(
        rows=3, text_bytes=40 * 1024, prefix_bytes=16 * 1024))
    lodge_db.connect(tmp_path).close()
    with lodge_db.write(tmp_path) as conn:
        for n in range(5):
            conversation_log.append(conn, kind="notify", text=f"m{n}", sender_mailbox=1, sender_name="a",
                                    recipient_mailbox=None, recipient_name="telegram", human=True, at=n)
    assert rows(tmp_path, "SELECT body FROM conversation_log ORDER BY id") == [("m2",), ("m3",), ("m4",)]
    counters_match(tmp_path)
    with lodge_db.write(tmp_path) as conn:
        for n in range(3):  # 16 KiB each: bytes, not rows, now decide
            conversation_log.append(conn, kind="notify", text="é" * 8192, sender_mailbox=1, sender_name="a",
                                    recipient_mailbox=None, recipient_name="telegram", human=True, at=9)
    assert rows(tmp_path, "SELECT id FROM conversation_log ORDER BY id") == [(7,), (8,)]
    counters_match(tmp_path)


def test_long_text_keeps_a_valid_utf8_prefix_marked_truncated(tmp_path):
    lodge_db.connect(tmp_path).close()
    small = conversation_log.Limits(rows=10, text_bytes=64 * 1024, prefix_bytes=11)  # the cut falls inside the 3-byte €
    with lodge_db.write(tmp_path) as conn:
        conversation_log.append(conn, kind="notify", text="abcdefghé€", sender_mailbox=1, sender_name="a",
                                recipient_mailbox=None, recipient_name="slack", human=True, at=1, limits=small)
    assert rows(tmp_path, "SELECT body, size, original_size FROM conversation_log") == [("abcdefghé", 10, 13)]


def test_reads_are_scoped_by_mailbox_on_both_sides_never_by_name(tmp_path):
    store = lodge(tmp_path)
    alpha, beta, gamma = (reader(store, f"{w}1", w) for w in ("alpha", "beta", "gamma"))
    store.send(alpha, "beta", "k1", "to beta")
    store.send(alpha, "alpha", "k2", "to myself")
    with lodge_db.write(tmp_path) as conn:  # notify text with no mailbox: nobody's
        conversation_log.append(conn, kind="notify", text="loose", sender_mailbox=None, sender_name="alpha",
                                recipient_mailbox=None, recipient_name="telegram", human=True, at=1)

    def seen(token):
        return [(r["text"], r["direction"]) for r in store.history(token)["records"]]
    assert seen(alpha) == [("to myself", "out"), ("to beta", "out")]
    assert seen(beta) == [("to beta", "in")]
    assert seen(gamma) == []
    acked(store, beta)
    store.set_state("beta", "closed")  # beta is recreated: a new mailbox, no inherited history
    assert seen(reader(store, "b2", "beta")) == []
    with pytest.raises(inbox.Refused, match="reader_invalid"):
        store.history(beta)


def _page_all(store, token, **kw):
    pages, before = [], None
    while True:
        page = store.history(token, before=before, **kw)
        pages.append(page)
        if page["next"] is None:
            return pages
        before = page["next"]


def test_pages_stay_within_max_bytes_and_a_huge_row_is_skipped_not_stuck(tmp_path):
    store = lodge(tmp_path, pending_bytes=100_000, mailbox_bytes=100_000)
    token = reader(store, "s1")
    texts = ["é\"\\\n" * 300, "x" * 30_000, "\x01" * 1000, "plain"]  # escapes: JSON is larger than text
    for n, text in enumerate(texts):
        store.send(token, "pal", f"k{n}", text)
    pages = _page_all(store, token, limit=2, max_bytes=8192)
    for page in pages:  # bytes counts the records exactly as the route serializes them
        sent = json.dumps(page["records"], ensure_ascii=False, separators=(",", ":")).encode()
        assert page["bytes"] <= 8192 and len(sent) == page["bytes"] + len(page["records"]) + 1
    records = [r for page in pages for r in page["records"]]
    assert [r["id"] for r in records] == [4, 3, 2, 1]  # every row once, newest first
    big = records[2]  # the 30 KB one
    assert big["id"] == 2 and "text" not in big and "max_bytes" in big["omitted"]
    full = store.history(token, before=3, limit=1, max_bytes=64 * 1024)["records"][0]
    assert full["text"] == "x" * 30_000
    window = pages[0]["retained"]
    assert (window["oldest"], window["newest"], window["evicted"]) == (1, 4, 0) and pages[0]["capture"]
    for bad in ({"limit": 0}, {"limit": 201}, {"max_bytes": 100}, {"max_bytes": 9 * 2**20}, {"before": 0}):
        with pytest.raises(inbox.Invalid):
            store.history(token, **bad)
    assert rows(tmp_path, "SELECT state FROM inbox_messages") == [("queued",)] * 4  # reading took nothing


# -- notify -------------------------------------------------------------------

def _notify_lodge(tmp_path, monkeypatch, send):
    store = lodge(tmp_path)
    reader(store, "s1", "pal")
    monkeypatch.setattr(notify, "WOLTS_DIR", tmp_path)
    monkeypatch.setattr(notify, "_connector_up", lambda name: True)
    monkeypatch.setattr(notify, "append_chat_history", Mock())
    monkeypatch.setitem(notify.SENDERS, "telegram", Plugin("telegram", send))
    threads = []
    real = conversation_log.record_attempt

    def record_attempt(*args):
        threads.append(threading.current_thread() is threading.main_thread())
        return real(*args)
    monkeypatch.setattr(conversation_log, "record_attempt", record_attempt)
    return threads


def _send(text, session="s1"):
    return notify._send("telegram", session, text, "42", session_record={"name": "s1", "wolt": "pal"})


@pytest.mark.asyncio
@pytest.mark.parametrize("route_session", ["s1", ""])  # "": an explicit route outside the session's thread
async def test_notify_records_the_attempt_off_the_loop_then_accepted(tmp_path, monkeypatch, route_session):
    sent = []

    async def send(message, destination, context):
        sent.append(archived(tmp_path)[-1][-1])  # the attempt is on disk before the send
    threads = _notify_lodge(tmp_path, monkeypatch, send)
    await _send("done", route_session)
    assert sent == ["unknown"] and threads == [False]
    assert archived(tmp_path) == [("notify", 1, "pal", None, "telegram", 1, "done", "accepted")]


@pytest.mark.asyncio
async def test_notify_refuses_to_send_what_it_cannot_record(tmp_path, monkeypatch):
    send = Mock()
    _notify_lodge(tmp_path, monkeypatch, send)
    lodge_db.connect(tmp_path).close()
    with failing(tmp_path, "conversation_log"):
        with pytest.raises(conversation_log.Unavailable) as error:
            await _send(SECRET)
    assert not send.called and SECRET not in str(error.value) and error.value.__cause__ is None


@pytest.mark.asyncio
async def test_a_lost_outcome_keeps_the_providers_success_and_never_resends(tmp_path, monkeypatch, caplog):
    calls = []

    async def send(message, destination, context):
        calls.append(message.text)
    _notify_lodge(tmp_path, monkeypatch, send)
    monkeypatch.setattr(conversation_log, "set_outcome", Mock(side_effect=OSError(SECRET)))
    result = await _send(SECRET)
    assert result["adapter"] == "telegram" and calls == [SECRET]
    assert archived(tmp_path)[-1][-1] == "unknown" and SECRET not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [RuntimeError("provider down"), asyncio.CancelledError()])
async def test_an_error_or_cancel_leaves_the_attempt_unknown_and_unsent_again(tmp_path, monkeypatch, fault):
    calls = []

    async def send(message, destination, context):
        calls.append(1)
        raise fault
    _notify_lodge(tmp_path, monkeypatch, send)
    with pytest.raises(type(fault)):
        await _send("maybe")
    assert calls == [1] and archived(tmp_path)[-1][-2:] == ("maybe", "unknown")


def test_an_evicted_attempt_is_not_brought_back_by_its_outcome(tmp_path):
    lodge_db.connect(tmp_path).close()
    session(tmp_path, "s1")
    attempt = conversation_log.record_attempt(tmp_path, "s1", "telegram", "x", time.time())
    with lodge_db.write(tmp_path) as conn:
        conn.execute("DELETE FROM conversation_log")
    conversation_log.set_outcome(tmp_path, attempt, "accepted")
    assert archived(tmp_path) == []


# -- backup and restore ----------------------------------------------------------

def test_an_archive_restores_with_or_without_mail(tmp_path, monkeypatch):
    source = tmp_path / "wolts"
    store = lodge(source)
    (source / ".space" / "platform").mkdir(parents=True, exist_ok=True)
    token = reader(store, "s1")
    store.send(token, "pal", "k", "kept as history")
    queued = backup.restore_backup(backup.create_backup(source, out_dir=tmp_path / "o1").archive,
                                   to=tmp_path / "b1").wolts_dir  # queued mail restores as it is
    assert archived(queued) == archived(source)
    assert rows(queued, "SELECT state FROM inbox_messages") == [("queued",)]
    acked(store, token)
    later = time.time() + inbox.RETAIN_SECONDS + 1
    monkeypatch.setattr(inbox.time, "time", lambda: later)
    assert store.fetch(token) is None  # a write: the expired receipt is pruned, the archive is not
    restored = backup.restore_backup(backup.create_backup(source, out_dir=tmp_path / "o2").archive,
                                     to=tmp_path / "b2").wolts_dir
    assert archived(restored) == archived(source) and len(archived(source)) == 1
    assert rows(restored, "SELECT count(*) FROM inbox_messages") == [(0,)]
