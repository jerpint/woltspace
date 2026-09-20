import json
import subprocess

import pytest

from woltspace.dig import DigError, DigStore, ResolvedSSH, connect, resolve_ssh


def resolved(host="server.example", user="colony", port=22):
    return ResolvedSSH(host, user, port)


def grant(store):
    return store.grant(
        name="new-colony",
        wolt="n00b",
        destination="my-colony",
        resolved=resolved(),
        now=100,
    )


def test_resolve_ssh_uses_config_without_a_shell():
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(
            argv, 0, "hostname server.example\nuser colony\nport 2222\n", ""
        )

    assert resolve_ssh("my-colony", runner=runner) == resolved(port=2222)
    assert calls[0][0] == ["ssh", "-G", "--", "my-colony"]


@pytest.mark.parametrize("destination", ["-oProxyCommand=oops", "host name", "host;id", ""])
def test_destination_cannot_inject_ssh_options(destination):
    with pytest.raises(DigError):
        resolve_ssh(destination, runner=lambda *a, **k: None)


def test_grant_store_is_private_and_revoke_is_honest(tmp_path):
    store = DigStore(tmp_path / ".space")
    record = grant(store)
    assert record["bootstrap_dir"] == ".woltspace/bootstrap"
    assert store.root.stat().st_mode & 0o777 == 0o700
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.revoke("new-colony") is True
    assert store.revoke("new-colony") is False
    with pytest.raises(DigError, match="unknown dig"):
        store.get("new-colony")


def test_connect_rechecks_resolution_uses_strict_host_key_and_audits(tmp_path):
    store = DigStore(tmp_path / ".space")
    grant(store)
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 7)

    code = connect(
        store,
        "new-colony",
        command=("woltspace", "status", "--json"),
        resolver=lambda _: resolved(),
        runner=runner,
    )
    assert code == 7
    assert calls == [([
        "ssh", "-o", "StrictHostKeyChecking=yes", "--", "my-colony",
        "woltspace", "status", "--json",
    ], {"check": False})]
    audited = store.get("new-colony")
    assert audited["connect_count"] == 1
    assert audited["last_exit_code"] == 7


def test_connect_refuses_changed_ssh_resolution_before_running(tmp_path):
    store = DigStore(tmp_path / ".space")
    grant(store)
    ran = False

    def runner(*args, **kwargs):
        nonlocal ran
        ran = True

    with pytest.raises(DigError, match="no longer resolves"):
        connect(store, "new-colony", resolver=lambda _: resolved(host="evil.example"), runner=runner)
    assert ran is False
    assert store.get("new-colony")["connect_count"] == 0


def test_session_wolt_cannot_use_another_wolts_dig(tmp_path):
    store = DigStore(tmp_path / ".space")
    grant(store)
    with pytest.raises(DigError, match="belongs to wolt n00b"):
        connect(
            store,
            "new-colony",
            actor_wolt="someone-else",
            resolver=lambda _: resolved(),
            runner=lambda *a, **k: None,
        )


def test_interrupted_connection_is_audited(tmp_path):
    store = DigStore(tmp_path / ".space")
    grant(store)

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        connect(store, "new-colony", resolver=lambda _: resolved(), runner=interrupted)
    audit = store.get("new-colony")
    assert audit["connect_count"] == 1
    assert audit["last_exit_code"] == 130


def test_bootstrap_path_cannot_escape_remote_home(tmp_path):
    store = DigStore(tmp_path / ".space")
    with pytest.raises(DigError, match="relative"):
        store.grant(
            name="bad",
            wolt="n00b",
            destination="host",
            resolved=resolved(),
            bootstrap_dir="../../etc",
        )


def test_store_contains_no_credentials_or_command_bodies(tmp_path):
    store = DigStore(tmp_path / ".space")
    grant(store)
    payload = json.loads(store.path.read_text())
    text = json.dumps(payload)
    assert "PRIVATE KEY" not in text
    assert "remote_command" not in text
    assert payload["version"] == "woltspace.digs/v0"
