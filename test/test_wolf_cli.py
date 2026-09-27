"""`woltspace wolf ...` — a thin client of the lodge's /wolf/crons API.

The HTTP layer is mocked throughout: these pin argument parsing, where the
message comes from, what gets sent, and what gets printed. Nothing here may
reach a lodge.

Usage: uv run pytest test/test_wolf_cli.py -v
"""

import io
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from woltspace import cli  # noqa: E402

CRON = {
    "wolt": "alpha", "name": "digest", "schedule": "0 6 * * *", "prompt": "/digest",
    "notify": None, "catch_up": True,
    "next_run": "2026-09-27T06:00:00-04:00", "last_run": "2026-09-26T06:00:00-04:00",
}


class Stdin(io.StringIO):
    def __init__(self, text="", tty=False):
        super().__init__(text)
        self._tty = tty

    def isatty(self):
        return self._tty


@pytest.fixture
def api(monkeypatch):
    """Record every request; answer from a queue of (status, body)."""
    calls, replies = [], []

    def fake(method, path, body=None):
        calls.append((method, path, body))
        return replies.pop(0) if replies else (200, {})

    monkeypatch.setattr(cli, "_wolf_request", fake)
    for key in ("WOLTSPACE_WOLT_NAME", "WOLT_NAME"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(sys, "stdin", Stdin(tty=True))
    return calls, replies


def run(*argv):
    return cli.main(["wolf", *argv])


class TestList:

    def test_json_filters_by_wolt(self, api, capsys):
        calls, replies = api
        replies.append((200, {"tz": "America/Montreal", "crons": [
            CRON, {**CRON, "wolt": "beta", "name": "other"}]}))
        assert run("list", "--wolt", "alpha", "--json") == 0
        assert calls == [("GET", "/wolf/crons", None)]
        out = json.loads(capsys.readouterr().out)
        assert out["tz"] == "America/Montreal"
        assert [c["name"] for c in out["crons"]] == ["digest"]

    def test_human_output_shows_lodge_times(self, api, capsys):
        api[1].append((200, {"tz": "America/Montreal", "crons": [CRON]}))
        assert run("list") == 0
        out = capsys.readouterr().out
        assert "alpha/digest" in out and "0 6 * * *" in out
        assert "next 2026-09-27 06:00" in out and "last 2026-09-26 06:00" in out
        assert "America/Montreal" in out and "/digest" in out


class TestAdd:

    def test_message_from_stdin_heredoc(self, api, monkeypatch, capsys):
        calls, replies = api
        monkeypatch.setattr(sys, "stdin", Stdin("Check the deploy\nthen tell me\n"))
        replies.append((201, {**CRON, "name": "check-the-deploy-then"}))
        assert run("add", "--wolt", "alpha", "--cron", "*/15 * * * *") == 0
        assert calls == [("POST", "/wolf/crons", {
            "wolt": "alpha", "prompt": "Check the deploy\nthen tell me",
            "schedule": "*/15 * * * *"})]
        assert "added alpha/check-the-deploy-then" in capsys.readouterr().out

    def test_message_flag_one_off_and_options(self, api):
        calls, replies = api
        replies.append((201, CRON))
        assert run("add", "--wolt", "alpha", "--at", "2026-10-01T09:30",
                   "--message", "ping", "--name", "remind", "--notify", "telegram") == 0
        assert calls[0][2] == {"wolt": "alpha", "prompt": "ping", "at": "2026-10-01T09:30",
                               "name": "remind", "notify": "telegram"}

    def test_wolt_defaults_to_the_calling_wolt(self, api, monkeypatch):
        calls, replies = api
        monkeypatch.setenv("WOLTSPACE_WOLT_NAME", "beta")
        replies.append((201, CRON))
        run("add", "--cron", "0 6 * * *", "--message", "hi")
        assert calls[0][2]["wolt"] == "beta"

    def test_dry_run_hits_the_dry_run_route(self, api, capsys):
        calls, replies = api
        replies.append((200, {"dry_run": True, "wolt": "alpha",
                              "entry": {"name": "hi", "schedule": "0 6 * * *", "prompt": "hi"},
                              "before": "", "after": "{}\n"}))
        assert run("add", "--wolt", "alpha", "--cron", "0 6 * * *",
                   "--message", "hi", "--dry-run", "--json") == 0
        assert calls[0][1] == "/wolf/crons?dry_run=1"
        assert json.loads(capsys.readouterr().out)["dry_run"] is True

    def test_needs_a_wolt_a_message_and_exactly_one_when(self, api, capsys):
        calls, _ = api
        assert run("add", "--cron", "0 6 * * *", "--message", "x") == 2      # no wolt
        assert run("add", "--wolt", "alpha", "--cron", "0 6 * * *") == 2     # tty, no message
        with pytest.raises(SystemExit):
            run("add", "--wolt", "alpha", "--cron", "x", "--at", "y", "--message", "m")
        with pytest.raises(SystemExit):
            run("add", "--wolt", "alpha", "--message", "m")
        assert calls == []

    def test_validation_errors_are_reported_with_their_field(self, api, capsys):
        api[1].append((400, {"error": "minute: 61 is out of range 0-59", "field": "schedule"}))
        assert run("add", "--wolt", "alpha", "--cron", "61 * * * *", "--message", "x") == 1
        out = capsys.readouterr().out
        assert "minute: 61 is out of range 0-59" in out and "(schedule)" in out

    def test_json_errors_stay_json(self, api, capsys):
        api[1].append((409, {"error": "taken", "field": "name"}))
        assert run("add", "--wolt", "a", "--cron", "0 6 * * *", "--message", "x", "--json") == 1
        assert json.loads(capsys.readouterr().out) == {"error": "taken", "field": "name"}


class TestSetRmRunRuns:

    def test_set_sends_only_what_changed(self, api):
        calls, replies = api
        replies.append((200, CRON))
        assert run("set", "alpha", "digest", "--cron", "30 7 * * 1-5", "--notify", "") == 0
        assert calls == [("PUT", "/wolf/crons/alpha/digest",
                          {"schedule": "30 7 * * 1-5", "notify": ""})]

    def test_set_move_and_message_from_stdin(self, api, monkeypatch):
        calls, replies = api
        monkeypatch.setattr(sys, "stdin", Stdin("new words\n"))
        replies.append((200, {**CRON, "wolt": "beta"}))
        assert run("set", "alpha", "digest", "--move-to", "beta", "--message", "-") == 0
        assert calls[0][2] == {"wolt": "beta", "prompt": "new words"}

    def test_set_without_changes_is_refused(self, api):
        assert run("set", "alpha", "digest") == 2
        assert api[0] == []

    def test_rm(self, api, capsys):
        calls, replies = api
        replies.append((200, {"deleted": True}))
        assert run("rm", "alpha", "digest") == 0
        assert calls == [("DELETE", "/wolf/crons/alpha/digest", None)]
        replies.append((404, {"error": "alpha has no cron named 'x'", "field": "name"}))
        assert run("rm", "alpha", "x") == 1

    def test_run(self, api, capsys):
        calls, replies = api
        replies.append((200, {"session": "alpha-abc123", "url": "http://x/tui?session=alpha-abc123"}))
        assert run("run", "alpha", "digest", "--json") == 0
        assert calls == [("POST", "/wolf/crons/alpha/digest/fire", None)]
        assert json.loads(capsys.readouterr().out)["session"] == "alpha-abc123"

    def test_runs(self, api, capsys):
        calls, replies = api
        replies.append((200, {"count": 1, "fires": [
            {"ts": "2026-09-26T06:00:01", "cron": "digest", "owner": "alpha",
             "event": "manual", "session": "alpha-abc123"}]}))
        assert run("runs", "--wolt", "alpha", "--limit", "5") == 0
        assert calls == [("GET", "/wolf/fires?limit=5&wolt=alpha", None)]
        out = capsys.readouterr().out
        assert "2026-09-26 06:00" in out and "alpha/digest" in out and "manual" in out


class TestTransport:

    def test_uses_woltspace_api_and_sends_json(self, monkeypatch):
        import urllib.request
        seen = {}

        class Response(io.BytesIO):
            status = 201

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout):
            seen["url"], seen["method"] = request.full_url, request.get_method()
            seen["body"] = json.loads(request.data)
            seen["type"] = request.get_header("Content-type")
            return Response(b'{"ok": true}')

        monkeypatch.setenv("WOLTSPACE_API", "http://127.0.0.1:9/")
        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        assert cli._wolf_request("POST", "/wolf/crons", {"a": 1}) == (201, {"ok": True})
        assert seen == {"url": "http://127.0.0.1:9/wolf/crons", "method": "POST",
                        "body": {"a": 1}, "type": "application/json"}

    def test_an_unreachable_lodge_exits_2(self, monkeypatch):
        import urllib.error
        import urllib.request

        def refuse(request, timeout):
            raise urllib.error.URLError("refused")

        monkeypatch.setenv("WOLTSPACE_API", "http://127.0.0.1:9")
        monkeypatch.setattr(urllib.request, "urlopen", refuse)
        with pytest.raises(SystemExit) as exc:
            cli._wolf_request("GET", "/wolf/crons")
        assert exc.value.code == 2
