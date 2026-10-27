import errno
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError

import pytest
from scripts import startup_transport_probe as probe


@pytest.mark.parametrize("case", ["match", "api_missing", "wrong_destination", "wrong_session", "legacy_session", "wrong_executable", "blocked_health", "different_namespace", "send_timeout", "send_failure"])
def test_probe_emits_classes_and_only_sends_to_matching_private_endpoint(tmp_path, monkeypatch, capsys, case):
    expected = "http://127.0.0.1:45678"
    executable = Path(__file__).resolve().parents[1] / "container/bin/notify"
    config = tmp_path / "transport-probe-config.json"
    probe.write_json(config, {"endpoint": expected, "session": "synthetic-session",
                             "notify": str(executable), "net_namespace": "synthetic-host-ns"})
    monkeypatch.setenv("WOLTSPACE_API", expected)
    monkeypatch.setenv("WOLTSPACE_WOLT_SESSION", "synthetic-session")
    monkeypatch.setattr(probe, "net_namespace", lambda: "synthetic-host-ns" if case != "different_namespace" else "synthetic-tool-ns")
    monkeypatch.setattr(probe.shutil, "which", lambda _: str(executable) if case != "wrong_executable" else str(tmp_path / "other-notify"))
    checks = []
    monkeypatch.setattr(probe, "health", lambda endpoint: checks.append(endpoint) or ("blocked" if case == "blocked_health" else "ok"))
    sends = []
    monkeypatch.setattr(probe.subprocess, "run", lambda *a, **kw: sends.append((a, kw)) or SimpleNamespace(returncode=0))
    if case == "api_missing":
        monkeypatch.delenv("WOLTSPACE_API")
    if case == "wrong_session":
        monkeypatch.setenv("WOLTSPACE_WOLT_SESSION", "other-synthetic-session")
    if case == "legacy_session":
        monkeypatch.setenv("WOLTSPACE_WOLT_SESSION", "")
        monkeypatch.setenv("WOLT_SESSION", "synthetic-session")
    if case == "wrong_destination":
        monkeypatch.setenv("WOLTSPACE_API", "http://127.0.0.1:45679")
    if case in {"send_timeout", "send_failure"}:
        def fail(*a, **kw):
            sends.append((a, kw))
            if case == "send_timeout":
                raise subprocess.TimeoutExpired("synthetic-private-command", 10)
            return SimpleNamespace(returncode=7)
        monkeypatch.setattr(probe.subprocess, "run", fail)
    result = probe.run(config)
    data = json.loads(config.with_name("transport-probe-result.json").read_text())
    assert probe.valid_result(data)
    assert checks == [expected]
    should_send = case in {"match", "legacy_session", "different_namespace", "send_timeout", "send_failure"}
    assert bool(sends) == should_send
    assert result == (0 if should_send and case not in {"send_timeout", "send_failure"} else 2)
    assert data["destination"] == ("default_7777" if case == "api_missing" else "other" if case == "wrong_destination" else "expected")
    assert data["net_namespace"] == ("different" if case == "different_namespace" else "same")
    text = capsys.readouterr().out
    assert all(value not in text for value in (expected, "synthetic-session", "synthetic-host-ns", str(executable)))
    attempts = len(sends)
    assert probe.run(config) == 2
    assert len(sends) == attempts  # No retry, including ambiguous send timeout.
    assert "synthetic-private-command" not in capsys.readouterr().out


def test_health_is_body_free_and_classifies_network_denial(monkeypatch):
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self): raise AssertionError("response body must not be read")
    calls = []
    monkeypatch.setattr(probe, "urlopen", lambda url, **kw: calls.append(url) or Response())
    assert probe.health("http://127.0.0.1:45678") == "ok"
    assert calls == ["http://127.0.0.1:45678/health"]
    def denied(*_, **__): raise URLError(PermissionError(errno.EPERM, "synthetic private error"))
    monkeypatch.setattr(probe, "urlopen", denied)
    assert probe.health("http://127.0.0.1:45678") == "blocked"


def test_probe_refuses_non_test_default_port(tmp_path, capsys):
    executable = Path(__file__).resolve().parents[1] / "container/bin/notify"
    config = tmp_path / "transport-probe-config.json"
    probe.write_json(config, {"endpoint": "http://127.0.0.1:7777", "session": "synthetic",
                             "notify": str(executable), "net_namespace": "synthetic"})
    assert probe.run(config) == 2
    assert not config.with_name("transport-probe-result.json").exists()
    assert "no environment or exception detail" in capsys.readouterr().out
