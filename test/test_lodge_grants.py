"""Auto and dig grants live in the lodge DB's `grants` table, and the old JSON
files come in once through `import_grants`."""

import json
import sys
import threading
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "container" / "lib"))

import lodge_db  # noqa: E402
from execution_policy import AutoGrantStore, POLICY_VERSION, import_grants  # noqa: E402
from session_targets import SessionTarget  # noqa: E402
from woltspace.dig import DigError, DigStore, ResolvedSSH  # noqa: E402


def _target(wolts, wolt, workdir):
    (wolts / wolt).mkdir(parents=True, exist_ok=True)
    workdir.mkdir(parents=True, exist_ok=True)
    return SessionTarget.resolve(wolt, workdir, wolts_dir=wolts)


def _rows(wolts):
    with closing(lodge_db.connect(wolts)) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT kind, wolt_id, scope, policy_version FROM grants ORDER BY kind, wolt_id, scope"
        )]


def _dig(store, name="new-colony", wolt="pal"):
    return store.grant(name=name, wolt=wolt, destination="my-colony",
                       resolved=ResolvedSSH("server.example", "colony", 22), now=100)


def test_an_auto_grant_is_a_row_and_no_file(tmp_path):
    target = _target(tmp_path, "pal", tmp_path / "repo")
    AutoGrantStore(tmp_path).grant(target)
    assert _rows(tmp_path) == [{"kind": "auto", "wolt_id": "pal",
                                "scope": str(target.canonical_workdir),
                                "policy_version": POLICY_VERSION}]
    assert lodge_db.path(tmp_path).stat().st_mode & 0o777 == 0o600  # consents stay private
    assert not (tmp_path / ".space" / "auto-grants.json").exists()
    assert not (tmp_path / ".space" / "auto-grants.lock").exists()


def test_granting_twice_keeps_one_row(tmp_path):
    target = _target(tmp_path, "pal", tmp_path / "repo")
    store = AutoGrantStore(tmp_path)
    store.grant(target)
    store.grant(target)
    assert len(store.list()) == 1


def test_a_grant_from_an_older_policy_does_not_count(tmp_path):
    target = _target(tmp_path, "pal", tmp_path / "repo")
    with lodge_db.write(tmp_path) as conn:
        conn.execute(
            "INSERT INTO grants(kind, wolt_id, scope, policy_version, approved_at)"
            " VALUES ('auto', 'pal', ?, ?, 1)",
            (str(target.canonical_workdir), POLICY_VERSION - 1),
        )
    assert AutoGrantStore(tmp_path).find(target) is None


def test_parallel_grants_for_different_targets_all_survive(tmp_path):
    """Two writers no longer read-modify-write one file: none erases another."""
    targets = [_target(tmp_path, f"wolt-{i}", tmp_path / f"repo-{i}") for i in range(8)]
    threads = [threading.Thread(target=AutoGrantStore(tmp_path).grant, args=(t,))
               for t in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(AutoGrantStore(tmp_path).list()) == 8


def test_reading_grants_of_a_lodge_without_a_db_creates_nothing(tmp_path):
    target = _target(tmp_path, "pal", tmp_path / "repo")
    store = AutoGrantStore(tmp_path)
    assert store.list() == [] and store.find(target) is None
    assert not lodge_db.path(tmp_path).exists()


def test_revoke_wolt_leaves_dig_grants_alone(tmp_path):
    AutoGrantStore(tmp_path).grant(_target(tmp_path, "pal", tmp_path / "repo"))
    _dig(DigStore(tmp_path / ".space"))
    assert AutoGrantStore(tmp_path).revoke_wolt("pal") == 1
    assert [r["kind"] for r in _rows(tmp_path)] == ["dig"]


def test_a_dig_grant_is_a_row_keyed_by_its_name(tmp_path):
    record = _dig(DigStore(tmp_path / ".space"))
    assert _rows(tmp_path) == [{"kind": "dig", "wolt_id": "pal",
                                "scope": "new-colony", "policy_version": 0}]
    assert DigStore(tmp_path / ".space").get("new-colony") == record
    assert not (tmp_path / ".space" / "digs").exists()


def test_a_dig_name_is_taken_for_every_wolt(tmp_path):
    store = DigStore(tmp_path / ".space")
    _dig(store, wolt="pal")
    with pytest.raises(DigError, match="already exists"):
        _dig(store, wolt="nunu")


def test_dig_audit_counts_every_connection(tmp_path):
    store = DigStore(tmp_path / ".space")
    _dig(store)
    store.record_connection("new-colony", exit_code=0, now=200)
    store.record_connection("new-colony", exit_code=3, now=300)
    record = store.get("new-colony")
    assert (record["connect_count"], record["last_connected_at"], record["last_exit_code"]) == (2, 300, 3)
    assert [r["name"] for r in store.list()] == ["new-colony"]


def test_audit_of_an_unknown_dig_is_refused(tmp_path):
    with pytest.raises(DigError, match="unknown dig"):
        DigStore(tmp_path / ".space").record_connection("ghost", exit_code=0)


# --- import ---------------------------------------------------------------

def _old_files(wolts, workdir):
    space = wolts / ".space"
    (space / "digs").mkdir(parents=True)
    (space / "auto-grants.json").write_text(json.dumps({"grants": [
        {"wolt_id": "pal", "canonical_workdir": str(workdir),
         "policy_version": POLICY_VERSION, "approved_at": 7},
        "not a grant",
    ]}))
    (space / "auto-grants.lock").write_text("")
    (space / "digs" / "grants.json").write_text(json.dumps({
        "version": "woltspace.digs/v0",
        "grants": [{"name": "new-colony", "wolt": "nunu", "destination": "my-colony",
                    "resolved": {"hostname": "h", "user": "u", "port": 22},
                    "bootstrap_dir": ".woltspace/bootstrap", "created_at": 9,
                    "connect_count": 4, "last_connected_at": 10, "last_exit_code": 0}],
    }))
    (space / "digs" / "grants.lock").write_text("")


def test_import_takes_both_files_and_names_them_for_retiring(tmp_path):
    target = _target(tmp_path, "pal", tmp_path / "repo")
    _old_files(tmp_path, target.canonical_workdir)

    with lodge_db.write(tmp_path) as conn:
        counts, retire = import_grants(conn, tmp_path)

    space = tmp_path / ".space"
    assert counts == {"auto": 1, "dig": 1, "skipped": 0}
    assert retire == [space / "auto-grants.json", space / "digs" / "grants.json"]
    grant = AutoGrantStore(tmp_path).find(target)
    assert grant is not None and grant.approved_at == 7
    assert DigStore(space).get("new-colony")["connect_count"] == 4


def test_import_of_a_lodge_with_no_grant_files_is_empty(tmp_path):
    with lodge_db.write(tmp_path) as conn:
        assert import_grants(conn, tmp_path) == ({"auto": 0, "dig": 0, "skipped": 0}, [])


def test_an_unreadable_grant_file_is_not_retired(tmp_path, capsys):
    space = tmp_path / ".space"
    space.mkdir()
    (space / "auto-grants.json").write_text("{torn")
    with lodge_db.write(tmp_path) as conn:
        assert import_grants(conn, tmp_path) == ({"auto": 0, "dig": 0, "skipped": 0}, [])
    assert "auto-grants.json" in capsys.readouterr().err


def test_run_imports_retires_both_files_and_their_locks(tmp_path):
    target = _target(tmp_path, "pal", tmp_path / "repo")
    _old_files(tmp_path, target.canonical_workdir)
    lodge_db.run_imports(tmp_path, {"grants": import_grants})
    space = tmp_path / ".space"
    assert sorted(p.name for p in space.iterdir() if p.is_file() and "lodge.db" not in p.name) == [
        "auto-grants.json.imported"]
    assert sorted(p.name for p in (space / "digs").iterdir()) == ["grants.json.imported"]


def test_a_file_put_back_after_a_downgrade_never_beats_a_live_row(tmp_path):
    target = _target(tmp_path, "pal", tmp_path / "repo")
    AutoGrantStore(tmp_path).grant(target)
    DigStore(tmp_path / ".space").grant(name="new-colony", wolt="pal", destination="my-colony",
                                        resolved=ResolvedSSH("h", "u", 22), now=500)
    _old_files(tmp_path, target.canonical_workdir)
    with lodge_db.write(tmp_path) as conn:
        counts, _ = import_grants(conn, tmp_path)
    assert counts == {"auto": 0, "dig": 0, "skipped": 2}
    assert AutoGrantStore(tmp_path).find(target).approved_at > 7
    assert DigStore(tmp_path / ".space").get("new-colony")["wolt"] == "pal"


def test_a_dig_imported_without_a_count_still_counts_its_connections(tmp_path):
    with lodge_db.write(tmp_path) as conn:
        conn.execute("INSERT INTO grants(kind, wolt_id, scope, approved_at, record)"
                     " VALUES ('dig', 'pal', 'new-colony', 1, ?)",
                     (json.dumps({"name": "new-colony", "wolt": "pal"}),))
    store = DigStore(tmp_path / ".space")
    store.record_connection("new-colony", exit_code=0, now=5)
    assert store.get("new-colony")["connect_count"] == 1


def test_after_the_boot_import_an_old_grant_is_found(tmp_path):
    """Upgrade through the real boot entry point: auto-grants.json is in the DB."""
    from woltspace.adoption import adopt_runtime_sessions
    from woltspace.layout import RuntimeLayout

    wolts = tmp_path / "wolts"
    target = _target(wolts, "pal", tmp_path / "repo")
    _old_files(wolts, target.canonical_workdir)

    adopt_runtime_sessions(RuntimeLayout(wolts_dir=wolts, install_root=ROOT))

    assert AutoGrantStore(wolts).find(target) is not None
    assert DigStore(wolts / ".space").get("new-colony")["wolt"] == "nunu"
