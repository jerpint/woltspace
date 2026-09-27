"""The lodge's wolf management API: /wolf/crons and friends.

Filesystem-backed, against a throwaway colony; the one route that spawns a
session has `start_session` mocked.

Usage: uv run pytest test/test_wolf_api.py -v
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "container" / "lib"))

# The lib modules first: importing `server.app` prepends the installed
# bundle's container/lib, which would otherwise shadow this worktree's copy.
import sessions  # noqa: E402,F401
import wolfcore  # noqa: E402

import server.app as server_app  # noqa: E402


def _future(days=2):
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")


@pytest.fixture
def wolts(tmp_path, monkeypatch):
    root = tmp_path / "wolts"
    for wolt in ("alpha", "beta"):
        (root / wolt / "wolt").mkdir(parents=True)
        (root / wolt / "wolt" / "wolt.json").write_text(json.dumps({"name": wolt}))
    (root / "alpha" / "wolt" / "wolf.json").write_text(json.dumps({"crons": [
        {"name": "digest", "schedule": "0 6 * * *", "prompt": "/digest",
         "notify": "digest time", "timezone": "kept-as-is"},
    ]}, indent=2) + "\n")
    monkeypatch.setattr(server_app, "WOLTS_DIR", root)
    return root


@pytest.fixture
def client(wolts):
    from starlette.testclient import TestClient
    return TestClient(server_app.app, base_url="http://localhost:7777")


def _file(wolts, wolt):
    return json.loads((wolts / wolt / "wolt" / "wolf.json").read_text())


def test_the_server_uses_this_worktrees_core():
    assert Path(server_app.wolfcore.__file__).resolve() == \
        (ROOT / "container" / "lib" / "wolfcore.py").resolve()


class TestList:

    def test_lists_with_words_and_times(self, client, wolts):
        state = wolts / ".space" / "wolf" / "alpha"
        state.mkdir(parents=True)
        (state / "digest.last").write_text("2026-03-15-06:00")
        body = client.get("/wolf/crons").json()
        assert body["tz"]
        [cron] = body["crons"]
        assert cron["wolt"] == "alpha" and cron["name"] == "digest"
        assert cron["schedule"] == "0 6 * * *" and "at" not in cron
        # the fixture's old free-text notify is shown as the channel it always used
        assert cron["prompt"] == "/digest" and cron["notify"] == "telegram"
        assert cron["catch_up"] is True
        assert datetime.fromisoformat(cron["next_run"]).tzinfo is not None
        assert cron["last_run"].startswith("2026-03-15T06:00:00")

    def test_offers_only_channels_that_can_deliver(self, client, monkeypatch):
        monkeypatch.setattr(server_app, "dotenv_env", lambda key: {
            "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_ALLOWED_USERS": "1",
            "SLACK_BOT_TOKEN": "s",  # no SLACK_NOTIFY_CHANNEL: a ping would have nowhere to land
        }.get(key, ""))
        assert client.get("/wolf/crons").json()["channels"] == ["telegram"]

    def test_sorted_by_next_run_and_bad_entries_are_shown_not_fatal(self, client, wolts):
        (wolts / "beta" / "wolt" / "wolf.json").write_text(json.dumps({"crons": [
            {"name": "soon", "at": "2020-01-01T00:00", "prompt": "x"},  # overdue: first
            {"name": "broken", "schedule": "61 * * * *", "prompt": "x"},
        ]}))
        body = client.get("/wolf/crons").json()
        names = [c["name"] for c in body["crons"]]
        assert names == ["soon", "digest", "broken"]
        assert "minute: 61" in body["crons"][-1]["error"]


class TestAdd:

    def test_adds_with_a_derived_name(self, client, wolts):
        r = client.post("/wolf/crons", json={
            "wolt": "alpha", "schedule": "*/15 * * * *",
            "prompt": "Check the CI of the PR please"})
        assert r.status_code == 201, r.text
        assert r.json()["name"] == "check-the-ci-of"
        crons = _file(wolts, "alpha")["crons"]
        assert crons[-1] == {"name": "check-the-ci-of", "schedule": "*/15 * * * *",
                             "prompt": "Check the CI of the PR please"}
        assert crons[0]["timezone"] == "kept-as-is"           # unknown keys survive
        assert (wolts / "alpha" / "wolt" / "wolf.json").read_text().endswith("}\n")

    def test_derived_names_are_deduplicated(self, client, wolts):
        for _ in range(2):
            client.post("/wolf/crons", json={"wolt": "alpha", "schedule": "0 7 * * *",
                                             "prompt": "digest"})
        names = [c["name"] for c in _file(wolts, "alpha")["crons"]]
        assert names == ["digest", "digest-2", "digest-3"]

    def test_notify_takes_a_channel_not_free_text(self, client, wolts):
        r = client.post("/wolf/crons", json={
            "wolt": "beta", "schedule": "0 9 * * *", "prompt": "hi", "notify": "heads up"})
        assert r.status_code == 400 and r.json()["field"] == "notify"

    def test_one_off_with_options(self, client, wolts):
        at = _future()
        r = client.post("/wolf/crons", json={
            "wolt": "beta", "name": "remind", "at": at, "prompt": "ping",
            "notify": "telegram", "catch_up": False})
        assert r.status_code == 201
        assert _file(wolts, "beta")["crons"] == [{"name": "remind", "at": at, "prompt": "ping",
                                                  "notify": "telegram", "catch_up": False}]
        assert r.json()["next_run"].startswith(at)

    def test_dry_run_writes_nothing(self, client, wolts):
        path = wolts / "alpha" / "wolt" / "wolf.json"
        before = path.read_text()
        r = client.post("/wolf/crons?dry_run=1", json={
            "wolt": "alpha", "name": "x", "schedule": "0 9 * * 1", "prompt": "hi"})
        body = r.json()
        assert r.status_code == 200 and body["dry_run"] is True
        assert body["entry"] == {"name": "x", "schedule": "0 9 * * 1", "prompt": "hi"}
        assert body["before"] == before
        assert json.loads(body["after"])["crons"][-1]["name"] == "x"
        assert path.read_text() == before

    def test_dry_run_into_a_wolt_without_a_wolf_json(self, client, wolts):
        r = client.post("/wolf/crons?dry_run=1", json={
            "wolt": "beta", "schedule": "0 9 * * *", "prompt": "hi"})
        assert r.json()["before"] == ""
        assert not (wolts / "beta" / "wolt" / "wolf.json").exists()

    @pytest.mark.parametrize("body, status, field", [
        ({"schedule": "0 6 * * *", "prompt": "p"}, 400, "wolt"),
        ({"wolt": "../x", "schedule": "0 6 * * *", "prompt": "p"}, 400, "wolt"),
        ({"wolt": "ghost", "schedule": "0 6 * * *", "prompt": "p"}, 404, "wolt"),
        ({"wolt": "alpha", "prompt": "p"}, 400, "schedule"),
        ({"wolt": "alpha", "schedule": "0 6 * * *", "at": _future(), "prompt": "p"}, 400, "schedule"),
        ({"wolt": "alpha", "schedule": "61 6 * * *", "prompt": "p"}, 400, "schedule"),
        ({"wolt": "alpha", "schedule": "0 0 30 2 *", "prompt": "p"}, 400, "schedule"),
        ({"wolt": "alpha", "at": "2020-01-01T00:00", "prompt": "p"}, 400, "at"),
        ({"wolt": "alpha", "at": "next tuesday", "prompt": "p"}, 400, "at"),
        ({"wolt": "alpha", "schedule": "0 6 * * *"}, 400, "prompt"),
        ({"wolt": "alpha", "schedule": "0 6 * * *", "prompt": "p", "name": "a b"}, 400, "name"),
        ({"wolt": "alpha", "schedule": "0 6 * * *", "prompt": "p", "name": "digest"}, 409, "name"),
    ])
    def test_validation(self, client, wolts, body, status, field):
        before = (wolts / "alpha" / "wolt" / "wolf.json").read_text()
        r = client.post("/wolf/crons", json=body)
        assert r.status_code == status, r.text
        assert r.json()["field"] == field and r.json()["error"]
        assert (wolts / "alpha" / "wolt" / "wolf.json").read_text() == before

    def test_an_unreadable_wolf_json_is_a_conflict_not_a_clobber(self, client, wolts):
        path = wolts / "alpha" / "wolt" / "wolf.json"
        path.write_text("{ hand edit gone wrong")
        r = client.post("/wolf/crons", json={"wolt": "alpha", "schedule": "0 6 * * *",
                                             "prompt": "p"})
        assert r.status_code == 409
        assert path.read_text() == "{ hand edit gone wrong"


class TestUpdate:

    def test_partial_update_keeps_the_rest(self, client, wolts):
        r = client.put("/wolf/crons/alpha/digest", json={"schedule": "30 7 * * 1-5"})
        assert r.status_code == 200, r.text
        [cron] = _file(wolts, "alpha")["crons"]
        # an old free-text notify is written back as the channel it always meant
        assert cron == {"name": "digest", "schedule": "30 7 * * 1-5", "prompt": "/digest",
                        "notify": "telegram", "timezone": "kept-as-is"}

    def test_switch_kind_and_clear_notify(self, client, wolts):
        at = _future()
        r = client.put("/wolf/crons/alpha/digest",
                       json={"at": at, "notify": None, "prompt": "once"})
        assert r.status_code == 200
        [cron] = _file(wolts, "alpha")["crons"]
        assert cron["at"] == at and "schedule" not in cron and "notify" not in cron
        assert cron["prompt"] == "once"

    def test_move_to_another_wolt_carries_the_stamp(self, client, wolts):
        state = wolts / ".space" / "wolf"
        (state / "alpha").mkdir(parents=True)
        (state / "alpha" / "digest.last").write_text("2026-03-15-06:00")
        r = client.put("/wolf/crons/alpha/digest", json={"wolt": "beta"})
        assert r.status_code == 200 and r.json()["wolt"] == "beta"
        assert _file(wolts, "alpha")["crons"] == []
        assert _file(wolts, "beta")["crons"][0]["name"] == "digest"
        assert (state / "beta" / "digest.last").exists()
        assert not (state / "alpha" / "digest.last").exists()

    @pytest.mark.parametrize("path, body, status, field", [
        ("/wolf/crons/alpha/nope", {"prompt": "x"}, 404, "name"),
        ("/wolf/crons/ghost/digest", {"prompt": "x"}, 404, "wolt"),
        ("/wolf/crons/alpha/digest", {"wolt": "ghost"}, 404, "wolt"),
        ("/wolf/crons/alpha/digest", {"schedule": "0 99 * * *"}, 400, "schedule"),
        ("/wolf/crons/alpha/digest", {"schedule": "0 1 * * *", "at": _future()}, 400, "schedule"),
        ("/wolf/crons/alpha/digest", {"prompt": ""}, 400, "prompt"),
        ("/wolf/crons/alpha/digest", {"catch_up": "yes"}, 400, "catch_up"),
    ])
    def test_validation(self, client, wolts, path, body, status, field):
        before = (wolts / "alpha" / "wolt" / "wolf.json").read_text()
        r = client.put(path, json=body)
        assert r.status_code == status, r.text
        assert r.json()["field"] == field
        assert (wolts / "alpha" / "wolt" / "wolf.json").read_text() == before

    def test_move_onto_a_taken_name_conflicts(self, client, wolts):
        (wolts / "beta" / "wolt" / "wolf.json").write_text(json.dumps({"crons": [
            {"name": "digest", "schedule": "0 1 * * *", "prompt": "theirs"}]}))
        r = client.put("/wolf/crons/alpha/digest", json={"wolt": "beta"})
        assert r.status_code == 409
        assert _file(wolts, "alpha")["crons"][0]["name"] == "digest"


class TestDelete:

    def test_delete_removes_entry_and_stamp(self, client, wolts):
        state = wolts / ".space" / "wolf" / "alpha"
        state.mkdir(parents=True)
        (state / "digest.last").write_text("2026-03-15-06:00")
        r = client.delete("/wolf/crons/alpha/digest")
        assert r.status_code == 200 and r.json()["deleted"] is True
        assert _file(wolts, "alpha")["crons"] == []
        assert not (state / "digest.last").exists()

    def test_unknown(self, client):
        assert client.delete("/wolf/crons/alpha/nope").status_code == 404
        assert client.delete("/wolf/crons/ghost/digest").status_code == 404


class TestFire:

    def test_runs_now_and_journals_manual(self, client, wolts, monkeypatch):
        calls = []

        def fake_start(**kwargs):
            calls.append(kwargs)
            return {"name": "alpha-abc123", "url": "http://localhost:7777/tui?session=alpha-abc123"}

        monkeypatch.setattr(server_app, "start_session", fake_start)
        before = (wolts / "alpha" / "wolt" / "wolf.json").read_text()
        r = client.post("/wolf/crons/alpha/digest/fire")
        assert r.status_code == 200
        assert r.json() == {"session": "alpha-abc123",
                            "url": "http://localhost:7777/tui?session=alpha-abc123"}
        assert calls == [{"wolt": "alpha", "prompt": "/digest", "routing": {"adapter": "lodge"}}]
        assert (wolts / "alpha" / "wolt" / "wolf.json").read_text() == before
        assert not (wolts / ".space" / "wolf" / "alpha" / "digest.last").exists()
        [line] = (wolts / ".space" / "wolf" / "jobs.jsonl").read_text().splitlines()
        entry = json.loads(line)
        assert entry["event"] == "manual" and entry["cron"] == "digest"
        assert entry["owner"] == "alpha" and entry["session"] == "alpha-abc123"
        # and it shows up where fires are read
        assert client.get("/wolf/fires?cron=digest").json()["fires"][0]["event"] == "manual"

    def test_spawn_failure_is_journaled(self, client, wolts, monkeypatch):
        def boom(**kwargs):
            raise RuntimeError("tmux is gone")

        monkeypatch.setattr(server_app, "start_session", boom)
        r = client.post("/wolf/crons/alpha/digest/fire")
        assert r.status_code == 500 and "tmux is gone" in r.json()["error"]
        entry = json.loads((wolts / ".space" / "wolf" / "jobs.jsonl").read_text())
        assert entry["event"] == "manual" and entry["error"] == "tmux is gone"

    def test_unknown(self, client, monkeypatch):
        monkeypatch.setattr(server_app, "start_session", lambda **k: pytest.fail("spawned"))
        assert client.post("/wolf/crons/alpha/nope/fire").status_code == 404
        assert client.post("/wolf/crons/ghost/digest/fire").status_code == 404


class TestTheGuardStillApplies:

    def test_cross_site_writes_are_refused(self, client, wolts):
        before = (wolts / "alpha" / "wolt" / "wolf.json").read_text()
        r = client.post("/wolf/crons", json={"wolt": "alpha", "schedule": "0 6 * * *",
                                             "prompt": "p"},
                        headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
        r = client.delete("/wolf/crons/alpha/digest", headers={"Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 403
        assert (wolts / "alpha" / "wolt" / "wolf.json").read_text() == before

    def test_untrusted_host_is_refused(self, wolts):
        from starlette.testclient import TestClient
        evil = TestClient(server_app.app, base_url="http://evil.example")
        assert evil.get("/wolf/crons").status_code == 403


def test_schedules_reads_per_wolt_stamps(client, wolts):
    """/wolf/schedules is unchanged in shape; it now reads the per-wolt stamp."""
    state = wolts / ".space" / "wolf" / "alpha"
    state.mkdir(parents=True)
    (state / "digest.last").write_text("2026-03-15-06:00")
    [alpha] = [s for s in client.get("/wolf/schedules").json()["schedules"] if s["wolt"] == "alpha"]
    assert alpha["crons"][0]["last_run"] == "2026-03-15-06:00"
    assert set(alpha["crons"][0]) == {"name", "schedule", "at", "last_run"}
