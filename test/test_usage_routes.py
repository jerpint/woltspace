"""Lodge usage over HTTP and `woltspace usage`: fresh, single-flight, read-only and lodge-private."""

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI
from starlette.testclient import TestClient

from usage_support import claude, count, emit, host, request, save, wolt

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import usage_collector  # noqa: E402
from server import usage_routes  # noqa: E402


@pytest.fixture
def lodge(tmp_path, monkeypatch):
    """A lodge with one alpha transcript; the routes count it with neutral shared homes."""
    transcript = emit(claude(tmp_path), request("one"))
    monkeypatch.setattr(usage_routes, "count_usage", lambda: count(tmp_path, bases=host(tmp_path)))
    return tmp_path, transcript


@pytest.fixture
def client(lodge):
    app = FastAPI()
    app.include_router(usage_routes.router)
    return TestClient(app, base_url="http://localhost:7777")


def test_data_is_counted_fresh_for_every_request_and_never_cached(lodge, client):
    tmp_path, transcript = lodge
    save(tmp_path)
    history = [(path.read_bytes(), path.stat().st_mtime_ns)
               for path in (tmp_path / "history.json", tmp_path / "history.json.lock")]
    first = client.get("/api/usage/data")
    assert first.status_code == 200 and first.headers["cache-control"] == "no-store"
    assert first.json()["periods"]["all_time"]["totals"]["input_fresh"] == 5
    emit(transcript, request("one"), request("two", fresh=7))
    second = client.get("/api/usage/data").json()
    assert second["periods"]["all_time"]["totals"]["input_fresh"] == 12
    assert second["last_collected"] > first.json()["last_collected"]
    assert [(path.read_bytes(), path.stat().st_mtime_ns)
            for path in (tmp_path / "history.json", tmp_path / "history.json.lock")] == history


def test_concurrent_requests_share_one_count_and_a_later_one_counts_again(monkeypatch):
    calls, release = [], threading.Event()

    def slow_count():
        calls.append(1)
        release.wait(10)
        return {"count": len(calls)}

    monkeypatch.setattr(usage_routes, "count_usage", slow_count)

    async def scenario():
        waiting = [asyncio.create_task(usage_routes.fresh_count()) for _ in range(5)]
        await asyncio.sleep(0)  # every task has run up to its await on the one flight
        while not calls:
            await asyncio.sleep(0.01)  # the count has started in its worker thread
        release.set()
        shared = await asyncio.gather(*waiting)
        return shared, await usage_routes.fresh_count()

    shared, later = asyncio.run(scenario())
    assert shared == [{"count": 1}] * 5 and later == {"count": 2}


def test_a_bad_query_is_refused_before_any_count(monkeypatch, client):
    def unexpected():
        raise AssertionError("an invalid query started a count")

    monkeypatch.setattr(usage_routes, "count_usage", unexpected)
    for query, error in (("by=bogus", "invalid by"), ("wolt=..%2Falpha", "invalid wolt"),
                         ("by=wolt&by=day", "repeated parameter"), ("secret=1", "unknown parameter")):
        response = client.get(f"/api/usage?{query}")
        assert response.status_code == 400 and response.json() == {"error": error}
        assert response.headers["cache-control"] == "no-store"


def test_failures_are_fixed_codes_without_details(monkeypatch, client, capsys):
    def broken():
        raise OSError("/private/path/history.json")

    def other_zone():
        raise usage_collector.HistoryZoneMismatch("history days are in Zone/A, the lodge is in Zone/B")

    for failing, code in ((broken, "count_failed"), (other_zone, "history_zone_mismatch")):
        monkeypatch.setattr(usage_routes, "count_usage", failing)
        for path in ("/api/usage/data", "/api/usage?by=wolt"):
            response = client.get(path)
            assert response.status_code == 503 and response.json() == {"error": code}
    log = capsys.readouterr().out
    assert "/private" not in log and "Zone/" not in log and "OSError" in log


def test_queries_answer_in_each_format(lodge, client):
    for fmt, media in (("json", "application/json"), ("table", "text/plain"), ("csv", "text/csv")):
        response = client.get(f"/api/usage?by=wolt&format={fmt}")
        assert response.status_code == 200 and response.headers["content-type"].startswith(media)
        assert response.headers["cache-control"] == "no-store"
    assert client.get("/api/usage?by=wolt").json()["rows"][0]["wolt"] == "alpha"
    assert client.get("/api/usage?wolt=nobody").json() == {"error": "unknown or ambiguous wolt"}


def test_an_app_subdomain_or_foreign_host_never_reaches_usage(monkeypatch):
    import server.app as server_app

    def unexpected():
        raise AssertionError("usage was served outside the lodge")

    monkeypatch.setattr(usage_routes, "count_usage", unexpected)
    monkeypatch.setattr("apps.running_apps", lambda: [])
    monkeypatch.setattr(server_app.tunnel_mgr, "get_tunnel_domain", lambda: "lodge.example")
    client = TestClient(server_app.app, base_url="http://localhost:7777")
    for host_header in ("pal.localhost:7777", "pal.lodge.example"):  # a shared app's address
        for path in ("/api/usage/data", "/api/usage?by=wolt"):
            response = client.get(path, headers={"host": host_header})
            assert response.status_code == 503 and "is not running" in response.text
    assert client.get("/api/usage/data", headers={"host": "evil.example"}).status_code == 403


@pytest.fixture
def api(client):
    """The same routes on loopback, for a real `woltspace usage` process."""
    server = uvicorn.Server(uvicorn.Config(client.app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline and thread.is_alive()
        time.sleep(0.02)
    yield f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    server.should_exit = True
    thread.join(10)


def usage_cli(api, *args):
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("TMPDIR", "/tmp"), "WOLTSPACE_API": api}
    return subprocess.run([sys.executable, str(ROOT / "container" / "bin" / "woltspace"), "usage", *args],
                          env=env, capture_output=True, text=True, timeout=60)


def test_the_cli_is_a_thin_client_of_the_query_route(api, lodge):
    table = usage_cli(api, "table", "--by", "wolt", "--since", "3650d")
    assert table.returncode == 0, table.stderr
    assert table.stdout.startswith("last_collected ") and "\nalpha " in table.stdout
    data = json.loads(usage_cli(api, "summary", "--format", "json").stdout)
    assert data["totals"]["input_fresh"] == 5 and "limits" in data
    csv_out = usage_cli(api, "table", "--by", "day,provider", "--format", "csv").stdout
    assert csv_out.splitlines()[0].startswith("last_collected,history_starts,timezone,record")
    bad = usage_cli(api, "table", "--by", "bogus")
    assert (bad.returncode, bad.stderr.strip()) == (2, "woltspace usage: invalid by")
    unknown = usage_cli(api, "summary", "--wolt", "nobody")
    assert (unknown.returncode, unknown.stderr.strip()) == (2, "woltspace usage: unknown or ambiguous wolt")


def test_the_native_cli_hands_usage_to_the_bundled_client():
    from woltspace import cli
    parser = cli.build_parser()
    args = parser.parse_args(["usage", "table", "--by", "wolt"])
    assert args.func is cli._delegate and args.noun_args == ["table", "--by", "wolt"]


def test_operator_sessions_never_reach_the_api(tmp_path, monkeypatch, client):
    alpha = wolt(tmp_path, "alpha")
    bases = host(tmp_path)
    emit(bases["claude"] / "-home-op/op.jsonl", request("op1", cwd=tmp_path / "home", model="op-model"))
    emit(bases["claude"] / "-wolts-alpha/a.jsonl", request("a1", cwd=alpha))
    monkeypatch.setattr(usage_routes, "count_usage", lambda: count(tmp_path, bases=bases))
    for path in ("/api/usage/data", "/api/usage?by=session,model&format=csv", "/api/usage?command=limits"):
        assert "op-model" not in client.get(path).text and "-home-op" not in client.get(path).text
