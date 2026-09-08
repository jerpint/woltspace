"""Regressions for the defects the first agent-driven e2e run found.

Each class names the defect and reproduces the evidence in the run report
(`wolt/drafts/e2e-run-1-report.md` in uxwolt's drafts). They are unit-level on
purpose: the run needed a container and a browser to *find* these, but every one
of them is a wrong line of Python that can be pinned without either.

Usage: uv run pytest test/test_e2e_run1_fixes.py -v
"""

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "container" / "lib"))

# The lib modules first, deliberately: importing `server.app` prepends the
# *installed* bundle's container/lib to sys.path, which would otherwise shadow
# this worktree's copy of `sessions` and test the wrong file.
import sessions  # noqa: E402,F401
from harness_auth import (  # noqa: E402
    CLAUDE_TOKEN_VARS,
    auth_source,
    claude_authenticated,
    claude_token_in_env,
)

import server.app as server_app  # noqa: E402


# ---------------------------------------------------------------------------
# D5 — an authenticated colony was still greeted with a login screen
# ---------------------------------------------------------------------------

class TestAuthProbeHonoursTheEnvironmentToken:
    """The run booted a colony on CLAUDE_CODE_OAUTH_TOKEN. Its sessions
    authenticated fine; its human window was sent to `wclaude /login`, whose own
    warning says continuing there replaces the working token."""

    @pytest.fixture(autouse=True)
    def _no_ambient_token(self, monkeypatch):
        """Both vars, every test: a developer with ANTHROPIC_API_KEY exported
        must get the same answers as CI, or these assertions mean nothing."""
        for name in CLAUDE_TOKEN_VARS:
            monkeypatch.delenv(name, raising=False)

    def test_a_credentials_file_is_still_authentication(self, tmp_path):
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / ".credentials.json").write_text("{}")

        assert claude_authenticated(tmp_path, {}) is True

    def test_a_token_in_the_environment_is_authentication(self, tmp_path):
        assert claude_authenticated(tmp_path, {"CLAUDE_CODE_OAUTH_TOKEN": "tok"}) is True
        assert claude_authenticated(tmp_path, {"ANTHROPIC_API_KEY": "sk-x"}) is True

    def test_neither_is_not(self, tmp_path):
        assert claude_authenticated(tmp_path, {}) is False

    def test_an_empty_or_whitespace_token_does_not_count(self, tmp_path):
        assert claude_authenticated(tmp_path, {"CLAUDE_CODE_OAUTH_TOKEN": ""}) is False
        assert claude_authenticated(tmp_path, {"CLAUDE_CODE_OAUTH_TOKEN": "   "}) is False

    def test_the_environment_is_read_at_call_time(self, tmp_path, monkeypatch):
        """The entrypoint assembles its environment mid-boot; a snapshot taken
        at import would answer with the environment boot started in."""
        assert claude_token_in_env() is False
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
        assert claude_token_in_env() is True


class TestTheAuthSourceIsVisible:
    """Presence is not validity. A dead env token still reads as authenticated,
    so the one thing the platform owes a user debugging that is which credential
    answered — the thing to remove."""

    def test_the_file_and_the_token_are_told_apart(self, tmp_path):
        assert auth_source(tmp_path, {}) == "none"
        assert auth_source(tmp_path, {"CLAUDE_CODE_OAUTH_TOKEN": "t"}) == "env-token"
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / ".credentials.json").write_text("{}")
        assert auth_source(tmp_path, {}) == "credentials-file"

    def test_the_file_wins_when_both_are_present(self, tmp_path):
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / ".credentials.json").write_text("{}")

        assert auth_source(tmp_path, {"CLAUDE_CODE_OAUTH_TOKEN": "t"}) == "credentials-file"

    def test_the_module_says_out_loud_that_presence_is_not_validity(self):
        import harness_auth

        doc = harness_auth.__doc__
        assert "Presence is not validity" in doc
        # and names the way out
        assert "unset" in doc.lower() and "restart" in doc.lower()

    def test_onboard_status_reports_the_source(self, monkeypatch):
        from starlette.testclient import TestClient

        monkeypatch.setattr(server_app, "CONTAINER_HOME", Path("/nonexistent-home"))
        for name in CLAUDE_TOKEN_VARS:
            monkeypatch.delenv(name, raising=False)
        body = TestClient(server_app.app).get("/onboard-status").json()
        assert body["auth_source"] == "none"
        assert body["has_oauth"] is False

        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
        body = TestClient(server_app.app).get("/onboard-status").json()
        assert body["auth_source"] == "env-token"
        assert body["has_oauth"] is True

    def test_doctor_names_the_token_rather_than_folding_it_in(self):
        source = (ROOT / "src" / "woltspace" / "doctor.py").read_text()
        assert 'authenticated.append("claude (env token)")' in source


class TestBootGreetsAnAuthenticatedColonyProperly:
    def _greeting(self, tmp_path, monkeypatch, env):
        from woltspace import container_entrypoint as boot

        calls = []
        monkeypatch.setattr(boot.subprocess, "run",
                            lambda command, **kw: calls.append(list(command)))
        monkeypatch.setattr(boot, "HOME", tmp_path / "home")
        (tmp_path / "home" / ".claude").mkdir(parents=True)
        for key in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)

        boot.open_tmux_window("mywolt", tmp_path / "wolt", tmp_path / "wolts")
        return [c[4] for c in calls if c[:2] == ["tmux", "send-keys"]]

    def test_a_token_colony_gets_the_wolt_greeting_not_login(self, tmp_path, monkeypatch):
        sent = self._greeting(tmp_path, monkeypatch, {"CLAUDE_CODE_OAUTH_TOKEN": "tok"})

        assert sent == ['wclaude --dangerously-skip-permissions "hey mywolt"']

    def test_an_unauthenticated_colony_still_gets_login(self, tmp_path, monkeypatch):
        sent = self._greeting(tmp_path, monkeypatch, {})

        assert sent == ["wclaude /login"]


class TestDoctorHostAuthHonoursTheToken:
    def test_a_token_counts_as_claude_auth(self, monkeypatch):
        from woltspace import doctor

        for name in CLAUDE_TOKEN_VARS:
            monkeypatch.delenv(name, raising=False)
        assert doctor._claude_token_in_env() is False

        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
        assert doctor._claude_token_in_env() is True


# ---------------------------------------------------------------------------
# D1 — public/ was served as text/html, whatever the file actually was
# ---------------------------------------------------------------------------

class TestPlatformFilesGetRealMimeTypes:
    """`/sw.js` came back `text/html`, so chromium refused to register the
    service worker: "The script has an unsupported MIME type ('text/html')"."""

    def _client(self, tmp_path, monkeypatch):
        from starlette.testclient import TestClient

        monkeypatch.setattr(server_app, "PUBLIC_DIR", tmp_path)
        return TestClient(server_app.app)

    def test_the_service_worker_is_javascript(self, tmp_path, monkeypatch):
        (tmp_path / "sw.js").write_text("self.addEventListener('install', () => {});\n")

        response = self._client(tmp_path, monkeypatch).get("/sw.js")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/javascript")
        assert "addEventListener" in response.text

    @pytest.mark.parametrize("name,expected", [
        ("favicon.svg", "image/svg+xml"),
        ("manifest.json", "application/json"),
        ("style.css", "text/css"),
        ("onboard.html", "text/html"),
    ])
    def test_each_extension_is_answered_as_itself(self, tmp_path, monkeypatch,
                                                  name, expected):
        (tmp_path / name).write_text("x")

        response = self._client(tmp_path, monkeypatch).get(f"/{name}")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith(expected)

    def test_a_binary_file_is_not_decoded_as_text(self, tmp_path, monkeypatch):
        """`read_text()` on a PNG raises; the icons in public/ are real files."""
        png = bytes.fromhex("89504e470d0a1a0a") + b"\x00\xff\xfe"
        (tmp_path / "icon.png").write_bytes(png)

        response = self._client(tmp_path, monkeypatch).get("/icon.png")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/png")
        assert response.content == png

    def test_a_missing_file_is_still_a_404(self, tmp_path, monkeypatch):
        assert self._client(tmp_path, monkeypatch).get("/nope.js").status_code == 404


# ---------------------------------------------------------------------------
# D2 + D3 — the file watchers never learned to stop
# ---------------------------------------------------------------------------

class TestWatchersStopOnShutdown:
    """`docker stop` logged `Cancel 4 running task(s), timeout graceful shutdown
    exceeded` plus a CancelledError traceback, took ~3s, and intermittently exited
    133 with `FATAL: exception not rethrown` — a daemon thread killed mid-call."""

    def test_the_lodge_watcher_is_handed_a_stop_event(self, tmp_path, monkeypatch):
        seen = {}

        def fake_watch(path, **kwargs):
            seen.update(kwargs)
            seen["path"] = path
            while not kwargs["stop_event"].is_set():
                time.sleep(0.01)
            return iter(())

        monkeypatch.setattr(server_app, "SITE_DIR", tmp_path)
        monkeypatch.setitem(sys.modules, "watchfiles",
                            type(sys)("watchfiles"))
        sys.modules["watchfiles"].watch = fake_watch

        server_app._start_file_watcher()
        try:
            for _ in range(200):
                if "stop_event" in seen:
                    break
                time.sleep(0.01)
            assert seen["path"] == str(tmp_path)
            assert isinstance(seen["stop_event"], threading.Event)
            # it must come up for air often enough to notice the flag
            assert 0 < seen["rust_timeout"] <= 1000
            assert server_app._watcher_threads, "thread was not registered for joining"
        finally:
            server_app.stop_file_watchers()

    def test_stopping_joins_the_thread_rather_than_leaving_it_to_be_killed(
            self, tmp_path, monkeypatch):
        """A daemon thread killed by interpreter shutdown is exactly the 133."""
        def fake_watch(path, **kwargs):
            while not kwargs["stop_event"].is_set():
                time.sleep(0.01)
            return iter(())

        monkeypatch.setattr(server_app, "SITE_DIR", tmp_path)
        monkeypatch.setitem(sys.modules, "watchfiles", type(sys)("watchfiles"))
        sys.modules["watchfiles"].watch = fake_watch

        server_app._start_file_watcher()
        started = list(server_app._watcher_threads)
        assert started and started[0].is_alive()

        server_app.stop_file_watchers()

        assert not started[0].is_alive()
        assert server_app._watcher_threads == []

    def test_shutdown_also_stops_every_open_livereload_socket(self):
        stop = threading.Event()
        server_app._livereload_stops.add(stop)
        try:
            server_app.stop_file_watchers()
            assert stop.is_set(), "an open livereload watcher was left running"
        finally:
            server_app._livereload_stops.discard(stop)

    def test_the_lifespan_stops_watchers_before_the_tunnel(self):
        """Order matters: the watchers are what outran the graceful window."""
        source = (ROOT / "server" / "app.py").read_text()
        body = source[source.index("async def lifespan("):]
        assert body.index("stop_file_watchers()") < body.index("tunnel_mgr.stop_tunnel()")

    def test_the_socket_handler_watches_for_the_disconnect_too(self):
        """uvicorn closes connections *before* running lifespan shutdown, so a
        handler that only watches files hears about the stop too late."""
        source = (ROOT / "server" / "app.py").read_text()
        handler = source[source.index("async def site_livereload_ws("):]
        handler = handler[:handler.index("@app.get")]

        assert "stop_event=stop" in handler
        assert "ws.receive()" in handler, "nothing notices uvicorn's disconnect"
        assert "FIRST_COMPLETED" in handler
        assert "except asyncio.CancelledError:" in handler
        assert "_livereload_stops.discard(stop)" in handler
        # no orphan task may outlive the handler — but the watcher is given a
        # moment to finish on its own first, because ripping an async generator
        # out of a to_thread call is what leaked a CancelledError into whoever
        # opened the socket
        assert "asyncio.wait_for(asyncio.shield(watcher)" in handler
        assert "watcher.cancel()" in handler
        assert "return_exceptions=True" in handler

    def test_a_client_data_frame_is_not_a_disconnect(self):
        """A heartbeat used to look like a hangup: it ended the watcher and
        left the socket open with nothing behind it."""
        source = (ROOT / "server" / "app.py").read_text()
        handler = source[source.index("async def site_livereload_ws("):]
        handler = handler[:handler.index("@app.get")]

        assert "async def _await_disconnect()" in handler
        assert 'message.get("type") == "websocket.disconnect"' in handler
        assert "while True:" in handler

    def test_flagging_is_separable_from_joining(self):
        """The flags must be raisable without blocking on a thread join."""
        from server import app as server_app

        stop = threading.Event()
        server_app._livereload_stops.add(stop)
        try:
            server_app.signal_watchers_to_stop()
            assert stop.is_set()
        finally:
            server_app._livereload_stops.discard(stop)
            server_app._watcher_stop.clear()


# ---------------------------------------------------------------------------
# D6 — an unconnected chat was reported as a server fault
# ---------------------------------------------------------------------------

class TestNotifyWithNowhereToSend:
    def _client(self):
        from starlette.testclient import TestClient

        return TestClient(server_app.app, raise_server_exceptions=False)

    def test_an_unconfigured_channel_is_409_not_500(self, monkeypatch):
        from server import app as server_app
        from server.notify import NoNotificationTarget

        async def refuse(*args, **kwargs):
            raise NoNotificationTarget("no notification target — set TELEGRAM_BOT_TOKEN")

        monkeypatch.setattr(server_app, "send_notification", refuse)
        response = self._client().post("/notify", json={"message": "hi"})

        assert response.status_code == 409
        body = response.json()
        assert body["ok"] is False
        assert body["reason"] == "no_notification_target"
        assert "TELEGRAM_BOT_TOKEN" in body["remedy"]

    def test_a_real_failure_is_still_a_500(self, monkeypatch):
        async def explode(*args, **kwargs):
            raise RuntimeError("telegram API is on fire")

        monkeypatch.setattr(server_app, "send_notification", explode)
        response = self._client().post("/notify", json={"message": "hi"})

        assert response.status_code == 500

    def test_the_message_is_still_required(self, monkeypatch):
        """The stand-in is the assertion: a bodyless notify must be refused
        before anything reaches the sending layer."""
        sent = []

        async def record(*args, **kwargs):
            sent.append(args)
            return {"adapter": "telegram"}

        monkeypatch.setattr(server_app, "send_notification", record)

        assert self._client().post("/notify", json={}).status_code == 400
        assert sent == []


# ---------------------------------------------------------------------------
# D4 + F3 — the wolf's state was undocumented and unobservable
# ---------------------------------------------------------------------------

class TestWolfStatePathsAreDocumentedWhereTheyAre:
    """The source claimed `{wolt}/.state/wolf/jobs.jsonl`; the scheduler writes
    lodge-global `.space/wolf/`. Ten minutes of the e2e run went to polling a
    path that never appears."""

    def test_the_module_no_longer_promises_a_per_wolt_directory(self):
        source = (ROOT / "container" / "creatures" / "wolf.py").read_text()
        header = source[:source.index('"""', 3)]
        assert ".space/wolf/" in header
        assert "{wolf_wolt}/.state/wolf/" not in header
        assert "{wolt}/.state/wolf/jobs.jsonl" not in source

    def test_the_skill_points_at_the_real_directory(self):
        skill = (ROOT / "container" / "skills" / "wolf" / "SKILL.md").read_text()
        assert "`.space/wolf/`" in skill
        assert "- Last-run timestamps stored in `.state/wolf/`" not in skill


class TestWolfObservabilityApi:
    """F3: the only way to see a fire was to grep a connector log from inside
    the container. Read-only, filesystem-backed, no new daemon."""

    @pytest.fixture
    def colony(self, tmp_path, monkeypatch):
        from starlette.testclient import TestClient

        wolts = tmp_path / "wolts"
        (wolts / "alpha" / "wolt").mkdir(parents=True)
        (wolts / "alpha" / "wolt" / "wolf.json").write_text(json.dumps({"crons": [
            {"name": "digest", "schedule": "0 6 * * *", "prompt": "/digest",
             "notify": "digest time"},
            {"name": "one-off", "at": "2026-03-22T14:30", "prompt": "check the deploy"},
        ]}))
        (wolts / "beta" / "wolt").mkdir(parents=True)
        (wolts / "beta" / "wolt" / "wolf.json").write_text(json.dumps({"crons": []}))
        state = wolts / ".space" / "wolf"
        state.mkdir(parents=True)
        (state / "digest.last").write_text("2026-09-06-06:00\n")
        (state / "jobs.jsonl").write_text(
            json.dumps({"ts": "2026-09-06T06:00:00", "cron": "digest",
                        "action": "session", "event": "started", "owner": "alpha"}) + "\n" +
            json.dumps({"ts": "2026-09-06T06:00:01", "cron": "digest",
                        "action": "session", "event": "dispatched", "owner": "alpha"}) + "\n" +
            "{ torn line while the wolf was mid-append\n" +
            json.dumps({"ts": "2026-09-06T07:00:00", "cron": "sweep",
                        "action": "session", "event": "started", "owner": "beta"}) + "\n"
        )
        monkeypatch.setattr(server_app, "WOLTS_DIR", wolts)
        return TestClient(server_app.app)

    def test_schedules_lists_every_wolts_crons_with_last_run(self, colony):
        body = colony.get("/wolf/schedules").json()

        by_wolt = {entry["wolt"]: entry for entry in body["schedules"]}
        assert set(by_wolt) == {"alpha", "beta"}
        assert by_wolt["beta"]["crons"] == []

        crons = {cron["name"]: cron for cron in by_wolt["alpha"]["crons"]}
        assert crons["digest"]["schedule"] == "0 6 * * *"
        assert crons["digest"]["last_run"] == "2026-09-06-06:00"
        # a one-off carries `at` instead of `schedule`, and has never fired
        assert crons["one-off"]["at"] == "2026-03-22T14:30"
        assert crons["one-off"]["schedule"] == ""
        assert crons["one-off"]["last_run"] is None

    def test_a_broken_wolf_json_is_reported_not_fatal(self, colony, tmp_path):
        (tmp_path / "wolts" / "beta" / "wolt" / "wolf.json").write_text("{ not json")

        body = colony.get("/wolf/schedules").json()

        beta = next(e for e in body["schedules"] if e["wolt"] == "beta")
        assert beta["crons"] == []
        assert beta["error"]

    def test_fires_are_newest_first_and_skip_torn_lines(self, colony):
        body = colony.get("/wolf/fires").json()

        assert body["count"] == 3          # the unparseable line is dropped
        assert body["fires"][0]["cron"] == "sweep"
        assert body["fires"][-1]["event"] == "started"

    def test_fires_can_be_narrowed_and_capped(self, colony):
        assert colony.get("/wolf/fires?cron=digest").json()["count"] == 2
        assert colony.get("/wolf/fires?wolt=beta").json()["count"] == 1
        assert len(colony.get("/wolf/fires?limit=1").json()["fires"]) == 1

    def test_a_colony_whose_wolf_never_fired_is_not_an_error(self, tmp_path, monkeypatch):
        from starlette.testclient import TestClient

        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setattr(server_app, "WOLTS_DIR", empty)
        client = TestClient(server_app.app)

        assert client.get("/wolf/fires").json() == {"count": 0, "fires": []}
        assert client.get("/wolf/schedules").json()["schedules"] == []

    def test_the_crons_own_words_are_never_served(self, colony):
        """No auth, and `Access-Control-Allow-Origin: *`. What a cron is told to
        do is the user's private business; when it runs is the observability."""
        raw = colony.get("/wolf/schedules").text

        assert "/digest" not in raw          # the prompt
        assert "digest time" not in raw      # the notify text
        assert "check the deploy" not in raw
        for cron in colony.get("/wolf/schedules").json()["schedules"][0]["crons"]:
            assert set(cron) == {"name", "schedule", "at", "last_run"}

    def test_a_cron_name_cannot_walk_out_of_the_state_dir(self, tmp_path, monkeypatch):
        """`wolf.json` is a file a wolt can write, so the name is untrusted."""
        from starlette.testclient import TestClient

        wolts = tmp_path / "wolts"
        (wolts / "alpha" / "wolt").mkdir(parents=True)
        secret = wolts / ".space" / "secret.last"
        secret.parent.mkdir(parents=True)
        secret.write_text("SECRET-CONTENT")
        (wolts / "alpha" / "wolt" / "wolf.json").write_text(json.dumps({"crons": [
            {"name": "../secret", "schedule": "0 6 * * *"},
            {"name": "../../etc/passwd", "schedule": "0 6 * * *"},
            {"name": "has spaces", "schedule": "0 6 * * *"},
            {"name": "fine-one_2", "schedule": "0 6 * * *"},
        ]}))
        monkeypatch.setattr(server_app, "WOLTS_DIR", wolts)

        body = TestClient(server_app.app).get("/wolf/schedules")

        assert "SECRET-CONTENT" not in body.text
        assert all(cron["last_run"] is None for cron in body.json()["schedules"][0]["crons"])

    def test_the_limit_is_clamped(self, colony):
        assert len(colony.get("/wolf/fires?limit=99999").json()["fires"]) <= 500
        assert colony.get("/wolf/fires?limit=-5").json()["fires"] == []

    def test_only_the_journals_tail_is_parsed(self, colony, monkeypatch):
        """A colony that has fired crons for a year must not pay for all of it."""
        monkeypatch.setattr(server_app, "_WOLF_JOURNAL_TAIL", 2)

        body = colony.get("/wolf/fires").json()

        assert body["count"] == 1   # only the last two lines were read; one is torn
        assert body["fires"][0]["cron"] == "sweep"

    def test_the_routes_do_not_block_the_event_loop(self):
        """Sync filesystem work in an `async def` stalls every other request."""
        import inspect

        assert not inspect.iscoroutinefunction(server_app.wolf_schedules)
        assert not inspect.iscoroutinefunction(server_app.wolf_fires)

    def test_the_exactly_once_evidence_the_e2e_run_had_to_grep_for(self, colony):
        """The run proved one fire by grepping a connector log. Now it is data."""
        fires = colony.get("/wolf/fires?cron=digest").json()["fires"]

        assert [f["event"] for f in fires] == ["dispatched", "started"]
        assert {f["owner"] for f in fires} == {"alpha"}


# ---------------------------------------------------------------------------
# F5 — "orphaned" never said why
# ---------------------------------------------------------------------------

class TestOrphanedSessionsRecordWhy:
    def _registry(self, tmp_path):
        from sessions import SessionRegistry

        return SessionRegistry(tmp_path)

    def test_reconcile_stamps_the_reason(self, tmp_path, monkeypatch):
        import sessions

        reg = self._registry(tmp_path)
        reg.create(name="s1", wolt="alpha", creature="otter", model="haiku",
                   dir=str(tmp_path / "alpha"), prompt="x")
        monkeypatch.setattr(sessions, "_tmux_sessions", lambda: set())

        assert reg.reconcile() == ["s1"]

        record = json.loads(
            (tmp_path / "alpha" / ".state" / "sessions" / "s1.json").read_text())
        assert record["status"] == "orphaned"
        assert record["orphaned_reason"] == sessions.ORPHAN_TMUX_MISSING

    def test_the_boot_sweep_says_the_runtime_went_away(self, tmp_path, fake_runtime):
        """A container restart resets the PID namespace — nothing to adopt."""
        import sessions

        reg = self._registry(tmp_path)
        reg.create(name="s2", wolt="alpha", creature="otter", model="haiku",
                   dir=str(tmp_path / "alpha"), prompt="x")
        fake_runtime._alive = False

        report = reg.adopt_runtime_sessions()

        assert [entry["session"] for entry in report["orphaned"]] == ["s2"]
        assert report["orphaned"][0]["reason"] == sessions.ORPHAN_RUNTIME_GONE
        record = json.loads(
            (tmp_path / "alpha" / ".state" / "sessions" / "s2.json").read_text())
        assert record["orphaned_reason"] == sessions.ORPHAN_RUNTIME_GONE

    def test_coming_back_to_life_clears_the_reason(self, tmp_path, fake_runtime):
        """`running` plus an `orphaned_reason` is a record that lies."""
        import sessions

        reg = self._registry(tmp_path)
        reg.create(name="s4", wolt="alpha", creature="otter", model="haiku",
                   dir=str(tmp_path / "alpha"), prompt="x")
        record_path = tmp_path / "alpha" / ".state" / "sessions" / "s4.json"

        fake_runtime._alive = False
        reg.adopt_runtime_sessions()
        assert json.loads(record_path.read_text())["orphaned_reason"]

        # every road back to running goes through _write, so one check covers
        # adoption and all three resume branches
        fake_runtime._alive = True
        reg.update("s4", wolt="alpha", status="running")

        record = json.loads(record_path.read_text())
        assert record["status"] == "running"
        assert "orphaned_reason" not in record

    def test_a_live_session_carries_no_reason(self, tmp_path, monkeypatch):
        import sessions

        reg = self._registry(tmp_path)
        reg.create(name="s3", wolt="alpha", creature="otter", model="haiku",
                   dir=str(tmp_path / "alpha"), prompt="x")
        monkeypatch.setattr(sessions, "_tmux_sessions", lambda: {"s3"})

        assert reg.reconcile() == []
        record = json.loads(
            (tmp_path / "alpha" / ".state" / "sessions" / "s3.json").read_text())
        assert "orphaned_reason" not in record
