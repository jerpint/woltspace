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


def test_host_claude_defaults_to_prompt_without_a_grant(tmp_path):
    target, wolts = _target(tmp_path)
    policy, grant = resolve_execution_policy(
        None, isolation="host", harness="claude",
        target=target, grants=AutoGrantStore(wolts)
    )
    assert policy == ExecutionPolicy(mode="prompt", isolation="host")
    assert grant is None


def test_host_codex_defaults_to_guarded_with_wolt_and_apps_roots(tmp_path):
    target, wolts = _target(tmp_path)
    (wolts / "apps").mkdir()
    (wolts / "otherwolt").mkdir()
    policy, grant = resolve_execution_policy(
        None, isolation="host", harness="codex",
        target=target, grants=AutoGrantStore(wolts)
    )
    assert policy.mode == "guarded"
    assert policy.network_access is True
    assert policy.approvals_reviewer == "auto_review"
    assert set(policy.writable_roots) == {
        str((wolts / "testwolt").resolve()),
        str((wolts / "apps").resolve()),
    }
    assert str((wolts / "otherwolt").resolve()) not in policy.writable_roots
    assert str((wolts / ".space").resolve()) not in policy.writable_roots
    assert grant is None


def test_guarded_policy_round_trips_its_codex_settings():
    policy = ExecutionPolicy(
        mode="guarded",
        isolation="host",
        writable_roots=("/wolts/apps", "/wolts/maple"),
        network_access=True,
        approvals_reviewer="auto_review",
    )
    assert ExecutionPolicy.from_record(policy.to_record()) == policy


def test_host_auto_grant_authorizes_but_does_not_select_full_auto(tmp_path):
    target, wolts = _target(tmp_path)
    store = AutoGrantStore(wolts)
    store.grant(target)

    policy, grant = resolve_execution_policy(
        None, isolation="host", harness="codex", target=target, grants=store
    )
    assert policy.mode == "guarded"
    assert grant is None

    policy, grant = resolve_execution_policy(
        "auto", isolation="host", harness="codex", target=target, grants=store
    )
    assert policy == ExecutionPolicy(mode="auto", isolation="host")
    assert grant is not None


def test_persistent_wolt_preference_selects_full_auto_without_a_launch_grant(tmp_path):
    target, wolts = _target(tmp_path)

    policy, grant = resolve_execution_policy(
        "auto",
        isolation="host",
        harness="codex",
        target=target,
        grants=AutoGrantStore(wolts),
        persistent=True,
    )

    assert policy == ExecutionPolicy(mode="auto", isolation="host")
    assert grant is None


def test_a_grant_elsewhere_does_not_relax_this_target(tmp_path):
    """The default is decided by the exact pair, like the enforcement is."""
    granted, wolts = _target(tmp_path, repo="repo-a")
    other, _ = _target(tmp_path, repo="repo-b")
    store = AutoGrantStore(wolts)
    store.grant(granted)

    policy, grant = resolve_execution_policy(
        None, isolation="host", harness="codex", target=other, grants=store
    )
    assert policy.mode == "guarded"
    assert grant is None


def test_explicit_prompt_is_honored_even_where_auto_is_approved(tmp_path):
    """Asking to be asked always wins; the record says what actually ran."""
    target, wolts = _target(tmp_path)
    store = AutoGrantStore(wolts)
    store.grant(target)

    policy, grant = resolve_execution_policy(
        "prompt", isolation="host", harness="codex", target=target, grants=store
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


def test_guarded_is_rejected_for_non_codex_harnesses(tmp_path):
    target, wolts = _target(tmp_path)
    with pytest.raises(ValueError, match="only by the codex"):
        resolve_execution_policy(
            "guarded", isolation="host", harness="claude",
            target=target, grants=AutoGrantStore(wolts),
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


def test_old_implicit_auto_grants_are_inactive_and_replaced(tmp_path):
    target, wolts = _target(tmp_path)
    store = AutoGrantStore(wolts)
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps({"grants": [{
        "wolt_id": target.wolt_id,
        "canonical_workdir": str(target.canonical_workdir),
        "policy_version": 1,
        "approved_at": 1,
    }]}) + "\n")

    assert store.find(target) is None
    assert store.list() == []
    fresh = store.grant(target)
    assert fresh.policy_version == POLICY_VERSION
    payload = json.loads(store.path.read_text())
    assert len(payload["grants"]) == 1
    assert payload["grants"][0]["policy_version"] == POLICY_VERSION


def test_start_session_native_policy_and_grant_are_persisted(
    tmp_path, monkeypatch, fake_runtime
):
    import paths
    import sessions

    wolts = tmp_path / "wolts"
    home = wolts / "testwolt"
    (home / "wolt" / "site").mkdir(parents=True)
    (home / "wolt" / "wolt.json").write_text(
        json.dumps({"name": "testwolt", "type": "raccoon", "harness": "codex"})
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(sessions, "WOLTS_DIR", wolts)
    monkeypatch.setattr(paths, "WOLTS_DIR", wolts)
    monkeypatch.setenv("WOLTSPACE_ISOLATION", "host")

    default = sessions.start_session(wolt="testwolt", workdir=repo)
    assert default["execution_policy"]["mode"] == "guarded"
    assert default["execution_policy"]["network_access"] is True
    assert str(home.resolve()) in default["execution_policy"]["writable_roots"]
    assert str((wolts / "apps").resolve()) in default["execution_policy"]["writable_roots"]
    default_cmd = sessions.prepare_session_command(default["name"], "spawn")
    assert "--sandbox workspace-write" in default_cmd
    assert "--ask-for-approval on-request" in default_cmd
    assert "approvals_reviewer=auto_review" in default_cmd
    assert "--dangerously-bypass-approvals-and-sandbox" not in default_cmd

    target = SessionTarget.resolve("testwolt", repo, wolts_dir=wolts)
    AutoGrantStore(wolts).grant(target)
    automated = sessions.start_session(
        wolt="testwolt", workdir=repo, execution_policy="auto"
    )
    assert automated["execution_policy"]["mode"] == "auto"
    assert automated["auto_grant"]["canonical_workdir"] == str(repo.resolve())
    auto_cmd = sessions.prepare_session_command(automated["name"], "spawn")
    assert "--dangerously-bypass-approvals-and-sandbox" in auto_cmd

    # A standing grant authorizes Full Auto but never silently selects it.
    implicit = sessions.start_session(wolt="testwolt", workdir=repo)
    assert implicit["execution_policy"]["mode"] == "guarded"
    assert implicit["auto_grant"] is None
    assert "--sandbox workspace-write" in sessions.prepare_session_command(
        implicit["name"], "spawn")


def test_start_session_honors_persistent_wolt_json_policy(
    tmp_path, monkeypatch, fake_runtime
):
    import paths
    import sessions

    wolts = tmp_path / "wolts"
    home = wolts / "testwolt"
    (home / "wolt" / "site").mkdir(parents=True)
    (home / "wolt" / "wolt.json").write_text(json.dumps({
        "name": "testwolt",
        "type": "raccoon",
        "harness": "codex",
        "execution_policy": "auto",
    }))
    monkeypatch.setattr(sessions, "WOLTS_DIR", wolts)
    monkeypatch.setattr(paths, "WOLTS_DIR", wolts)
    monkeypatch.setenv("WOLTSPACE_ISOLATION", "host")

    persistent = sessions.start_session(wolt="testwolt")
    assert persistent["execution_policy"]["mode"] == "auto"
    assert persistent["auto_grant"] is None
    assert "--dangerously-bypass-approvals-and-sandbox" in (
        sessions.prepare_session_command(persistent["name"], "spawn")
    )

    overridden = sessions.start_session(
        wolt="testwolt", execution_policy="prompt"
    )
    assert overridden["execution_policy"]["mode"] == "prompt"


def test_start_session_rejects_invalid_wolt_json_policy(
    tmp_path, monkeypatch, fake_runtime
):
    import paths
    import sessions

    wolts = tmp_path / "wolts"
    home = wolts / "testwolt"
    (home / "wolt" / "site").mkdir(parents=True)
    (home / "wolt" / "wolt.json").write_text(json.dumps({
        "name": "testwolt",
        "type": "raccoon",
        "harness": "codex",
        "execution_policy": "reckless-ish",
    }))
    monkeypatch.setattr(sessions, "WOLTS_DIR", wolts)
    monkeypatch.setattr(paths, "WOLTS_DIR", wolts)
    monkeypatch.setenv("WOLTSPACE_ISOLATION", "host")

    with pytest.raises(ValueError, match="unknown execution policy in"):
        sessions.start_session(wolt="testwolt")
