"""chat_state: which wolt and session each chat talks to, in lodge.db."""

import json
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "container" / "lib"))
sys.path.insert(0, str(ROOT / "container"))

import chat_state
import lodge_db


def _rows(wolts_dir):
    with closing(lodge_db.connect(wolts_dir, readonly=True)) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT adapter, chat_id, active_wolt, active_session FROM chat_state"
            " ORDER BY adapter, chat_id")]


def test_a_saved_chat_reads_back_with_its_extra_keys(tmp_path):
    chat_state.put(tmp_path, "telegram", 42,
                   {"active_wolt": "pal", "active_session": "pal-a", "note": "x"})

    assert chat_state.get(tmp_path, "telegram", 42) == {
        "active_wolt": "pal", "active_session": "pal-a", "note": "x"}
    assert _rows(tmp_path) == [{"adapter": "telegram", "chat_id": "42",
                                "active_wolt": "pal", "active_session": "pal-a"}]


def test_an_unknown_chat_is_empty(tmp_path):
    assert chat_state.get(tmp_path, "telegram", 7) == {}


def test_a_save_replaces_the_chat_and_drops_a_cleared_session(tmp_path):
    chat_state.put(tmp_path, "telegram", 42, {"active_wolt": "pal", "active_session": "pal-a"})
    chat_state.put(tmp_path, "telegram", 42, {"active_wolt": "nunu"})

    assert chat_state.get(tmp_path, "telegram", 42) == {"active_wolt": "nunu"}


def test_chats_are_keyed_by_adapter(tmp_path):
    chat_state.put(tmp_path, "telegram", "D1", {"active_wolt": "pal"})
    chat_state.put(tmp_path, "slack", "D1", {"active_wolt": "nunu"})

    assert chat_state.get(tmp_path, "telegram", "D1") == {"active_wolt": "pal"}
    assert chat_state.get(tmp_path, "slack", "D1") == {"active_wolt": "nunu"}


def test_delete_forgets_one_chat(tmp_path):
    chat_state.put(tmp_path, "slack", "D1:1.1", {"active_wolt": "pal"})
    chat_state.put(tmp_path, "slack", "D1:2.2", {"active_wolt": "pal"})

    chat_state.delete(tmp_path, "slack", "D1:1.1")

    assert [r["chat_id"] for r in _rows(tmp_path)] == ["D1:2.2"]


def test_forget_session_clears_it_from_that_adapters_chats_only(tmp_path):
    chat_state.put(tmp_path, "telegram", 1, {"active_wolt": "pal", "active_session": "pal-a"})
    chat_state.put(tmp_path, "telegram", 2, {"active_wolt": "pal", "active_session": "pal-b"})
    chat_state.put(tmp_path, "slack", "D1:1.1", {"active_wolt": "pal", "active_session": "pal-a"})

    assert chat_state.forget_session(tmp_path, "telegram", "pal-a") == 1

    assert chat_state.get(tmp_path, "telegram", 1) == {"active_wolt": "pal"}
    assert chat_state.get(tmp_path, "telegram", 2)["active_session"] == "pal-b"
    assert chat_state.get(tmp_path, "slack", "D1:1.1")["active_session"] == "pal-a"


# --- Telegram ---


def _telegram(monkeypatch, tmp_path):
    # bot.core makes its log dir under the wolts dir at import: keep it here.
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(tmp_path))
    import chat_lodge
    import connector_registry

    (tmp_path / "pal" / "wolt").mkdir(parents=True)
    (tmp_path / "pal" / "wolt" / "wolt.json").write_text("{}")
    # The lodge's side of the Telegram connector keeps its chats.
    return chat_lodge.ChatLodge(connector_registry.spec("telegram"), ())


def test_telegram_chat_state_lives_in_the_lodge_db(monkeypatch, tmp_path):
    adapter = _telegram(monkeypatch, tmp_path)

    adapter._save_chat_state(42, {"active_wolt": "pal", "active_session": "pal-a"})

    assert _rows(tmp_path) == [{"adapter": "telegram", "chat_id": "42",
                                "active_wolt": "pal", "active_session": "pal-a"}]
    loaded = adapter._load_chat_state(42)
    assert (loaded["active_wolt"], loaded["active_session"]) == ("pal", "pal-a")
    assert not (tmp_path / ".space" / "telegram" / "chats").exists()


def test_telegram_chat_on_a_gone_wolt_reads_as_no_choice(monkeypatch, tmp_path):
    adapter = _telegram(monkeypatch, tmp_path)
    adapter._save_chat_state(42, {"active_wolt": "nunu", "active_session": "nunu-a"})

    loaded = adapter._load_chat_state(42)

    assert "active_wolt" not in loaded and "active_session" not in loaded


def test_telegram_forgets_a_killed_session_in_every_chat(monkeypatch, tmp_path):
    adapter = _telegram(monkeypatch, tmp_path)
    adapter._save_chat_state(1, {"active_wolt": "pal", "active_session": "pal-a"})
    adapter._save_chat_state(2, {"active_wolt": "pal", "active_session": "pal-a"})
    adapter._save_chat_state(3, {"active_wolt": "pal", "active_session": "pal-b"})

    adapter._forget_session("pal-a")

    assert [adapter._load_chat_state(c).get("active_session") for c in (1, 2, 3)] == [
        None, None, "pal-b"]


def test_a_lodge_without_migration_3_gains_the_chat_and_app_tables(tmp_path):
    with lodge_db.write(tmp_path) as conn:
        for table in ("chat_state", "pending_messages", "apps_running"):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("DELETE FROM meta WHERE key='migration:3'")

    with closing(lodge_db.connect(tmp_path)) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        applied = lodge_db.applied_migrations(conn)

    assert {"sessions", "chat_state", "pending_messages", "apps_running"} <= tables
    assert 3 in applied


# --- One-time import of the old files ---


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data))


def _pending_rows(wolts_dir):
    with closing(lodge_db.connect(wolts_dir, readonly=True)) as conn:
        return {r["id"]: dict(r) for r in conn.execute("SELECT * FROM pending_messages")}


def test_telegram_chat_files_are_imported_and_retired(tmp_path):
    chats = tmp_path / ".space" / "telegram" / "chats"
    _write_json(chats / "42.json", {"active_wolt": "pal", "active_session": "pal-a",
                                     "chat_id": 42, "updated_at": "2026-10-01T00:00:00"})
    _write_json(chats / "43.json", '{"active_wolt": ')

    ran = lodge_db.run_imports(tmp_path, {"chat": chat_state.import_chat})

    assert ran == {"chat": {"imported": 1, "skipped": 1}}
    assert chat_state.get(tmp_path, "telegram", 42) == {"active_wolt": "pal",
                                                        "active_session": "pal-a"}
    # Only what was read is retired; the unreadable file stays to be looked at.
    assert sorted(p.name for p in chats.iterdir()) == ["42.json.imported", "43.json"]


def test_a_chat_already_in_the_db_wins_over_a_stale_file(tmp_path):
    chat_state.put(tmp_path, "telegram", 42, {"active_wolt": "nunu"})
    _write_json(tmp_path / ".space" / "telegram" / "chats" / "42.json", {"active_wolt": "pal"})

    ran = lodge_db.run_imports(tmp_path, {"chat": chat_state.import_chat})

    assert ran == {"chat": {"imported": 0, "skipped": 1}}
    assert chat_state.get(tmp_path, "telegram", 42) == {"active_wolt": "nunu"}


def test_slack_state_files_are_imported_from_any_wolt_dir_and_retired(tmp_path):
    # Natively the adapter wrote under a phantom wolt named "wolts".
    slack = tmp_path / "wolts" / ".state" / "chat" / "slack"
    _write_json(slack / "_thread_sessions.json", {"D1:1000.1": {
        "session": "pal-a", "wolt": "pal", "creature": "beaver", "session_link": "https://x"}})
    _write_json(slack / "_owner_selections.json", {"U1": "pal"})
    _write_json(slack / "_pending_messages.json", {
        "D1:2000.1": {"user": "U1", "channel": "D1", "root_ts": "2000.1", "event_id": "Ev1",
                      "text": "task", "picker_ts": "3000.1", "state": "pending",
                      "created_at": 1.0, "expires_at": 601.0},
        "D1:1000.1": {"user": "U1", "channel": "D1", "root_ts": "1000.1", "event_id": "Ev0",
                      "picker_ts": "1500.1", "state": "consumed", "wolt": "pal",
                      "session": "pal-a", "created_at": 0.5, "expires_at": 600.5}})
    _write_json(slack / "_active_threads.json", [])
    (slack / "_pending_messages.lock").touch()
    (slack / "D1_1000_1.jsonl").write_text("{}\n")

    ran = lodge_db.run_imports(tmp_path, {"chat": chat_state.import_chat})

    assert ran == {"chat": {"imported": 4, "skipped": 0}}
    assert chat_state.get(tmp_path, "slack", "D1:1000.1") == {
        "active_wolt": "pal", "active_session": "pal-a",
        "creature": "beaver", "session_link": "https://x"}
    assert chat_state.get(tmp_path, "slack", "U1") == {"active_wolt": "pal"}
    pending = _pending_rows(tmp_path)
    assert (pending["D1:2000.1"]["text"], pending["D1:2000.1"]["picker_ts"]) == ("task", "3000.1")
    assert (pending["D1:1000.1"]["text"], pending["D1:1000.1"]["state"]) == (None, "consumed")
    assert sorted(p.name for p in slack.iterdir()) == [
        "D1_1000_1.jsonl", "_active_threads.json.imported",
        "_owner_selections.json.imported", "_pending_messages.json.imported",
        "_thread_sessions.json.imported"]


def test_a_malformed_slack_thread_entry_is_skipped_not_fatal(tmp_path):
    slack = tmp_path / "pal" / ".state" / "chat" / "slack"
    _write_json(slack / "_thread_sessions.json", {
        "D1:1.1": "not-a-dict",
        "D1:2.2": {"session": "pal-a", "wolt": "pal"}})

    ran = lodge_db.run_imports(tmp_path, {"chat": chat_state.import_chat})

    assert ran == {"chat": {"imported": 1, "skipped": 1}}
    assert chat_state.get(tmp_path, "slack", "D1:2.2")["active_session"] == "pal-a"


def test_nothing_to_import_records_nothing(tmp_path):
    assert lodge_db.run_imports(tmp_path, {"chat": chat_state.import_chat}) == {}


def test_the_boot_import_brings_in_chats_and_apps(tmp_path):
    sys.path.insert(0, str(ROOT / "src"))
    from woltspace import adoption

    _write_json(tmp_path / ".space" / "telegram" / "chats" / "42.json", {"active_wolt": "pal"})
    _write_json(tmp_path / ".space" / "apps" / "pal-app.json", {"name": "pal-app", "pid": 7})
    layout = type("Layout", (), {"wolts_dir": tmp_path})()

    assert adoption.import_lodge_state(layout) == {}

    assert chat_state.get(tmp_path, "telegram", 42) == {"active_wolt": "pal"}
    with closing(lodge_db.connect(tmp_path, readonly=True)) as conn:
        assert set(lodge_db.imports(conn)) == {"chat", "apps"}
