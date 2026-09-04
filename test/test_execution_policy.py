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
