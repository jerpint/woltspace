import argparse
import json
import subprocess

import pytest

from woltspace import update as updater


def completed(argv, code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(argv, code, stdout, stderr)


def metadata(*versions, yanked=()):
    return {"releases": {v: [{"yanked": v in yanked}] for v in versions}}


@pytest.fixture
def native(monkeypatch):
    monkeypatch.delenv("WOLTSPACE_CONTAINER", raising=False)
    monkeypatch.setenv("WOLTSPACE_ISOLATION", "host")
    monkeypatch.setattr(updater, "__version__", "0.5.11")
    monkeypatch.setattr(updater.shutil, "which", lambda name: "/tools/woltspace")


def args(**overrides):
    values = {"check": False, "version": "", "pre": False, "yes": True, "json": True}
    values.update(overrides)
    return argparse.Namespace(**values)


def receipt(argv):
    assert argv == ["uv", "tool", "list", "--show-version-specifiers"]
    return completed(argv, stdout="woltspace v0.5.11 [required: woltspace==0.5.11]\n- woltspace\n")


@pytest.mark.parametrize(
    ("published", "expected", "available"),
    [(("0.5.10", "0.5.11", "0.5.12"), "0.5.12", True), (("0.5.10", "0.5.11"), "0.5.11", False)],
)
def test_check_only_and_up_to_date(native, monkeypatch, capsys, published, expected, available):
    calls = []
    monkeypatch.setattr(updater, "_pypi", lambda: metadata(*published))
    monkeypatch.setattr(updater, "_run", lambda argv: calls.append(argv) or receipt(argv))
    assert updater.update(args(check=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert (payload["target"], payload["update_available"]) == (expected, available)
    assert calls == [["uv", "tool", "list", "--show-version-specifiers"]]


def test_exact_version(native, monkeypatch, capsys):
    monkeypatch.setattr(updater, "_pypi", lambda: metadata("0.5.11", "0.5.13"))
    monkeypatch.setattr(updater, "_run", receipt)
    assert updater.update(args(check=True, version="0.5.13")) == 0
    assert json.loads(capsys.readouterr().out)["target"] == "0.5.13"


def test_pre_release_selected_and_installed_with_uv_flag(native, monkeypatch):
    calls = []
    statuses = iter([{"state": "stopped"}, {"state": "stopped"}])

    def run(argv):
        calls.append(argv)
        if argv[0] == "uv" and argv[1:3] == ["tool", "list"]: return receipt(argv)
        if argv[-2:] == ["status", "--json"]: return completed(argv, stdout=json.dumps(next(statuses)))
        if argv[-1] == "--version": return completed(argv, stdout="woltspace 0.5.12rc1\n")
        return completed(argv)

    monkeypatch.setattr(updater, "_pypi", lambda: metadata("0.5.11", "0.5.12rc1"))
    monkeypatch.setattr(updater, "_run", run)
    assert updater.update(args(pre=True)) == 0
    assert ["uv", "tool", "install", "--force", "woltspace==0.5.12rc1", "--prerelease", "allow"] in calls


@pytest.mark.parametrize(("arguments", "published", "error"), [({"version": "0.5.10", "check": True}, ("0.5.10",), "downgrade"), ({"version": "0.5.12", "check": True}, ("0.5.12",), "yanked")])
def test_downgrade_and_yanked_refused(native, monkeypatch, capsys, arguments, published, error):
    monkeypatch.setattr(updater, "_pypi", lambda: metadata(*published, yanked=published if error == "yanked" else ()))
    monkeypatch.setattr(updater, "_run", receipt)
    assert updater.update(args(**arguments)) == 1
    assert error in json.loads(capsys.readouterr().out)["error"]


def test_non_uv_install_refused(native, monkeypatch, capsys):
    monkeypatch.setattr(updater, "_run", lambda argv: completed(argv, stdout="ruff v0.8.0\n"))
    assert updater.update(args(check=True)) == 1
    assert "not a uv-managed" in json.loads(capsys.readouterr().out)["error"]


def test_custom_uv_source_refused(native, monkeypatch, capsys):
    monkeypatch.setattr(updater, "_run", lambda argv: completed(argv, stdout="woltspace v0.5.11 [required: woltspace @ git+https://example.test/repo]\n"))
    assert updater.update(args(check=True)) == 1
    assert "custom source" in json.loads(capsys.readouterr().out)["error"]


def test_container_refused(monkeypatch, capsys):
    monkeypatch.setenv("WOLTSPACE_ISOLATION", "external")
    assert updater.update(args(check=True)) == 1
    assert "host tooling" in json.loads(capsys.readouterr().out)["error"]


def test_false_container_flag_does_not_block_native(native, monkeypatch):
    monkeypatch.setenv("WOLTSPACE_CONTAINER", "false")
    monkeypatch.setattr(updater, "_pypi", lambda: metadata("0.5.11"))
    monkeypatch.setattr(updater, "_run", receipt)
    assert updater.update(args(check=True)) == 0


def lifecycle_run(calls, *, fail_install=False, initially_running=True):
    statuses = iter(([{"state": "healthy", "connectors": []}, {"state": "stopped"}, {"state": "healthy", "connectors": []}] if initially_running else [{"state": "stopped"}, {"state": "stopped"}]))
    def run(argv):
        calls.append(argv)
        if argv[0] == "uv" and argv[1:3] == ["tool", "list"]: return receipt(argv)
        if argv[-2:] == ["status", "--json"]: return completed(argv, stdout=json.dumps(next(statuses)))
        if argv[-1] == "--version": return completed(argv, stdout="woltspace 0.5.12\n")
        if argv[:4] == ["uv", "tool", "install", "--force"]: return completed(argv, 1 if fail_install else 0, stderr="install broke" if fail_install else "")
        return completed(argv)
    return run


def test_running_lodge_stop_install_start_order(native, monkeypatch):
    calls = []
    monkeypatch.setattr(updater, "_pypi", lambda: metadata("0.5.12"))
    monkeypatch.setattr(updater, "_run", lifecycle_run(calls))
    assert updater.update(args()) == 0
    actions = [c for c in calls if c[-1:] in (["stop"], ["start"]) or c[:4] == ["uv", "tool", "install", "--force"]]
    assert actions == [["/tools/woltspace", "stop"], ["uv", "tool", "install", "--force", "woltspace==0.5.12"], ["/tools/woltspace", "start"]]


def test_install_failure_still_restarts_and_fails(native, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(updater, "_pypi", lambda: metadata("0.5.12"))
    monkeypatch.setattr(updater, "_run", lifecycle_run(calls, fail_install=True))
    assert updater.update(args()) == 1
    assert ["/tools/woltspace", "start"] in calls
    assert "install broke" in json.loads(capsys.readouterr().out)["error"]


def test_stopped_lodge_not_started(native, monkeypatch):
    calls = []
    monkeypatch.setattr(updater, "_pypi", lambda: metadata("0.5.12"))
    monkeypatch.setattr(updater, "_run", lifecycle_run(calls, initially_running=False))
    assert updater.update(args()) == 0
    assert ["/tools/woltspace", "start"] not in calls
    assert ["/tools/woltspace", "stop"] not in calls
