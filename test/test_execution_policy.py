"""Host permission defaults and exact-target Auto grants."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))

from execution_policy import (
    AutoGrantStore,
    ExecutionPolicy,
    POLICY_VERSION,
    RECORD_VERSION,
    resolve_execution_policy,
)
from session_targets import SessionTarget


def _target(tmp_path, wolt="testwolt", repo="repo"):
    wolts = tmp_path / "wolts"
    (wolts / wolt).mkdir(parents=True, exist_ok=True)
    workdir = tmp_path / repo
    workdir.mkdir(exist_ok=True)
    return SessionTarget.resolve(wolt, workdir, wolts_dir=wolts), wolts


def test_external_isolation_defaults_to_bypass(tmp_path):
    target, wolts = _target(tmp_path)
    policy, grant = resolve_execution_policy(
        None, isolation="external", target=target, grants=AutoGrantStore(wolts)
    )
    assert policy == ExecutionPolicy(mode="bypass", isolation="external")
    assert grant is None


def test_host_defaults_to_prompt_without_a_grant(tmp_path):
    target, wolts = _target(tmp_path)
    policy, grant = resolve_execution_policy(
        None, isolation="host", target=target, grants=AutoGrantStore(wolts)
    )
    assert policy == ExecutionPolicy(mode="prompt", isolation="host")
    assert grant is None


def test_host_defaults_to_bypass_where_the_grant_already_says_so(tmp_path):
    """The grant *is* the consent, so it decides the default too.

    Nothing on the spawn path asks for Auto by name — not the bot, not the
    lodge — so a host default of prompt regardless of the grant meant every
    unattended native session sat waiting for a human who was not there.
    """
    target, wolts = _target(tmp_path)
    store = AutoGrantStore(wolts)
    store.grant(target)

    policy, grant = resolve_execution_policy(
        None, isolation="host", target=target, grants=store
    )
    assert policy == ExecutionPolicy(mode="bypass", isolation="host")
    assert grant is not None
    assert grant.canonical_workdir == target.canonical_workdir


def test_a_grant_elsewhere_does_not_relax_this_target(tmp_path):
    """The default is decided by the exact pair, like the enforcement is."""
    granted, wolts = _target(tmp_path, repo="repo-a")
    other, _ = _target(tmp_path, repo="repo-b")
    store = AutoGrantStore(wolts)
    store.grant(granted)

    policy, grant = resolve_execution_policy(
        None, isolation="host", target=other, grants=store
    )
    assert policy.mode == "prompt"
    assert grant is None


def test_explicit_prompt_is_honored_even_where_auto_is_approved(tmp_path):
    """Asking to be asked always wins; the record says what actually ran."""
    target, wolts = _target(tmp_path)
    store = AutoGrantStore(wolts)
    store.grant(target)

    policy, grant = resolve_execution_policy(
        "prompt", isolation="host", target=target, grants=store
    )
    assert policy == ExecutionPolicy(mode="prompt", isolation="host")
    assert grant is None


def test_external_isolation_ignores_grants_entirely(tmp_path):
    """A container's default was never the host's, and still is not."""
    target, wolts = _target(tmp_path)
    store = AutoGrantStore(wolts)

    policy, grant = resolve_execution_policy(
        None, isolation="external", target=target, grants=store
    )
    assert policy == ExecutionPolicy(mode="bypass", isolation="external")
    assert grant is None

    store.grant(target)
    policy, grant = resolve_execution_policy(
        None, isolation="external", target=target, grants=store
    )
    assert policy == ExecutionPolicy(mode="bypass", isolation="external")
    assert grant is None


@pytest.mark.parametrize("mode", ["auto", "bypass"])
def test_one_host_grant_allows_auto_and_bypass_for_the_exact_wolt_and_path(tmp_path, mode):
    target_a, wolts = _target(tmp_path, repo="repo-a")
    target_b, _ = _target(tmp_path, repo="repo-b")
    store = AutoGrantStore(wolts)
    store.grant(target_a)

    policy, grant = resolve_execution_policy(
        mode, isolation="host", target=target_a, grants=store
    )
    assert policy == ExecutionPolicy(mode=mode, isolation="host")
    assert grant is not None

    with pytest.raises(PermissionError, match="Auto and Bypass are not approved .*repo-b"):
        resolve_execution_policy(
            mode, isolation="host", target=target_b, grants=store
        )


# Version 1 decoding is checked against migration 6 on the same inputs (test_lodge_db).
@pytest.mark.parametrize("mode", ["prompt", "auto", "bypass"])
def test_a_version_2_policy_reads_as_written(mode):
    stored = {"mode": mode, "isolation": "host", "policy_version": 2}
    assert ExecutionPolicy.from_record(stored).to_record() == stored
    assert RECORD_VERSION == 2


def test_an_unknown_record_version_is_refused_not_guessed():
    with pytest.raises(ValueError, match="unknown execution policy version: 3"):
        ExecutionPolicy.from_record({"mode": "auto", "isolation": "host", "policy_version": 3})


def test_the_grant_keeps_its_own_version():
    assert POLICY_VERSION == 1


def test_grant_for_same_path_does_not_cross_wolts(tmp_path):
    target_a, wolts = _target(tmp_path, wolt="alpha")
    target_b, _ = _target(tmp_path, wolt="beta")
    store = AutoGrantStore(wolts)
    store.grant(target_a)
    assert store.find(target_a) is not None
    assert store.find(target_b) is None


def test_grant_store_is_versioned_and_revocable(tmp_path):
    target, wolts = _target(tmp_path)
    store = AutoGrantStore(wolts)
    grant = store.grant(target)

    assert store.find(target).policy_version == POLICY_VERSION
    assert grant.approved_at > 0
    assert store.revoke(target) is True
    assert store.find(target) is None
    assert store.revoke(target) is False


def test_start_session_native_policy_and_grant_are_persisted(
    tmp_path, monkeypatch, fake_runtime
):
    import paths
    import sessions

    wolts = tmp_path / "wolts"
    home = wolts / "testwolt"
    (home / "wolt" / "site").mkdir(parents=True)
    (home / "wolt" / "wolt.json").write_text(
        json.dumps({"name": "testwolt", "type": "raccoon"})
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(sessions, "WOLTS_DIR", wolts)
    monkeypatch.setattr(paths, "WOLTS_DIR", wolts)
    monkeypatch.setenv("WOLTSPACE_ISOLATION", "host")

    default = sessions.start_session(wolt="testwolt", workdir=repo)
    assert default["execution_policy"]["mode"] == "prompt"
    default_cmd = sessions.prepare_session_command(default["name"], "spawn")
    assert "--dangerously-skip-permissions" not in default_cmd

    target = SessionTarget.resolve("testwolt", repo, wolts_dir=wolts)
    AutoGrantStore(wolts).grant(target)
    automated = sessions.start_session(
        wolt="testwolt", workdir=repo, execution_policy="auto"
    )
    assert automated["execution_policy"]["mode"] == "auto"
    assert automated["auto_grant"]["canonical_workdir"] == str(repo.resolve())
    auto_cmd = sessions.prepare_session_command(automated["name"], "spawn")
    assert "--permission-mode auto" in auto_cmd
    assert "--dangerously-skip-permissions" not in auto_cmd

    # And the caller that asks for nothing — every real spawn path — gets
    # Bypass, as before, because the grant is already there to say so.
    implicit = sessions.start_session(wolt="testwolt", workdir=repo)
    assert implicit["execution_policy"]["mode"] == "bypass"
    assert implicit["auto_grant"]["canonical_workdir"] == str(repo.resolve())
    assert "--dangerously-skip-permissions" in sessions.prepare_session_command(
        implicit["name"], "spawn"
    )


def _host_wolt(tmp_path, monkeypatch):
    import paths
    import sessions

    wolts = tmp_path / "wolts"
    home = wolts / "testwolt"
    (home / "wolt" / "site").mkdir(parents=True)
    (home / "wolt" / "wolt.json").write_text(
        json.dumps({"name": "testwolt", "type": "raccoon", "harness": "claude"})
    )
    monkeypatch.setattr(sessions, "WOLTS_DIR", wolts)
    monkeypatch.setattr(paths, "WOLTS_DIR", wolts)
    return wolts


def test_an_old_session_file_imported_after_the_migration_resumes_as_bypass(
    tmp_path, monkeypatch, fake_runtime
):
    import lodge_db
    import sessions

    wolts = _host_wolt(tmp_path, monkeypatch)
    lodge_db.connect(wolts).close()  # migration 6 has run; the file arrives after it
    folder = wolts / "testwolt" / ".state" / "sessions"
    folder.mkdir(parents=True)
    (folder / "testwolt-old.json").write_text(json.dumps({
        "name": "testwolt-old", "wolt": "testwolt", "harness": "claude",
        "status": "stopped", "created_at": 1,
        "harness_session_id": "00000000-0000-4000-8000-000000000001",
        "execution_policy": {"mode": "auto", "isolation": "external", "policy_version": 1},
    }))
    lodge_db.run_imports(wolts, {"sessions": sessions.import_sessions})
    command = sessions.prepare_session_command("testwolt-old", "resume")
    assert "--dangerously-skip-permissions" in command
    assert "--resume 00000000-0000-4000-8000-000000000001" in command
