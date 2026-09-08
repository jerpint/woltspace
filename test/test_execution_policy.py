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
    resolve_execution_policy,
)
from session_targets import SessionTarget


def _target(tmp_path, wolt="testwolt", repo="repo"):
    wolts = tmp_path / "wolts"
    (wolts / wolt).mkdir(parents=True, exist_ok=True)
    workdir = tmp_path / repo
    workdir.mkdir(exist_ok=True)
    return SessionTarget.resolve(wolt, workdir, wolts_dir=wolts), wolts


def test_external_isolation_defaults_to_auto(tmp_path):
    target, wolts = _target(tmp_path)
    policy, grant = resolve_execution_policy(
        None, isolation="external", target=target, grants=AutoGrantStore(wolts)
    )
    assert policy == ExecutionPolicy(mode="auto", isolation="external")
    assert grant is None


def test_host_defaults_to_prompt_without_a_grant(tmp_path):
    target, wolts = _target(tmp_path)
    policy, grant = resolve_execution_policy(
        None, isolation="host", target=target, grants=AutoGrantStore(wolts)
    )
    assert policy == ExecutionPolicy(mode="prompt", isolation="host")
    assert grant is None


def test_host_defaults_to_auto_where_the_grant_already_says_so(tmp_path):
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
    assert policy == ExecutionPolicy(mode="auto", isolation="host")
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
    assert policy == ExecutionPolicy(mode="auto", isolation="external")
    assert grant is None

    store.grant(target)
    policy, grant = resolve_execution_policy(
        None, isolation="external", target=target, grants=store
    )
    assert policy == ExecutionPolicy(mode="auto", isolation="external")
    assert grant is None


def test_host_auto_requires_exact_wolt_and_path(tmp_path):
    target_a, wolts = _target(tmp_path, repo="repo-a")
    target_b, _ = _target(tmp_path, repo="repo-b")
    store = AutoGrantStore(wolts)
    store.grant(target_a)

    policy, grant = resolve_execution_policy(
        "auto", isolation="host", target=target_a, grants=store
    )
    assert policy.mode == "auto"
    assert grant is not None

    with pytest.raises(PermissionError, match="repo-b"):
        resolve_execution_policy(
            "auto", isolation="host", target=target_b, grants=store
        )


def test_grant_for_same_path_does_not_cross_wolts(tmp_path):
    target_a, wolts = _target(tmp_path, wolt="alpha")
    target_b, _ = _target(tmp_path, wolt="beta")
    store = AutoGrantStore(wolts)
    store.grant(target_a)
    assert store.find(target_a) is not None
    assert store.find(target_b) is None


def test_grant_store_is_versioned_private_and_revocable(tmp_path):
    target, wolts = _target(tmp_path)
    store = AutoGrantStore(wolts)
    grant = store.grant(target)

    payload = json.loads(store.path.read_text())
    assert payload["grants"][0]["policy_version"] == POLICY_VERSION
    assert store.path.stat().st_mode & 0o777 == 0o600
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
    assert "--dangerously-skip-permissions" in auto_cmd

    # And the caller that asks for nothing — every real spawn path — now gets
    # the same Auto, because the grant is already there to say so.
    implicit = sessions.start_session(wolt="testwolt", workdir=repo)
    assert implicit["execution_policy"]["mode"] == "auto"
    assert implicit["auto_grant"]["canonical_workdir"] == str(repo.resolve())
    assert "--dangerously-skip-permissions" in sessions.prepare_session_command(
        implicit["name"], "spawn"
    )
