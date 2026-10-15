"""The connector protocol, driven by the fake-transport fixture: every endpoint,
refusal and limit, against the lodge's own ASGI app."""

import asyncio
import json
import sys
import types
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))
sys.path.insert(0, str(ROOT / "connectors" / "fake"))

import server.app as server_app  # noqa: E402
from server import connector_api  # noqa: E402
from server.connectors import (  # noqa: E402
    ConnectorError,
    ConnectorStateUnreadable,
    ConnectorStore,
    Limits,
    NoConnectorBinding,
)
from woltspace_connector_fake import FakeConnector, FakeProvider, ProviderDown  # noqa: E402

LOCAL = "http://localhost:7777"
REMOTE = "https://owner.woltspace.test"
LIMITS = Limits(
    lease_seconds=60, max_attempts=5, retry_window_seconds=600,
    backoff_seconds=(1, 1, 1, 1), upload_ttl_seconds=600, upload_max_bytes=1000,
    upload_disk_cap_bytes=2500, file_retention_seconds=86400, text_max_bytes=64,
    request_max_bytes=65536, events_per_minute=20, uploads_per_hour=10,
    upload_bytes_per_hour=10_000, max_in_flight=3, lease_max_batch=10, max_queued=6,
    journal_max_bytes=400, outbound_file_max_bytes=1_000_000,
)


class Clock:
    def __init__(self):
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Lodge:
    """One lodge with a store the API serves, an enabled instance and a binding."""

    def __init__(self, root: Path, clock: Clock, limits: Limits = LIMITS):
        self.clock = clock
        self.store = ConnectorStore(root, clock=clock, limits=limits)
        self.routed: list = []
        self.failed: list = []

        def router(event):
            self.routed.append(event)
            return {"routed": "test"}

        self.store.router = router
        self.store.on_failure(lambda instance_id, delivery: self.failed.append((instance_id, delivery)))
        self.store.add_instance("tg-main", "telegram", "bot:1")
        self.binding = self.store.bind("tg-main", sender="u1", conversation="c1", default_wolt="softie")
        self.store.enable("tg-main")
        self.token = self.store.issue_token("tg-main")

    def enqueue(self, text="hello", conversation="c1", **kwargs) -> str:
        return self.store.enqueue(
            channel="telegram", account="bot:1", conversation=conversation, text=text,
            session="softie-1", reply_footer="session=softie-1", **kwargs,
        )

    def deliveries(self, instance_id="tg-main") -> dict:
        return {d["delivery_id"]: d for d in self.store.list_deliveries(instance_id)}

    def second_instance(self, instance_id="tg-new", account="bot:2", conversation="c9", enable=False) -> str:
        self.store.add_instance(instance_id, "telegram", account)
        self.store.bind(instance_id, sender="u9", conversation=conversation, default_wolt="softie")
        if enable:
            self.store.disable("tg-main")
            self.store.enable(instance_id)
        return self.store.issue_token(instance_id)

    def reload(self) -> "ConnectorStore":
        """A lodge restart: a new store over the same files, served by the API."""
        self.store = ConnectorStore(self.store.root, clock=self.clock, limits=self.store.limits)
        self.store.on_failure(lambda instance_id, delivery: self.failed.append((instance_id, delivery)))
        connector_api._store = self.store
        return self.store


@pytest.fixture
def lodge(tmp_path, monkeypatch):
    clock = Clock()
    lodge = Lodge(tmp_path / "connectors", clock)
    monkeypatch.setattr(connector_api, "CONNECTORS_DIR", lodge.store.root)
    monkeypatch.setattr(connector_api, "_store", lodge.store)
    monkeypatch.setattr(connector_api, "_POLL_INTERVAL", 0.01)
    return lodge


def _client(*, remote: bool = False) -> httpx.AsyncClient:
    peer = ("203.0.113.9", 4321) if remote else ("127.0.0.1", 4321)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server_app.app, client=peer),
        base_url=REMOTE if remote else LOCAL,
    )


def _remote_lodge(monkeypatch):
    monkeypatch.setattr(server_app, "load_access_settings", lambda _root: None)
    monkeypatch.setattr(server_app.tunnel_mgr, "_tunnel_hostname", "owner.woltspace.test")
    monkeypatch.setattr(server_app.tunnel_mgr, "_tunnel_domain", "woltspace.test")


def _principal(lodge):
    return lodge.store.authenticate(lodge.token)


# --------------------------------------------------------------- admission


@pytest.mark.parametrize("headers", [
    [],
    [("Authorization", "Basic abc")],
    [("Authorization", "Bearer")],
    [("Authorization", "Bearer wst_not_a_connector_token")],
    [("Authorization", "Bearer wct_unknown")],
    [("Authorization", "Bearer wct_a wct_b")],
])
async def test_anything_but_one_live_bearer_is_401_and_never_the_owner(lodge, headers):
    async with _client() as client:
        response = await client.post("/connector/v1/deliveries/lease", json={"max": 1}, headers=headers)
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"


async def test_two_bearer_headers_are_401_even_when_one_is_valid(lodge):
    headers = [("Authorization", f"Bearer {lodge.token}"), ("Authorization", f"Bearer {lodge.token}")]
    async with _client() as client:
        response = await client.post("/connector/v1/deliveries/lease", json={"max": 1}, headers=headers)
    assert response.status_code == 401


OWNER_ROUTES = [
    ("GET", "/sessions", None),
    ("GET", "/settings/access", None),
    ("POST", "/settings/access", {"team_domain": "x.cloudflareaccess.com", "lodge_aud": "a", "owner_email": "o@example.com"}),
    ("POST", "/notify", {"message": "hi"}),
    ("GET", "/tui", None),
    ("GET", "/apps", None),
    ("POST", "/sessions/redirect", {"from": "a", "to": "b"}),
]


@pytest.mark.parametrize("method, path, body", OWNER_ROUTES)
async def test_a_connector_bearer_opens_no_other_route_and_causes_no_side_effect(lodge, monkeypatch, method, path, body):
    """Finding 1: the credential works on /connector/v1 and nowhere else."""
    touched = []
    monkeypatch.setattr(server_app, "send_notification", lambda *a, **k: touched.append(("notify", a)))
    async with _client() as client:
        response = await client.request(
            method, path, json=body, headers={"Authorization": f"Bearer {lodge.token}"},
        )
    assert response.status_code == 401
    assert response.json() == {"error": "connector token not accepted here"}
    assert touched == []
    assert not (lodge.store.root.parent / "woltspace.json").exists()


@pytest.mark.parametrize("headers", [
    [("Authorization", "Bearer wct_unknown")],
    [("Authorization", "Basic abc")],
    [("Authorization", "Bearer")],
    [("Authorization", "bearer wct_lowercase")],
    [("Authorization", "Bearer wct_a wct_b")],
    [("Authorization", "Bearer wst_session_shaped")],
    [("Authorization", "Token x")],
    [("Authorization", "")],
])
async def test_malformed_or_duplicate_authorization_never_becomes_the_owner(lodge, headers):
    """Finding 1 (b): no happy-path parser; any Authorization outside the API is 401."""
    async with _client() as client:
        single = await client.get("/sessions", headers=headers)
        twice = await client.get(
            "/sessions", headers=headers + [("Authorization", f"Bearer {lodge.token}")],
        )
        plain = await client.get("/sessions")
    assert single.status_code == 401
    assert twice.status_code == 401
    # Token-less local compatibility (row 4) is untouched.
    assert plain.status_code == 200


async def test_websocket_with_a_connector_bearer_is_closed_before_accept(lodge):
    client = TestClient(server_app.app, base_url=LOCAL)
    local = {"host": "localhost:7777", "origin": "http://localhost:7777"}
    for value in (f"Bearer {lodge.token}", "Bearer wct_unknown", "Basic abc", "Bearer"):
        with pytest.raises(WebSocketDisconnect) as refused:
            with client.websocket_connect("/livereload", headers={**local, "Authorization": value}):
                pass
        assert refused.value.code == 1008
    # Two Authorization headers, one of them valid: refused at the ASGI edge.
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "websocket.connect"}

    await server_app.app({
        "type": "websocket", "asgi": {"version": "3.0"}, "path": "/livereload", "raw_path": b"/livereload",
        "query_string": b"", "scheme": "ws", "server": ("localhost", 7777), "client": ("127.0.0.1", 1),
        "headers": [
            (b"host", b"localhost:7777"), (b"origin", b"http://localhost:7777"),
            (b"authorization", f"Bearer {lodge.token}".encode()), (b"authorization", b"Bearer x"),
        ],
    }, receive, send)
    assert sent == [{"type": "websocket.close", "code": 1008}]
    # Token-less, the same socket opens as before.
    with client.websocket_connect("/livereload", headers=local):
        pass


async def test_host_and_origin_checks_stay_independent_of_the_bearer_guard(lodge, monkeypatch):
    monkeypatch.setattr(server_app, "load_access_settings", lambda _root: None)
    sent = []
    monkeypatch.setattr(server_app, "send_notification", lambda *a, **k: sent.append(a))
    async with _client(remote=True) as client:
        bad_host = await client.get("/sessions")
        bad_host_with_bearer = await client.get("/sessions", headers={"Authorization": f"Bearer {lodge.token}"})
    assert bad_host.status_code == 403
    assert bad_host_with_bearer.status_code == 401
    async with _client() as client:
        cross_origin = await client.post(
            "/notify", json={"message": "hi"}, headers={"Origin": "https://evil.example"},
        )
    assert cross_origin.status_code == 403
    assert sent == []


async def test_revoked_and_rotated_tokens_are_refused(lodge):
    async with _client() as client:
        old = FakeConnector(client, lodge.token)
        assert (await old.lease()).status_code == 200
        new_token = lodge.store.issue_token("tg-main")
        assert (await old.lease()).status_code == 401
        new = FakeConnector(client, new_token)
        assert (await new.lease()).status_code == 200
        lodge.store.revoke_tokens("tg-main")
        assert (await new.lease()).status_code == 401
        assert (await new.send_event("e1", "u1", "c1", "hi")).status_code == 401


async def test_valid_token_from_a_remote_peer_is_403_and_a_bad_one_401(lodge, monkeypatch):
    _remote_lodge(monkeypatch)
    async with _client(remote=True) as client:
        valid = FakeConnector(client, lodge.token)
        response = await valid.lease()
        assert response.status_code == 403
        assert response.json() == {"error": "connector tokens are local-only"}
        assert (await FakeConnector(client, "wct_nope").lease()).status_code == 401
    # The same requests from the owning machine work.
    async with _client() as client:
        assert (await FakeConnector(client, lodge.token).lease()).status_code == 200


async def test_corrupt_registry_refuses_every_token_and_corrupt_queue_fails_closed(lodge):
    lodge.store.registry_path.write_text("{not json")
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        assert (await connector.lease()).status_code == 401
    with pytest.raises(ConnectorStateUnreadable):
        lodge.store.list_instances()
    with pytest.raises(ConnectorStateUnreadable):
        lodge.store.add_instance("x", "telegram", "bot:x")
    # A readable registry with a corrupt queue: the token is believed, the
    # queue is not served from a guess.
    lodge.store.registry_path.unlink()
    fresh = Lodge(lodge.store.root / "fresh", lodge.clock)
    connector_api._store, connector_api.CONNECTORS_DIR = fresh.store, fresh.store.root
    fresh.enqueue()
    (fresh.store.root / "queues" / "tg-main.json").write_text("[]")
    async with _client() as client:
        connector = FakeConnector(client, fresh.token)
        assert (await connector.lease()).status_code == 503
        assert (await connector.send_event("e1", "u1", "c1", "hi")).status_code == 503
        assert (await connector.upload(b"x")).status_code == 503


# ------------------------------------------------- stale principals (finding 2)


def test_a_principal_captured_before_a_revoke_is_refused_inside_the_lock(lodge, tmp_path):
    """Astre's probe: authenticate, revoke, then act with the old snapshot."""
    who = _principal(lodge)
    lodge.enqueue()
    lodge.store.revoke_tokens("tg-main")
    event = {"event_id": "e1", "sender": {"provider_id": "u1"}, "conversation": {"provider_id": "c1"}, "text": "hi"}
    for call in (
        lambda: lodge.store.accept_event(who, event),
        lambda: lodge.store.lease(who, 1),
        lambda: lodge.store.begin_upload(who),
        lambda: lodge.store.ack(who, "0" * 32, {"attempt_id": "1" * 32, "ok": True}),
        lambda: lodge.store.open_file(who, "0" * 32, "1" * 32),
    ):
        with pytest.raises(ConnectorError) as refused:
            call()
        assert refused.value.status == 401
    assert lodge.routed == []
    assert lodge.deliveries()[next(iter(lodge.deliveries()))]["status"] == "queued"
    # A rotation is just as final for the old snapshot, and so is removal.
    stale = _principal(Lodge(tmp_path / "other", lodge.clock))
    with pytest.raises(ConnectorError):
        lodge.store.lease(stale, 1)


async def test_a_request_whose_body_arrives_after_a_revoke_or_rotation_is_refused(lodge):
    event = json.dumps({
        "event_id": "slow", "sender": {"provider_id": "u1"}, "conversation": {"provider_id": "c1"}, "text": "hi",
    }).encode()

    def body(after_first):
        async def stream():
            yield event[:10]
            after_first()
            yield event[10:]
        return stream()

    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        revoked = await client.post(
            "/connector/v1/events", content=body(lambda: lodge.store.revoke_tokens("tg-main")),
            headers=connector.headers,
        )
        assert revoked.status_code == 401
        token = lodge.store.issue_token("tg-main")
        connector = FakeConnector(client, token)
        rotated = await client.post(
            "/connector/v1/events", content=body(lambda: lodge.store.issue_token("tg-main")),
            headers=connector.headers,
        )
        assert rotated.status_code == 401
        token = lodge.store.issue_token("tg-main")
        connector = FakeConnector(client, token)
        disabled = await client.post(
            "/connector/v1/events", content=body(lambda: lodge.store.disable("tg-main")),
            headers=connector.headers,
        )
        assert disabled.json()["reason"] == "instance_disabled"
        lodge.store.enable("tg-main")
        removed = await client.post(
            "/connector/v1/events", content=body(lambda: lodge.store.remove_instance("tg-main")),
            headers=connector.headers,
        )
        assert removed.status_code == 401
    assert lodge.routed == []


async def test_an_upload_whose_stream_outlives_its_token_leaves_nothing_behind(lodge):
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)

        async def stream():
            yield b"first"
            lodge.store.revoke_tokens("tg-main")
            yield b"second"

        response = await client.post(
            "/connector/v1/uploads", content=stream(), headers=connector.headers,
        )
    assert response.status_code == 401
    uploads = lodge.store.root / "uploads" / "tg-main"
    assert not uploads.exists() or list(uploads.iterdir()) == []
    queue = json.loads((lodge.store.root / "queues" / "tg-main.json").read_text())
    assert queue["uploads"] == {}


async def test_a_file_stream_is_cut_when_its_lease_expires_mid_transfer(lodge, tmp_path, monkeypatch):
    big = tmp_path / "big.bin"
    big.write_bytes(b"z" * (connector_api._CHUNK * 3))
    lodge.enqueue(attachment=big)
    allowed = lodge.store.stream_allowed
    calls = []

    def expiring(who, file_id, attempt_id):
        calls.append(1)
        if len(calls) == 2:
            lodge.clock.advance(LIMITS.lease_seconds + 1)
        return allowed(who, file_id, attempt_id)

    monkeypatch.setattr(lodge.store, "stream_allowed", expiring)
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        leased = (await connector.lease()).json()["deliveries"][0]
        received = b""
        try:
            async with client.stream(
                "GET", f"/connector/v1/files/{leased['attachments'][0]['file_id']}",
                params={"attempt_id": leased["attempt_id"]}, headers=connector.headers,
            ) as response:
                async for chunk in response.aiter_bytes():
                    received += chunk
        except httpx.HTTPError:
            pass
    assert len(received) == connector_api._CHUNK


# ------------------------------------------------------------------ events


async def test_bound_sender_is_accepted_once_and_routed_once(lodge):
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        first = await connector.send_event("tg:1", "u1", "c1", "hello", display="jer")
        again = await connector.send_event("tg:1", "u1", "c1", "hello", display="jerpint")
    assert first.status_code == 200
    assert first.json()["accepted"] is True
    assert first.json()["wolt"] == "softie"
    assert first.json()["routed"] == "test"
    assert again.json() == first.json()
    assert len(lodge.routed) == 1
    event = lodge.routed[0]
    assert (event.account, event.event_id, event.text) == ("bot:1", "tg:1", "hello")
    assert event.binding["binding_id"] == lodge.binding["binding_id"]


async def test_same_event_id_with_another_payload_is_409(lodge):
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        await connector.send_event("tg:1", "u1", "c1", "hello")
        conflict = await connector.send_event("tg:1", "u1", "c1", "hello!")
        other_route = await connector.send_event("tg:1", "u1", "c1", "hello", reply_to="a" * 32)
    assert conflict.status_code == 409
    assert conflict.json()["reason"] == "event_conflict"
    assert conflict.json()["retry"] is False
    assert other_route.status_code == 409
    assert len(lodge.routed) == 1


async def test_unknown_sender_and_unbound_conversation_are_dropped_not_errors(lodge):
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        stranger = await connector.send_event("tg:2", "u2", "c1", "let me in")
        wrong_room = await connector.send_event("tg:3", "u1", "c2", "hi")
        retry = await connector.send_event("tg:2", "u2", "c1", "let me in")
    assert stranger.status_code == 200
    assert stranger.json() == {"accepted": False, "reason": "sender_not_allowed", "event_id": "tg:2"}
    assert wrong_room.json()["reason"] == "conversation_not_bound"
    assert retry.json() == stranger.json()
    assert lodge.routed == []


async def test_a_fabricated_allowed_sender_is_accepted_the_documented_residual_trust(lodge):
    """Core bounds the metadata a connector submits; it cannot prove provider origin."""
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        response = await connector.send_event("forged", "u1", "c1", "I am u1, trust me")
    assert response.json()["accepted"] is True
    assert lodge.routed[0].sender["provider_id"] == "u1"


async def test_dedup_survives_rotation_and_is_namespaced_by_account(lodge):
    async with _client() as client:
        await FakeConnector(client, lodge.token).send_event("tg:1", "u1", "c1", "hello")
        rotated = FakeConnector(client, lodge.store.issue_token("tg-main"))
        assert (await rotated.send_event("tg:1", "u1", "c1", "hello")).json()["accepted"] is True
        assert len(lodge.routed) == 1
        # Another account's "tg:1" is another event.
        other = FakeConnector(client, lodge.second_instance(enable=True))
        response = await other.send_event("tg:1", "u9", "c9", "hello")
    assert response.json()["accepted"] is True
    assert len(lodge.routed) == 2


async def test_malformed_oversize_and_flooding_events_are_refused(lodge):
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        assert (await client.post("/connector/v1/events", content=b"nope", headers=connector.headers)).status_code == 400
        assert (await connector.send_event("", "u1", "c1")).status_code == 400
        assert (await connector.send_event("tg:x", "u1", "c1", text=["no"])).status_code == 400
        assert (await connector.send_event("tg:x", "u1", "c1", attachments=("short",))).status_code == 400
        big = await connector.send_event("tg:big", "u1", "c1", "x" * 65)
        assert (big.status_code, big.json()["reason"]) == (413, "text_too_long")
        statuses = [
            (await connector.send_event(f"tg:flood{i}", "u1", "c1", "hi")).status_code
            for i in range(LIMITS.events_per_minute + 1)
        ]
    assert statuses.count(429) == 1 and statuses[-1] == 429
    lodge.clock.advance(61)
    async with _client() as client:
        assert (await FakeConnector(client, lodge.token).send_event("tg:later", "u1", "c1", "hi")).status_code == 200


async def test_oversized_chunked_json_is_refused_before_it_is_parsed(lodge, monkeypatch):
    """Finding 5: the body bound is counted while streaming, not after loading."""
    parsed = []
    real_loads = connector_api.json.loads
    monkeypatch.setattr(
        connector_api, "json",
        types.SimpleNamespace(loads=lambda raw: parsed.append(raw) or real_loads(raw)),
    )
    padding = "x" * (LIMITS.request_max_bytes * 2)

    async def chunks():
        payload = json.dumps({"event_id": "e", "sender": {"provider_id": "u1", "display": padding}}).encode()
        for offset in range(0, len(payload), 512):
            yield payload[offset:offset + 512]

    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        response = await client.post("/connector/v1/events", content=chunks(), headers=connector.headers)
        assert (response.status_code, response.json()["reason"]) == (413, "request_too_large")
        assert parsed == []
        lease = await client.post(
            "/connector/v1/deliveries/lease", content=b"{" + b" " * LIMITS.request_max_bytes + b"}",
            headers=connector.headers,
        )
    assert lease.status_code == 413
    assert lodge.routed == []


async def test_display_and_unknown_fields_are_bounded(lodge):
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        long_display = await connector.send_event("tg:1", "u1", "c1", "hi", display="d" * 257)
        unknown_top = await connector.send_event("tg:2", "u1", "c1", "hi", surprise="x")
        unknown_nested = await client.post("/connector/v1/events", json={
            "event_id": "tg:3", "sender": {"provider_id": "u1", "role": "owner"},
            "conversation": {"provider_id": "c1"}, "text": "hi",
        }, headers=connector.headers)
        unknown_ack = await client.post(
            "/connector/v1/deliveries/" + "0" * 32 + "/ack",
            json={"attempt_id": "1" * 32, "ok": True, "extra": 1}, headers=connector.headers,
        )
        fine = await connector.send_event("tg:4", "u1", "c1", "hi", display="d" * 256)
    assert long_display.status_code == unknown_top.status_code == unknown_nested.status_code == 400
    assert unknown_ack.status_code == 400
    assert fine.json()["accepted"] is True
    assert len(lodge.routed) == 1


async def test_lease_wait_must_be_finite(lodge):
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        for raw in (b'{"max": 1, "wait": NaN}', b'{"max": 1, "wait": Infinity}', b'{"max": 1, "wait": -1}',
                    b'{"max": 1, "wait": "5"}', b'{"max": 1, "wait": 1, "poll": true}'):
            response = await client.post("/connector/v1/deliveries/lease", content=raw, headers=connector.headers)
            assert (response.status_code, response.json()["reason"]) == (400, "malformed_lease"), raw
        assert (await connector.lease(1, wait=0.5)).status_code == 200


async def test_disabled_instance_drops_events_for_good_and_hands_out_nothing(lodge):
    """Interpretation (d): disable pauses outbound, it does not buffer inbound.

    An event that reaches a disabled instance is dropped, the connector is
    told `paused` and stops forwarding, and the same event id replays the
    recorded drop after re-enable: nothing received while disabled is
    delivered later.
    """
    lodge.enqueue()
    lodge.store.disable("tg-main")
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        event = await connector.send_event("tg:1", "u1", "c1", "hi")
        lease = await connector.lease()
        assert event.json() == {"accepted": False, "reason": "instance_disabled", "event_id": "tg:1"}
        assert lease.json() == {"deliveries": [], "paused": True}
        lodge.store.enable("tg-main")
        replay = await connector.send_event("tg:1", "u1", "c1", "hi")
        fresh = await connector.send_event("tg:2", "u1", "c1", "hi")
    assert replay.json() == event.json()
    assert fresh.json()["accepted"] is True
    assert [e.event_id for e in lodge.routed] == ["tg:2"]


async def test_reply_to_resolves_only_through_the_events_exact_binding(lodge):
    """Finding 3: the same conversation is not the same binding."""
    other_sender = lodge.store.bind("tg-main", sender="u2", conversation="c1", default_wolt="softie")
    lodge.store.bind("tg-main", sender="u1", conversation="c2", default_wolt="softie")
    own = lodge.enqueue(conversation="c1", binding_id=lodge.binding["binding_id"])
    theirs = lodge.enqueue(conversation="c1", binding_id=other_sender["binding_id"])
    elsewhere = lodge.enqueue(conversation="c2")
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        same_conversation = await connector.send_event("tg:1", "u1", "c1", "re", reply_to=theirs)
        cross = await connector.send_event("tg:2", "u1", "c1", "re", reply_to=elsewhere)
        missing = await connector.send_event("tg:3", "u1", "c1", "re", reply_to="f" * 32)
        good = await connector.send_event("tg:4", "u1", "c1", "re", reply_to=own)
    assert same_conversation.json() == {"accepted": False, "reason": "reply_to_out_of_binding", "event_id": "tg:1"}
    assert cross.json()["reason"] == "reply_to_out_of_binding"
    assert missing.json()["reason"] == "reply_to_out_of_binding"
    assert good.json()["accepted"] is True
    assert good.json()["session"] == "softie-1"
    assert lodge.routed[0].reply_to_session == "softie-1"


async def test_a_reply_carries_core_s_own_record_of_what_it_replies_to(lodge):
    """Quoted from the delivery core sent, by delivery id or by the provider's message id."""
    plan = lodge.enqueue("the plan is ready", conversation="c1", binding_id=lodge.binding["binding_id"])
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        by_delivery = await connector.send_event("tg:1", "u1", "c1", "ship it", reply_to=plan)
        leased = (await connector.lease()).json()["deliveries"]
        attempt = next(d["attempt_id"] for d in leased if d["delivery_id"] == plan)
        await connector.ack(plan, attempt, provider_message_id="m-7")
        by_message = await connector.send_event("tg:2", "u1", "c1", "ok", reply_to={"provider_message_id": "m-7"})
    assert by_delivery.json()["accepted"] is True and by_message.json()["accepted"] is True
    quote = {"text": "the plan is ready", "file": None, "voice": False}
    assert [e.reply_quote for e in lodge.routed] == [quote, quote]


# ----------------------------------------------------------------- uploads


async def test_upload_is_claimed_by_exactly_one_accepted_event(lodge):
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        upload = await connector.upload(b"voice bytes", name="../note.ogg", chunk=4)
        assert upload.status_code == 200
        upload_id = upload.json()["upload_id"]
        first = await connector.send_event("tg:1", "u1", "c1", "", attachments=(upload_id,))
        second = await connector.send_event("tg:2", "u1", "c1", "", attachments=(upload_id,))
        retry = await connector.send_event("tg:1", "u1", "c1", "", attachments=(upload_id,))
    assert first.json()["accepted"] is True
    assert second.json()["reason"] == "upload_consumed"
    # The identical retry gets its recorded answer before any handle question.
    assert retry.json() == first.json()
    attachment = lodge.routed[0].attachments[0]
    assert attachment["name"] == "note.ogg"
    assert Path(attachment["path"]).read_bytes() == b"voice bytes"
    assert Path(attachment["path"]).parent == lodge.store.root / "uploads" / "tg-main"


async def test_upload_over_its_cap_is_cut_as_it_arrives(lodge):
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        response = await connector.upload(b"x" * (LIMITS.upload_max_bytes + 1), chunk=100)
        empty = await connector.upload(b"")
    assert (response.status_code, response.json()["reason"], response.json()["retry"]) == (413, "upload_too_large", False)
    assert empty.status_code == 400
    assert list((lodge.store.root / "uploads" / "tg-main").iterdir()) == []


async def test_upload_quota_is_counted_before_the_sender_check_and_only_own_handles_are_released(lodge):
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        ids = []
        for _ in range(2):
            response = await connector.upload(b"x" * 1000)
            assert response.status_code == 200
            ids.append(response.json()["upload_id"])
        # 2000 of 2500 bytes held: the next 1000 trips the cap while arriving,
        # without any event having named a sender. 429 says: try again later.
        over = await connector.upload(b"x" * 1000, chunk=200)
        assert (over.status_code, over.json()["reason"], over.json()["retry"]) == (429, "upload_quota", True)
        # A bound sender claims one; an unbound sender's drop names both but
        # may only release the one nobody holds.
        claimed = await connector.send_event("tg:0", "u1", "c1", "", attachments=(ids[0],))
        assert claimed.json()["accepted"] is True
        dropped = await connector.send_event("tg:1", "u2", "c1", "", attachments=tuple(ids))
        assert dropped.json()["reason"] == "sender_not_allowed"
        assert sorted(p.name for p in (lodge.store.root / "uploads" / "tg-main").iterdir()) == [ids[0]]
        assert (await connector.upload(b"x" * 1000)).status_code == 200
        # Claimed bytes count until the router releases them.
        full = await connector.upload(b"x" * 1000, chunk=500)
        assert full.status_code == 429
        lodge.store.release_uploads("tg-main", [ids[0]])
        assert (await connector.upload(b"x" * 1000)).status_code == 200


def test_overlapping_uploads_share_one_cap_and_one_hourly_window(lodge):
    """Astre's probe: two 7-byte uploads under a 10-byte cap, begun together."""
    store = ConnectorStore(lodge.store.root, clock=lodge.clock, limits=Limits(
        upload_disk_cap_bytes=10, upload_max_bytes=1000, uploads_per_hour=3, upload_bytes_per_hour=100,
    ))
    who = store.authenticate(lodge.token)
    a_id, a_part = store.begin_upload(who)
    b_id, b_part = store.begin_upload(who)
    store.account_upload_bytes(who, a_id, 7)
    with pytest.raises(ConnectorError) as refused:
        store.account_upload_bytes(who, b_id, 7)
    assert (refused.value.status, refused.value.reason, refused.value.retry) == (429, "upload_quota", True)
    store.abort_upload(who, b_id, b_part)
    a_part.write_bytes(b"x" * 7)
    store.commit_upload(who, a_id, a_part, "a")
    queue = json.loads((store.root / "queues" / "tg-main.json").read_text())
    assert sum(h["size"] for h in queue["uploads"].values()) == 7
    # A third begin sees the committed 7 and the slot count: two begins already used slots.
    c_id, c_part = store.begin_upload(who)
    with pytest.raises(ConnectorError, match="upload_quota"):
        store.account_upload_bytes(who, c_id, 4)
    store.abort_upload(who, c_id, c_part)
    with pytest.raises(ConnectorError, match="uploads_rate_limited"):
        store.begin_upload(who)


def test_abandoned_upload_parts_are_reclaimed_after_a_restart(lodge):
    """A process died mid-upload: its reservation and bytes are freed on reload."""
    who = _principal(lodge)
    dead_id, dead_part = lodge.store.begin_upload(who)
    dead_part.write_bytes(b"half")
    lodge.store.account_upload_bytes(who, dead_id, 900)
    queue_path = lodge.store.root / "queues" / "tg-main.json"
    queue = json.loads(queue_path.read_text())
    queue["uploads"][dead_id]["pid"] = 2 ** 22 + 7  # not a live process
    queue_path.write_text(json.dumps(queue))
    (lodge.store.root / "uploads" / "tg-main" / ".deadbeef.part").write_bytes(b"orphan")
    # Nothing reclaims it on its own in the dead process's accounting...
    live_id, live_part = lodge.store.begin_upload(who)
    live_part.write_bytes(b"x" * 900)
    lodge.store.account_upload_bytes(who, live_id, 900)
    # ...so a reload, which sweeps dead reservations, is what frees the room.
    reloaded = lodge.reload()
    fresh_id, fresh_part = reloaded.begin_upload(who)
    fresh_part.write_bytes(b"x" * 900)
    reloaded.account_upload_bytes(who, fresh_id, 900)
    assert not dead_part.exists()
    assert sorted(p.name for p in (reloaded.root / "uploads" / "tg-main").iterdir()) == sorted(
        [f".{live_id}.part", f".{fresh_id}.part"],
    )
    # The two live reservations are still coherent: a third 900 is over the cap.
    third_id, third_part = reloaded.begin_upload(who)
    with pytest.raises(ConnectorError, match="upload_quota"):
        reloaded.account_upload_bytes(who, third_id, 900)
    for upload_id, part in ((third_id, third_part), (fresh_id, fresh_part), (live_id, live_part)):
        reloaded.abort_upload(who, upload_id, part)


async def test_expired_upload_and_upload_rate_limits_are_refused(lodge):
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        upload_id = (await connector.upload(b"x")).json()["upload_id"]
        lodge.clock.advance(LIMITS.upload_ttl_seconds + 1)
        expired = await connector.send_event("tg:1", "u1", "c1", "", attachments=(upload_id,))
        assert expired.json()["reason"] == "upload_expired"
        for _ in range(LIMITS.uploads_per_hour - 1):
            assert (await connector.upload(b"y")).status_code == 200
        limited = await connector.upload(b"y")
    assert (limited.status_code, limited.json()["reason"], limited.json()["retry"]) == (429, "uploads_rate_limited", True)


# -------------------------------------------------------------- deliveries


def test_enqueue_targets_the_enabled_instance_and_exactly_one_binding(lodge):
    """Finding 3: never a disabled older instance, never an ambiguous conversation."""
    with pytest.raises(NoConnectorBinding):
        lodge.store.enqueue(channel="telegram", account="bot:2", conversation="c1", text="x")
    with pytest.raises(NoConnectorBinding):
        lodge.store.enqueue(channel="telegram", account="bot:1", conversation="c2", text="x")
    with pytest.raises(NoConnectorBinding):
        lodge.store.enqueue(channel="slack", account="bot:1", conversation="c1", text="x")
    # Two senders bound in one conversation: the caller must say which.
    second = lodge.store.bind("tg-main", sender="u2", conversation="c1", default_wolt="softie")
    with pytest.raises(NoConnectorBinding, match="2 bindings"):
        lodge.enqueue()
    chosen = lodge.enqueue(binding_id=second["binding_id"])
    assert lodge.deliveries()[chosen]["binding_id"] == second["binding_id"]
    with pytest.raises(NoConnectorBinding):
        lodge.enqueue(binding_id="not-a-binding")
    # A replacement with the same channel and account: only the enabled one receives.
    lodge.store.disable("tg-main")
    lodge.store.add_instance("tg-replacement", "telegram", "bot:1")
    lodge.store.bind("tg-replacement", sender="u1", conversation="c1", default_wolt="softie")
    lodge.store.enable("tg-replacement")
    routed = lodge.enqueue()
    assert routed in lodge.deliveries("tg-replacement")
    assert routed not in lodge.deliveries("tg-main")
    lodge.store.disable("tg-replacement")
    with pytest.raises(NoConnectorBinding, match="no enabled"):
        lodge.enqueue()


async def test_lease_send_ack_delivers_with_its_attachment(lodge, tmp_path):
    note = tmp_path / "note.ogg"
    note.write_bytes(b"opus" * 10)
    delivery_id = lodge.enqueue("listen", attachment=note, attachment_kind="voice")
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        results = await connector.run_once()
    assert results == [{"ok": True, "status": "delivered", "failed_attempts": 0}]
    sent = connector.provider.sent[0]
    assert (sent["delivery_id"], sent["text"], sent["files"]) == (delivery_id, "listen", [b"opus" * 10])
    assert sent["conversation"] == {"provider_id": "c1", "thread": None}
    assert sent["reply_footer"] == "session=softie-1"
    record = lodge.deliveries()[delivery_id]
    assert (record["status"], record["provider_message_id"]) == ("delivered", "msg-1")
    assert list((lodge.store.root / "files").iterdir()) == []
    assert lodge.failed == []


async def test_lodge_restart_and_token_rotation_keep_the_queue(lodge):
    delivery_id = lodge.enqueue()
    restarted = lodge.reload()
    async with _client() as client:
        survivor = FakeConnector(client, lodge.token)
        leased = (await survivor.lease()).json()["deliveries"]
        assert [d["delivery_id"] for d in leased] == [delivery_id]
        # The runner is relaunched: a new generation. The old lease is released
        # for the new process; the old token can no longer ack it.
        new_token = restarted.issue_token("tg-main")
        stale = await survivor.ack(delivery_id, leased[0]["attempt_id"], provider_message_id="m")
        assert stale.status_code == 401
        relaunched = FakeConnector(client, new_token)
        again = (await relaunched.lease()).json()["deliveries"]
        assert [d["delivery_id"] for d in again] == [delivery_id]
        assert again[0]["attempt_id"] != leased[0]["attempt_id"]
        assert (await relaunched.ack(delivery_id, again[0]["attempt_id"], provider_message_id="m")).json()["status"] == "delivered"
    assert restarted.list_deliveries("tg-main")[0]["failed_attempts"] == 0


async def test_disable_pauses_the_retry_clock_and_enable_resumes_it(lodge):
    delivery_id = lodge.enqueue()
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        connector.provider.failures.append(ProviderDown("timeout"))
        assert (await connector.run_once())[0]["status"] == "queued"
        lodge.store.disable("tg-main")
        lodge.clock.advance(LIMITS.retry_window_seconds * 3)
        assert (await connector.lease()).json()["paused"] is True
        lodge.store.enable("tg-main")
        lodge.clock.advance(2)
        results = await connector.run_once()
    assert results == [{"ok": True, "status": "delivered", "failed_attempts": 1}]
    assert lodge.deliveries()[delivery_id]["status"] == "delivered"


def test_enable_disable_and_rotation_are_single_write_transitions(lodge, monkeypatch):
    """Finding 6: no second file to crash between; a failed write changes nothing."""
    delivery_id = lodge.enqueue()
    who = _principal(lodge)
    leased = lodge.store.lease(who, 1)["deliveries"][0]
    queue_path = lodge.store.root / "queues" / "tg-main.json"
    queue_before = queue_path.read_text()
    registry_before = lodge.store.registry_path.read_text()

    def crash(_path, _data):
        raise OSError("disk gone")

    with monkeypatch.context() as patched:
        patched.setattr(ConnectorStore, "_write_json", staticmethod(crash))
        for transition in (
            lambda: lodge.store.disable("tg-main"),
            lambda: lodge.store.issue_token("tg-main"),
            lambda: lodge.store.enable("tg-main"),
        ):
            with pytest.raises(OSError):
                transition()
    assert lodge.store.registry_path.read_text() == registry_before
    assert queue_path.read_text() == queue_before

    # Each transition is one registry write; the queue file is not touched.
    lodge.store.disable("tg-main")
    lodge.clock.advance(1000)
    new_token = lodge.store.issue_token("tg-main")
    lodge.store.enable("tg-main")
    assert queue_path.read_text() == queue_before
    # A reload derives everything: the old lease is released without a failed
    # attempt, and the paused second did not count against the window.
    reloaded = lodge.reload()
    record = {d["delivery_id"]: d for d in reloaded.list_deliveries("tg-main")}[delivery_id]
    assert (record["status"], record["failed_attempts"]) == ("queued", 0)
    fresh = reloaded.authenticate(new_token)
    again = reloaded.lease(fresh, 1)["deliveries"][0]
    assert again["attempt_id"] != leased["attempt_id"]
    assert reloaded.ack(fresh, delivery_id, {"attempt_id": again["attempt_id"], "ok": True})["status"] == "delivered"


def test_remove_cancels_the_queue_revokes_and_detaches(lodge):
    delivery_id = lodge.enqueue()
    outcome = lodge.store.remove_instance("tg-main")
    assert outcome == {"cancelled": 1, "transferred": 0}
    assert lodge.store.list_instances() == [] and lodge.store.list_bindings() == []
    assert not (lodge.store.root / "queues" / "tg-main.json").exists()
    assert lodge.failed[0][1]["status"] == "cancelled"
    record = json.loads((lodge.store.root / "failures.jsonl").read_text())
    assert (record["delivery_id"], record["status"], record["delivered"]) == (delivery_id, "cancelled", False)
    assert lodge.store.authenticate(lodge.token) is None


def test_remove_interrupted_after_its_registry_write_is_finished_on_reload(lodge, monkeypatch):
    """Finding 6: the orphan queue is cancelled once, by recovery, never twice."""
    delivery_id = lodge.enqueue()

    def crash(self, instance_id, tx):
        raise OSError("crashed mid-removal")

    with monkeypatch.context() as patched:
        patched.setattr(ConnectorStore, "_cancel_orphan_queue", crash)
        with pytest.raises(OSError):
            lodge.store.remove_instance("tg-main")
    assert lodge.store.list_instances() == []
    assert (lodge.store.root / "queues" / "tg-main.json").exists()
    assert lodge.failed == []
    reloaded = lodge.reload()
    assert reloaded.recover() == 1
    assert not (reloaded.root / "queues" / "tg-main.json").exists()
    assert [f[1]["delivery_id"] for f in lodge.failed] == [delivery_id]
    assert reloaded.recover() == 0
    assert len((reloaded.root / "failures.jsonl").read_text().splitlines()) == 1
    # The API's store does the same on first load.
    connector_api._store = None
    assert connector_api.get_store().root == reloaded.root


async def test_unbind_cancels_queued_deliveries_and_ends_in_flight_authority(lodge, tmp_path):
    """Finding 3 and (c): withdrawing the grant withdraws the queue, the lease and the file."""
    note = tmp_path / "n.ogg"
    note.write_bytes(b"audio")
    in_flight = lodge.enqueue(attachment=note)
    queued = lodge.enqueue("second")
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        leased = (await connector.lease(1)).json()["deliveries"][0]
        assert leased["delivery_id"] == in_flight
        file_id, attempt_id = leased["attachments"][0]["file_id"], leased["attempt_id"]
        assert (await connector.fetch_file(file_id, attempt_id)).status_code == 200
        assert lodge.store.unbind("tg-main", sender="u1", conversation="c1") is True
        assert (await connector.lease()).json()["deliveries"] == []
        assert (await connector.fetch_file(file_id, attempt_id)).status_code == 404
        ack = await connector.ack(in_flight, attempt_id, provider_message_id="m")
        assert (ack.status_code, ack.json()["reason"]) == (409, "stale_attempt")
    records = lodge.deliveries()
    assert {records[in_flight]["status"], records[queued]["status"]} == {"cancelled"}
    assert records[queued]["error"] == "binding removed"
    assert sorted(f[1]["delivery_id"] for f in lodge.failed) == sorted([in_flight, queued])
    assert list((lodge.store.root / "files").iterdir()) == []
    # A withdrawn grant is gone: binding the sender again is a new grant.
    again = lodge.store.bind("tg-main", sender="u1", conversation="c1", default_wolt="softie")
    assert again["binding_id"] != lodge.binding["binding_id"]
    # Changing a standing grant in place keeps its id, so its deliveries stand.
    changed = lodge.store.bind("tg-main", sender="u1", conversation="c1", default_wolt="softie", may_wake=True)
    assert (changed["binding_id"], changed["may_wake"]) == (again["binding_id"], True)
    assert len(lodge.store.list_bindings("tg-main")) == 1


async def test_lease_expiry_redelivers_and_the_stale_attempt_cannot_ack(lodge):
    delivery_id = lodge.enqueue()
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        first = (await connector.lease()).json()["deliveries"][0]
        assert (await connector.lease()).json()["deliveries"] == []
        lodge.clock.advance(LIMITS.lease_seconds + 1)
        second = (await connector.lease()).json()["deliveries"][0]
        assert (second["delivery_id"], second["attempt_id"] != first["attempt_id"]) == (delivery_id, True)
        stale = await connector.ack(delivery_id, first["attempt_id"], provider_message_id="m1")
        assert (stale.status_code, stale.json()["reason"]) == (409, "stale_attempt")
        good = await connector.ack(delivery_id, second["attempt_id"], provider_message_id="m2")
    assert good.json() == {"ok": True, "status": "delivered", "failed_attempts": 1}


async def test_cross_instance_and_cross_generation_acks_are_refused(lodge):
    delivery_id = lodge.enqueue()
    other_token = lodge.second_instance()
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        leased = (await connector.lease()).json()["deliveries"][0]
        lodge.store.disable("tg-main")
        lodge.store.enable("tg-new")
        foreign = await FakeConnector(client, other_token).ack(delivery_id, leased["attempt_id"], provider_message_id="m")
        assert (foreign.status_code, foreign.json()["reason"]) == (409, "stale_attempt")
        lodge.store.disable("tg-new")
        lodge.store.enable("tg-main")
        next_generation = FakeConnector(client, lodge.store.issue_token("tg-main"))
        cross_generation = await next_generation.ack(delivery_id, leased["attempt_id"], provider_message_id="m")
        assert cross_generation.status_code == 409
        unknown = await next_generation.ack("0" * 32, leased["attempt_id"], provider_message_id="m")
        assert unknown.status_code == 409
        malformed = await next_generation.ack(delivery_id, "short", provider_message_id="m")
        assert malformed.status_code == 400
    assert lodge.deliveries()[delivery_id]["status"] == "queued"


async def test_repeat_ack_for_the_same_attempt_returns_the_first_answer(lodge):
    delivery_id = lodge.enqueue()
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        leased = (await connector.lease()).json()["deliveries"][0]
        first = await connector.ack(delivery_id, leased["attempt_id"], provider_message_id="m1")
        lodge.clock.advance(LIMITS.lease_seconds + 5)
        repeat = await connector.ack(delivery_id, leased["attempt_id"], provider_message_id="m1")
        changed = await connector.ack(delivery_id, leased["attempt_id"], ok=False, error="x", retry=False)
    assert first.status_code == repeat.status_code == changed.status_code == 200
    assert first.json() == repeat.json() == changed.json()
    assert lodge.deliveries()[delivery_id]["provider_message_id"] == "m1"


async def test_crash_after_provider_send_before_ack_duplicates_exactly_as_documented(lodge):
    delivery_id = lodge.enqueue("once, hopefully")
    async with _client() as client:
        provider = FakeProvider()
        crashed = FakeConnector(client, lodge.token, provider, crash_before_ack=True)
        assert (await crashed.run_once())[0]["crashed"] is True
        assert len(provider.sent) == 1
        lodge.clock.advance(LIMITS.lease_seconds + 1)
        restarted = FakeConnector(client, lodge.token, provider)
        results = await restarted.run_once()
    assert results == [{"ok": True, "status": "delivered", "failed_attempts": 1}]
    assert [s["delivery_id"] for s in provider.sent] == [delivery_id, delivery_id]


async def test_retries_are_bounded_and_the_terminal_failure_is_reported(lodge):
    delivery_id = lodge.enqueue()
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        statuses = []
        for attempt in range(LIMITS.max_attempts):
            connector.provider.failures.append(ProviderDown(f"down {attempt}"))
            statuses.append((await connector.run_once())[0]["status"])
            lodge.clock.advance(2)
        assert (await connector.lease()).json()["deliveries"] == []
    assert statuses == ["queued"] * (LIMITS.max_attempts - 1) + ["failed"]
    assert connector.provider.sent == []
    record = lodge.deliveries()[delivery_id]
    assert (record["status"], record["failed_attempts"]) == ("failed", LIMITS.max_attempts)
    assert "down 4" in record["error"]
    assert lodge.failed[0][1]["delivery_id"] == delivery_id
    failure = json.loads((lodge.store.root / "failures.jsonl").read_text())
    assert (failure["session"], failure["delivered"], failure["failed_attempts"]) == ("softie-1", False, 5)


async def test_the_retry_window_ends_a_delivery_and_a_permanent_nack_ends_it_at_once(lodge):
    slow = lodge.enqueue("slow")
    refused = lodge.enqueue("refused")
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        connector.provider.failures.extend([ProviderDown("busy"), ValueError("chat not found")])
        results = await connector.run_once()
        assert [r["status"] for r in results] == ["queued", "failed"]
        lodge.clock.advance(LIMITS.retry_window_seconds + 1)
        assert (await connector.lease()).json()["deliveries"] == []
    records = lodge.deliveries()
    assert records[refused]["error"] == "chat not found"
    assert (records[slow]["status"], records[slow]["failed_attempts"]) == ("failed", 1)
    assert "retry window" in records[slow]["error"]


async def test_attachment_retention_running_out_while_disabled_fails_the_delivery(lodge, tmp_path):
    note = tmp_path / "late.ogg"
    note.write_bytes(b"late")
    delivery_id = lodge.enqueue(attachment=note)
    lodge.store.disable("tg-main")
    lodge.clock.advance(LIMITS.file_retention_seconds / 2)
    assert lodge.deliveries()[delivery_id]["status"] == "queued"
    assert len(list((lodge.store.root / "files").iterdir())) == 1
    lodge.clock.advance(LIMITS.file_retention_seconds)
    record = lodge.deliveries()[delivery_id]
    assert (record["status"], record["error"]) == ("failed", "attachment retention expired")
    assert list((lodge.store.root / "files").iterdir()) == []
    lodge.store.enable("tg-main")
    async with _client() as client:
        assert (await FakeConnector(client, lodge.token).lease()).json()["deliveries"] == []


async def test_in_flight_leases_are_capped_and_the_batch_is_bounded(lodge):
    for i in range(LIMITS.max_in_flight + 2):
        lodge.enqueue(f"m{i}")
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        first = (await connector.lease(100)).json()["deliveries"]
        assert len(first) == LIMITS.max_in_flight
        capped = await connector.lease(1)
        assert (capped.status_code, capped.json()["reason"]) == (429, "deliveries_in_flight")
        assert (await connector.lease(0)).status_code == 400
        await connector.ack(first[0]["delivery_id"], first[0]["attempt_id"], provider_message_id="m")
        assert len((await connector.lease(1)).json()["deliveries"]) == 1


def test_queue_length_text_size_and_journals_are_bounded(lodge, tmp_path):
    """Finding 5: outbound bounds and journal rotation."""
    for i in range(LIMITS.max_queued):
        lodge.enqueue(f"m{i}")
    with pytest.raises(ConnectorError) as refused:
        lodge.enqueue("one too many")
    assert (refused.value.status, refused.value.reason) == (429, "queue_full")
    with pytest.raises(ConnectorError, match="text_too_long"):
        lodge.store.enqueue(
            channel="telegram", account="bot:1", conversation="c1",
            text="x" * (LIMITS.outbound_text_max_bytes + 1),
        )
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * (LIMITS.outbound_file_max_bytes + 1))
    with pytest.raises(ConnectorError, match="attachment_too_large"):
        lodge.enqueue(attachment=big)
    for delivery_id in list(lodge.deliveries()):
        lodge.store.unbind("tg-main", sender="u1", conversation="c1")
    lodge.store.list_deliveries("tg-main")
    journal = lodge.store.root / "failures.jsonl"
    assert journal.stat().st_size <= LIMITS.journal_max_bytes + 400
    assert (lodge.store.root / "failures.jsonl.1").exists()


async def test_a_waiting_lease_returns_when_work_arrives_and_dies_with_its_token(lodge):
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)

        async def enqueue_soon():
            await asyncio.sleep(0.05)
            lodge.enqueue("while you wait")

        response, _ = await asyncio.gather(connector.lease(1, wait=5), enqueue_soon())
        assert [d["text"] for d in response.json()["deliveries"]] == ["while you wait"]
        await connector.ack(
            response.json()["deliveries"][0]["delivery_id"],
            response.json()["deliveries"][0]["attempt_id"], provider_message_id="m",
        )

        async def revoke_soon():
            await asyncio.sleep(0.05)
            lodge.store.revoke_tokens("tg-main")

        response, _ = await asyncio.gather(connector.lease(1, wait=5), revoke_soon())
        assert response.status_code == 401

        token = lodge.store.issue_token("tg-main")
        connector = FakeConnector(client, token)

        async def disable_soon():
            await asyncio.sleep(0.05)
            lodge.store.disable("tg-main")

        response, _ = await asyncio.gather(connector.lease(1, wait=5), disable_soon())
    assert response.json() == {"deliveries": [], "paused": True}


# ------------------------------------------------- callbacks (finding 6)


def test_failure_listeners_run_after_the_lock_and_may_enqueue(lodge):
    """A listener that tells the session by sending another message does not deadlock."""
    told = []

    def tell_the_session(instance_id, delivery):
        told.append(delivery["delivery_id"])
        lodge.store.enqueue(
            channel="telegram", account="bot:1", conversation="c1",
            text=f"could not deliver {delivery['delivery_id']}",
        )

    lodge.store.on_failure(tell_the_session)
    who = _principal(lodge)
    delivery_id = lodge.enqueue()
    leased = lodge.store.lease(who, 1)["deliveries"][0]
    result = lodge.store.ack(who, delivery_id, {"attempt_id": leased["attempt_id"], "ok": False, "retry": False})
    assert result["status"] == "failed"
    assert told == [delivery_id]
    texts = [d["text"] for d in lodge.store.list_deliveries("tg-main") if d["status"] == "queued"]
    assert texts == [f"could not deliver {delivery_id}"]


def test_the_router_runs_after_the_claim_is_durable_and_may_use_the_store(lodge):
    seen = []

    def router(event):
        queue = json.loads((lodge.store.root / "queues" / "tg-main.json").read_text())
        seen.append(("claimed", queue["uploads"][event.attachments[0]["upload_id"]]["claimed_by"]))
        seen.append(("deliveries", len(lodge.store.list_deliveries("tg-main"))))
        raise RuntimeError("session unreachable")

    lodge.store.router = router
    who = _principal(lodge)
    upload_id, part = lodge.store.begin_upload(who)
    part.write_bytes(b"x")
    lodge.store.commit_upload(who, upload_id, part, "x")
    event = {
        "event_id": "e1", "sender": {"provider_id": "u1"}, "conversation": {"provider_id": "c1"},
        "text": "hi", "attachments": [{"upload_id": upload_id}],
    }
    status, body = lodge.store.accept_event(who, event)
    assert (status, body["accepted"], body["routed"]) == (200, True, "error")
    assert seen == [("claimed", "e1"), ("deliveries", 0)]
    # The record keeps the outcome; a retry does not run the router again.
    assert lodge.store.accept_event(who, event) == (200, body)
    assert len(seen) == 2


def test_a_store_call_from_inside_the_lock_is_refused_not_deadlocked(lodge):
    with lodge.store._lock():
        with pytest.raises(RuntimeError, match="inside its own lock"):
            lodge.store.add_instance("x", "telegram", "bot:x")
        with pytest.raises(RuntimeError, match="inside its own lock"):
            lodge.enqueue()
    # Released: the same calls work again.
    assert lodge.store.list_instances()[0]["instance_id"] == "tg-main"


# ------------------------------------------------------------------- files


async def test_files_need_the_current_generations_active_lease_for_that_attempt(lodge, tmp_path):
    note = tmp_path / "n.ogg"
    note.write_bytes(b"audio")
    delivery_id = lodge.enqueue(attachment=note)
    other_token = lodge.second_instance()
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        leased = (await connector.lease()).json()["deliveries"][0]
        file_id, attempt_id = leased["attachments"][0]["file_id"], leased["attempt_id"]
        good = await connector.fetch_file(file_id, attempt_id)
        assert (good.status_code, good.content) == (200, b"audio")
        assert good.headers["x-woltspace-attachment-name"] == "n.ogg"
        assert good.headers["cache-control"] == "no-store"
        assert (await connector.fetch_file(file_id, None)).status_code == 404
        assert (await connector.fetch_file(file_id, "0" * 32)).status_code == 404
        # Another instance, even with the right attempt id.
        lodge.store.disable("tg-main")
        lodge.store.enable("tg-new")
        assert (await FakeConnector(client, other_token).fetch_file(file_id, attempt_id)).status_code == 404
        lodge.store.disable("tg-new")
        lodge.store.enable("tg-main")
        # A stale attempt after the lease expired, then a fresh one works again.
        lodge.clock.advance(LIMITS.lease_seconds + 1)
        assert (await connector.fetch_file(file_id, attempt_id)).status_code == 404
        again = (await connector.lease()).json()["deliveries"][0]
        assert (await connector.fetch_file(file_id, again["attempt_id"])).status_code == 200
        # A new generation cannot use the old generation's attempt.
        rotated = FakeConnector(client, lodge.store.issue_token("tg-main"))
        assert (await rotated.fetch_file(file_id, again["attempt_id"])).status_code == 404
        final = (await rotated.lease()).json()["deliveries"][0]
        assert (await rotated.fetch_file(file_id, final["attempt_id"])).status_code == 200
        # Acked: the file expired with the delivery.
        await rotated.ack(delivery_id, final["attempt_id"], provider_message_id="m")
        assert (await rotated.fetch_file(file_id, final["attempt_id"])).status_code == 404
    assert list((lodge.store.root / "files").iterdir()) == []


async def test_revoking_mid_stream_cuts_the_file_at_the_next_chunk(lodge, tmp_path, monkeypatch):
    big = tmp_path / "big.bin"
    big.write_bytes(b"z" * (connector_api._CHUNK * 3))
    lodge.enqueue(attachment=big)
    allowed = lodge.store.stream_allowed
    calls = []

    def revoking(who, file_id, attempt_id):
        calls.append(1)
        if len(calls) == 2:
            lodge.store.revoke_tokens("tg-main")
        return allowed(who, file_id, attempt_id)

    monkeypatch.setattr(lodge.store, "stream_allowed", revoking)
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        leased = (await connector.lease()).json()["deliveries"][0]
        received = b""
        try:
            async with client.stream(
                "GET", f"/connector/v1/files/{leased['attachments'][0]['file_id']}",
                params={"attempt_id": leased["attempt_id"]}, headers=connector.headers,
            ) as response:
                async for chunk in response.aiter_bytes():
                    received += chunk
        except httpx.HTTPError:
            pass
    assert len(received) == connector_api._CHUNK


# ------------------------------------- commit before dispatch (round 3)


def _one_shot(lodge, **limits) -> "ConnectorStore":
    """The lodge's store with a single allowed attempt: one expiry is terminal."""
    store = ConnectorStore(
        lodge.store.root, clock=lodge.clock,
        limits=Limits(max_attempts=1, backoff_seconds=(1,), outbound_file_max_bytes=1_000_000, **limits),
    )
    store.on_failure(lambda instance_id, delivery: lodge.failed.append((instance_id, delivery)))
    return store


def test_a_refused_ack_still_commits_the_expiry_and_notifies_exactly_once(lodge):
    """Astre's round-2 probe 1: the 409 path persisted the failure but told nobody."""
    store = _one_shot(lodge)
    who = store.authenticate(lodge.token)
    delivery_id = lodge.enqueue()
    leased = store.lease(who, 1)["deliveries"][0]
    lodge.clock.advance(LIMITS.lease_seconds + 1)
    with pytest.raises(ConnectorError) as refused:
        store.ack(who, delivery_id, {"attempt_id": leased["attempt_id"], "ok": True})
    assert refused.value.status == 409
    assert store._queue("tg-main").deliveries[0]["status"] == "failed"
    assert [f[1]["delivery_id"] for f in lodge.failed] == [delivery_id]
    store.list_deliveries("tg-main")
    store.lease(who, 1)
    assert len(lodge.failed) == 1
    assert len((store.root / "failures.jsonl").read_text().splitlines()) == 1


def test_a_dedup_replay_commits_its_sweep_and_notifies_exactly_once(lodge):
    """Astre's round-2 probe 2: the early return dispatched what it never wrote."""
    store = _one_shot(lodge)
    who = store.authenticate(lodge.token)
    event = {"event_id": "e1", "sender": {"provider_id": "u1"}, "conversation": {"provider_id": "c1"}, "text": "hi"}
    assert store.accept_event(who, event)[1]["accepted"] is True
    delivery_id = lodge.enqueue()
    store.lease(who, 1)
    lodge.clock.advance(LIMITS.lease_seconds + 1)
    first = store.accept_event(who, event)
    second = store.accept_event(who, event)
    assert first == second
    assert store._queue("tg-main").deliveries[0]["status"] == "failed"
    assert [f[1]["delivery_id"] for f in lodge.failed] == [delivery_id]
    store.list_deliveries("tg-main")
    assert len(lodge.failed) == 1


def test_a_refusal_before_any_write_commits_the_sweep_too(lodge):
    """A saved-then-refused path and a refused-before-saving path both leave the disk current."""
    store = _one_shot(lodge)
    who = store.authenticate(lodge.token)
    delivery_id = lodge.enqueue()
    store.lease(who, 1)
    lodge.clock.advance(LIMITS.lease_seconds + 1)
    # open_file sweeps, saves, then refuses: the expiry is on disk and announced once.
    with pytest.raises(ConnectorError):
        store.open_file(who, "0" * 32, "1" * 32)
    assert store._queue("tg-main").deliveries[0]["status"] == "failed"
    assert [f[1]["delivery_id"] for f in lodge.failed] == [delivery_id]
    # An unexpected exception inside the lock commits nothing and announces nothing.
    second = lodge.enqueue()
    store.lease(who, 1)
    lodge.clock.advance(LIMITS.lease_seconds + 1)
    before = (store.root / "queues" / "tg-main.json").read_text()
    original = ConnectorStore._save_queue

    def crash(self, instance_id, queue):
        raise OSError("disk gone")

    ConnectorStore._save_queue = crash
    try:
        with pytest.raises(OSError):
            store.list_deliveries("tg-main")
    finally:
        ConnectorStore._save_queue = original
    assert (store.root / "queues" / "tg-main.json").read_text() == before
    assert len(lodge.failed) == 1
    store.list_deliveries("tg-main")
    assert [f[1]["delivery_id"] for f in lodge.failed] == [delivery_id, second]


def test_journal_rotation_and_append_are_serialized_without_the_store_lock(lodge):
    import threading

    store = lodge.store
    written = threading.Event()

    def append():
        store.record_failure("tg-main", {"delivery_id": "x" * 32, "status": "failed", "conversation": "c1"})
        written.set()

    with store._journal_lock():
        worker = threading.Thread(target=append)
        worker.start()
        assert not written.wait(0.2)
        # The store lock is free meanwhile: the journal lock is its own.
        assert store.list_instances()[0]["instance_id"] == "tg-main"
        lodge.enqueue()
    assert written.wait(2)
    worker.join()
    assert len((store.root / "failures.jsonl").read_text().splitlines()) == 1


async def test_deeply_nested_json_is_malformed_input_not_a_crash(lodge):
    nested = b"[" * 30000 + b"]" * 30000
    assert len(nested) <= LIMITS.request_max_bytes
    with pytest.raises(RecursionError):
        json.loads(nested)
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        for path in ("/connector/v1/events", "/connector/v1/deliveries/lease"):
            response = await client.post(path, content=nested, headers=connector.headers)
            assert (response.status_code, response.json()["reason"]) == (400, "malformed_json"), path


# ----------------------------------- slice 2a: handoff, cursor, message map


def _event(event_id="e1", text="hi", **extra) -> dict:
    return {
        "event_id": event_id, "sender": {"provider_id": "u1"}, "conversation": {"provider_id": "c1"},
        "text": text, **extra,
    }


def test_an_accepted_event_is_durable_before_the_router_and_handed_off_once(lodge, monkeypatch):
    """Boundary core -> session: accepted is written first; the receipt follows the router."""
    store = lodge.store
    who = _principal(lodge)
    seen = []

    def router(event):
        queue = json.loads((store.root / "queues" / "tg-main.json").read_text())
        seen.append(queue["inbound"][event.dedup_key]["state"])
        return {"routed": "test"}

    store.router = router
    status, body = store.accept_event(who, _event())
    assert (status, body["routed"]) == (200, "test")
    assert seen == ["accepted"]
    record = store._queue("tg-main").inbound["bot:1\x00e1"]
    assert (record["state"], record["result"]) == ("handed_off", {"routed": "test"})
    # Replays and sweeps never hand it off again.
    assert store.accept_event(who, _event()) == (200, body)
    lodge.clock.advance(LIMITS.handoff_retry_seconds * 2)
    store.list_deliveries("tg-main")
    assert seen == ["accepted"]


def test_a_crash_between_accept_and_receipt_is_retried_idempotently_on_load(lodge, monkeypatch):
    """Fault injection: the router ran (or did not) but the receipt never landed."""
    store = lodge.store
    who = _principal(lodge)
    delivered = []

    def crashing_router(event):
        delivered.append(event.dedup_key)
        raise KeyboardInterrupt("process died after the paste")

    store.router = crashing_router
    with pytest.raises(KeyboardInterrupt):
        store.accept_event(who, _event())
    record = store._queue("tg-main").inbound["bot:1\x00e1"]
    assert record["state"] == "accepted" and "result" not in record
    assert delivered == ["bot:1\x00e1"]
    # A replay inside the retry window answers from dedup without a handoff.
    assert store.accept_event(who, _event())[1]["accepted"] is True
    assert len(delivered) == 1
    # The lodge restarts: the next read of the queue hands the event off
    # again, outside the lock. An idempotent router (keyed on dedup_key)
    # sees the same key and may skip; a session paste would repeat.
    reloaded = lodge.reload()
    receipts = []

    def idempotent_router(event):
        if event.dedup_key in delivered:
            receipts.append(("skipped", event.dedup_key))
            return {"routed": "already_delivered"}
        delivered.append(event.dedup_key)
        return {"routed": "delivered"}

    reloaded.router = idempotent_router
    lodge.clock.advance(LIMITS.handoff_retry_seconds + 1)
    who = reloaded.authenticate(lodge.token)
    reloaded.lease(who, 1)  # a connector-driven read claims the next attempt
    assert receipts == [("skipped", "bot:1\x00e1")]
    record = reloaded._queue("tg-main").inbound["bot:1\x00e1"]
    assert (record["state"], record["result"]) == ("handed_off", {"routed": "already_delivered"})
    assert reloaded.pending_handoffs("tg-main") == []
    lodge.clock.advance(LIMITS.handoff_retry_seconds + 1)
    reloaded.lease(who, 1)
    assert len(receipts) == 1


def test_a_router_that_keeps_failing_is_retried_up_to_the_cap_then_parked(lodge):
    store = lodge.store
    who = _principal(lodge)
    tries = []
    store.router = lambda event: tries.append(1) or (_ for _ in ()).throw(RuntimeError("session unreachable"))
    status, body = store.accept_event(who, _event())
    assert (status, body["routed"]) == (200, "error")
    for _ in range(LIMITS.handoff_max_attempts + 2):
        lodge.clock.advance(LIMITS.handoff_retry_seconds + 1)
        store.lease(who, 1)
    assert len(tries) == LIMITS.handoff_max_attempts
    record = store._queue("tg-main").inbound["bot:1\x00e1"]
    assert record["state"] == "handoff_failed"
    assert "session unreachable" in record["last_error"]
    # Parked, not aged out: still listed after the retention period, with its
    # owner recovery path.
    lodge.clock.advance(LIMITS.dedup_retention_seconds + 1)
    store.lease(who, 1)
    assert [h["state"] for h in store.handoffs("tg-main")] == ["handoff_failed"]
    assert store.retry_handoff("tg-main", "e1") is True
    lodge.clock.advance(1)
    store.lease(who, 1)
    assert len(tries) == LIMITS.handoff_max_attempts + 1
    assert store.cancel_handoff("tg-main", "e1") is True
    assert store.handoffs("tg-main") == []


async def test_state_and_cursor_advance_only_past_answered_updates(lodge):
    """Boundary receipt -> cursor: core refuses a cursor it has not answered up to."""
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        state = await client.get("/connector/v1/state", headers=connector.headers)
        assert state.json()["cursor"] is None and state.json()["runtime_active"] is True
        ahead = await client.post("/connector/v1/cursor", json={"cursor": 5}, headers=connector.headers)
        assert (ahead.status_code, ahead.json()["reason"]) == (409, "cursor_ahead")
        assert (await connector.send_event("tg:4", "u1", "c1", "hi", cursor=4)).json()["accepted"] is True
        dropped = await connector.send_event("tg:5", "u2", "c1", "hi", cursor=5)
        assert dropped.json()["accepted"] is False
        moved = await client.post("/connector/v1/cursor", json={"cursor": 6}, headers=connector.headers)
        assert moved.json() == {"cursor": 6, "answered_cursor": 5}
        too_far = await client.post("/connector/v1/cursor", json={"cursor": 7}, headers=connector.headers)
        assert too_far.json()["reason"] == "cursor_ahead"
        back = await client.post("/connector/v1/cursor", json={"cursor": 3}, headers=connector.headers)
        assert back.json()["reason"] == "cursor_regressed"
        for raw in (b'{"cursor": -1}', b'{"cursor": "6"}', b'{"cursor": 6, "x": 1}', b'{"cursor": true}'):
            response = await client.post("/connector/v1/cursor", content=raw, headers=connector.headers)
            assert response.status_code == 400, raw
        # A replay of an answered update is still an answer for its position.
        assert (await connector.send_event("tg:4", "u1", "c1", "hi", cursor=4)).json()["accepted"] is True
        # The cursor survives a restart and a rotation.
        lodge.reload()
        rotated = FakeConnector(client, lodge.store.issue_token("tg-main"))
        assert (await client.get("/connector/v1/state", headers=rotated.headers)).json()["cursor"] == 6


async def test_provider_message_map_covers_every_part_and_stays_inside_the_binding(lodge):
    """A reply to any part of a split send finds the delivery, in its own account and binding."""
    other_sender = lodge.store.bind("tg-main", sender="u2", conversation="c1", default_wolt="softie")
    delivery_id = lodge.enqueue("long", binding_id=lodge.binding["binding_id"])
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        leased = (await connector.lease()).json()["deliveries"][0]
        ack = await client.post(
            f"/connector/v1/deliveries/{delivery_id}/ack",
            json={"attempt_id": leased["attempt_id"], "ok": True, "provider_message_ids": ["m1", "m2", "m3"]},
            headers=connector.headers,
        )
        assert ack.json()["status"] == "delivered"
        for part in ("m1", "m2", "m3"):
            reply = await connector.send_event(f"re:{part}", "u1", "c1", "re", reply_to={"provider_message_id": part})
            assert (reply.json()["accepted"], reply.json()["session"]) == (True, "softie-1"), part
        # Same conversation, other sender: another binding, nothing resolves.
        foreign = await connector.send_event("re:x", "u2", "c1", "re", reply_to={"provider_message_id": "m2"})
        assert foreign.json()["reason"] == "reply_to_out_of_binding"
        unknown = await connector.send_event("re:y", "u1", "c1", "re", reply_to={"provider_message_id": "m9"})
        assert unknown.json()["reason"] == "reply_to_out_of_binding"
        both = await connector.send_event("re:z", "u1", "c1", "re", reply_to={"provider_message_id": "m1", "delivery_id": delivery_id})
        assert both.status_code == 400
        too_many = await client.post(
            f"/connector/v1/deliveries/{delivery_id}/ack",
            json={"attempt_id": leased["attempt_id"], "ok": True, "provider_message_ids": ["m"] * 51},
            headers=connector.headers,
        )
        assert too_many.status_code == 400
        # Another account's instance with the same message id: its own map is empty.
        other_token = lodge.second_instance(enable=True)
        other = FakeConnector(client, other_token)
        cross = await other.send_event("re:q", "u9", "c9", "re", reply_to={"provider_message_id": "m1"})
    assert cross.json()["reason"] == "reply_to_out_of_binding"
    record = lodge.deliveries()[delivery_id]
    assert (record["provider_message_id"], record["provider_message_ids"]) == ("m1", ["m1", "m2", "m3"])
    assert other_sender["binding_id"] != lodge.binding["binding_id"]


# ----------------------------------- round 2: dispatcher ownership, frontier, map


def _cli_store(lodge) -> ConnectorStore:
    """A store the way the CLI builds one: same files, no dispatcher."""
    return ConnectorStore(lodge.store.root, clock=lodge.clock, limits=LIMITS)


def test_a_store_without_a_dispatcher_never_hands_off_or_writes_a_receipt(lodge):
    """Astre's probe 1: a CLI read must not manufacture a receipt."""
    lodge.store.router = lambda event: (_ for _ in ()).throw(RuntimeError("temporary"))
    who = _principal(lodge)
    status, body = lodge.store.accept_event(who, _event())
    assert body["routed"] == "error"
    lodge.clock.advance(LIMITS.handoff_retry_seconds + 1)
    cli = _cli_store(lodge)
    assert cli.router is None
    cli.list_deliveries("tg-main")
    cli.state(who)
    cli.lease(who, 1)
    cli.recover()
    record = cli._queue("tg-main").inbound["bot:1\x00e1"]
    assert (record["state"], record["attempts"], record["result"]) == ("accepted", 1, {"routed": "error", "error": "temporary"})
    # A store with no dispatcher accepts, records, and leaves the event waiting.
    fresh = ConnectorStore(lodge.store.root / "nodispatch", clock=lodge.clock, limits=LIMITS)
    fresh.add_instance("x", "fake", "acct")
    fresh.bind("x", sender="u", conversation="c", default_wolt="softie")
    fresh.enable("x")
    me = fresh.authenticate(fresh.issue_token("x"))
    status, body = fresh.accept_event(me, {"event_id": "e", "sender": {"provider_id": "u"}, "conversation": {"provider_id": "c"}, "text": "hi"})
    assert body["routed"] == "pending"
    assert fresh.handoffs("x")[0]["state"] == "accepted" and "attempts" not in fresh.handoffs("x")[0]


def test_owner_reads_never_claim_an_attempt_connector_reads_do(lodge):
    calls = []
    lodge.store.router = lambda event: calls.append(1) or (_ for _ in ()).throw(RuntimeError("later"))
    who = _principal(lodge)
    lodge.store.accept_event(who, _event())
    assert calls == [1]
    lodge.clock.advance(LIMITS.handoff_retry_seconds + 1)
    lodge.store.list_deliveries("tg-main")
    lodge.store.state(who)
    lodge.store.recover()
    assert calls == [1]
    lodge.store.lease(who, 1)
    assert calls == [1, 1]


def test_retries_recheck_the_binding_the_instance_and_the_runtime(lodge, tmp_path):
    """Astre's probe 2: withdrawn grants never remain authority for a retry."""
    seen = []
    lodge.store.router = lambda event: (_ for _ in ()).throw(RuntimeError("temporary"))
    who = _principal(lodge)
    lodge.store.accept_event(who, _event())
    lodge.store.router = lambda event: seen.append(event) or {"routed": "delivered"}
    # Disabled: the record waits, no attempt is spent.
    lodge.store.disable("tg-main")
    lodge.clock.advance(LIMITS.handoff_retry_seconds + 1)
    lodge.store.state(who)
    lodge.store.lease(who, 1)
    record = lodge.store._queue("tg-main").inbound["bot:1\x00e1"]
    assert (seen, record["state"], record["attempts"]) == ([], "accepted", 1)
    # The channel's runtime moved to the adapter: still waiting.
    lodge.store.enable("tg-main")
    config = tmp_path / "woltspace.json"
    config.write_text(json.dumps({"telegram": {"runtime": "adapter"}}))
    lodge.store.runtime_config = config
    lodge.store.lease(who, 1)
    assert seen == []
    lodge.store.runtime_config = None
    # A changed grant is what the router sees.
    lodge.store.bind("tg-main", sender="u1", conversation="c1", default_wolt="otter", may_wake=True)
    lodge.store.lease(who, 1)
    assert [e.binding["default_wolt"] for e in seen] == ["otter"]
    assert seen[0].binding["may_wake"] is True
    # Unbound: cancelled, the router is never called.
    lodge.store.router = lambda event: (_ for _ in ()).throw(RuntimeError("temporary"))
    lodge.store.accept_event(who, _event("e2"))
    lodge.store.router = lambda event: seen.append(event) or {"routed": "delivered"}
    lodge.store.unbind("tg-main", sender="u1", conversation="c1")
    lodge.clock.advance(LIMITS.handoff_retry_seconds + 1)
    lodge.store.bind("tg-main", sender="u1", conversation="c1", default_wolt="softie")
    lodge.store.lease(who, 1)
    record = lodge.store._queue("tg-main").inbound["bot:1\x00e2"]
    assert (len(seen), record["state"], record["result"]["reason"]) == (1, "cancelled", "binding_removed")


def test_an_uncommitted_failure_path_dispatches_no_claim(lodge, monkeypatch):
    calls = []
    lodge.store.router = lambda event: calls.append(1) or (_ for _ in ()).throw(RuntimeError("later"))
    who = _principal(lodge)
    lodge.store.accept_event(who, _event())
    lodge.clock.advance(LIMITS.handoff_retry_seconds + 1)
    before = (lodge.store.root / "queues" / "tg-main.json").read_text()
    with monkeypatch.context() as patched:
        patched.setattr(ConnectorStore, "_save_queue", lambda self, i, q: (_ for _ in ()).throw(OSError("disk gone")))
        with pytest.raises(OSError):
            lodge.store.lease(who, 1)
    assert calls == [1]
    assert (lodge.store.root / "queues" / "tg-main.json").read_text() == before
    lodge.store.lease(who, 1)
    assert calls == [1, 1]


def _elsewhere(fn):
    """Run `fn` on another thread and wait: a concurrent connector read, not a nested one."""
    import threading

    thread = threading.Thread(target=fn)
    thread.start()
    thread.join(10)
    assert not thread.is_alive()


def test_a_slow_attempt_keeps_the_event_and_a_newer_claim_acts_on_nothing(lodge):
    """Attempt 1 is slow; attempt 2 is claimed meanwhile but cannot route beside it
    (Astre's rc1 review: both acting meant a start and a paste); attempt 1's receipt stands."""
    who = _principal(lodge)
    outcomes = []

    def slow_then_fast(event):
        if not outcomes:
            outcomes.append("slow started")
            lodge.clock.advance(LIMITS.handoff_retry_seconds + 1)
            lodge.store.router = lambda e: outcomes.append("fast") or {"routed": "delivered"}
            _elsewhere(lambda: lodge.store.lease(who, 1))   # claims attempt 2 and runs the fast router
            return {"routed": "late"}
        return {"routed": "delivered"}

    lodge.store.router = slow_then_fast
    status, body = lodge.store.accept_event(who, _event())
    record = lodge.store._queue("tg-main").inbound["bot:1\x00e1"]
    assert outcomes == ["slow started"]
    assert (record["state"], record["attempts"], record["result"]) == ("handed_off", 1, {"routed": "late"})
    assert body["routed"] == "late"


async def test_an_ignore_disposition_answers_the_position_for_good(lodge):
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        ignored = await client.post(
            "/connector/v1/events", json={"event_id": "tg:7", "cursor": 7, "ignore": "unsupported_update"},
            headers=connector.headers,
        )
        assert ignored.json() == {"accepted": False, "reason": "ignored_unsupported_update", "event_id": "tg:7"}
        assert (await client.post("/connector/v1/cursor", json={"cursor": 8}, headers=connector.headers)).json()["cursor"] == 8
        replay = await client.post(
            "/connector/v1/events", json={"event_id": "tg:7", "cursor": 7, "ignore": "unsupported_update"},
            headers=connector.headers,
        )
        assert replay.json() == ignored.json()
        bad = await client.post(
            "/connector/v1/events", json={"event_id": "tg:8", "cursor": 8, "ignore": "x", "text": "no"},
            headers=connector.headers,
        )
        assert bad.status_code == 400
    assert lodge.routed == []


async def test_a_replay_with_another_cursor_is_a_conflict_not_a_relabel(lodge):
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        assert (await connector.send_event("tg:5", "u1", "c1", "hi", cursor=5)).json()["accepted"] is True
        relabel = await connector.send_event("tg:5", "u1", "c1", "hi", cursor=9)
        assert (relabel.status_code, relabel.json()["reason"]) == (409, "event_conflict")
        state = (await client.get("/connector/v1/state", headers=connector.headers)).json()
        assert state["answered_cursor"] == 5
        assert (await client.post("/connector/v1/cursor", json={"cursor": 10}, headers=connector.headers)).json()["reason"] == "cursor_ahead"
        # A gap in provider ids is nobody's to answer: 6..19 were never fetched.
        assert (await connector.send_event("tg:20", "u1", "c1", "hi", cursor=20)).json()["accepted"] is True
        assert (await client.post("/connector/v1/cursor", json={"cursor": 21}, headers=connector.headers)).json()["cursor"] == 21


async def test_message_map_is_scoped_by_conversation_and_keeps_parts_of_failed_attempts(lodge):
    """Astre's probe 3, plus the sent parts of a nacked attempt."""
    lodge.store.bind("tg-main", sender="u1", conversation="c2", default_wolt="softie")
    in_c1 = lodge.enqueue("one", conversation="c1")
    in_c2 = lodge.enqueue("two", conversation="c2")
    async with _client() as client:
        connector = FakeConnector(client, lodge.token)
        leased = {d["delivery_id"]: d for d in (await connector.lease()).json()["deliveries"]}
        # Both chats hand out message id 100: unique per chat, not per bot.
        for delivery_id in (in_c1, in_c2):
            ack = await client.post(
                f"/connector/v1/deliveries/{delivery_id}/ack",
                json={"attempt_id": leased[delivery_id]["attempt_id"], "ok": True, "provider_message_ids": ["100"]},
                headers=connector.headers,
            )
            assert ack.json()["status"] == "delivered"
        reply = await connector.send_event("re:1", "u1", "c1", "re", reply_to={"provider_message_id": "100"})
        assert (reply.json()["accepted"], reply.json()["session"]) == (True, "softie-1")
        # A failed attempt that sent part one: the part is mapped, the retry adds the rest.
        partial = lodge.enqueue("long", conversation="c1")
        first = (await connector.lease()).json()["deliveries"][0]
        nack = await client.post(
            f"/connector/v1/deliveries/{partial}/ack",
            json={"attempt_id": first["attempt_id"], "ok": False, "retry": True, "error": "timeout", "provider_message_ids": ["201"]},
            headers=connector.headers,
        )
        assert nack.json()["status"] == "queued"
        re_part = await connector.send_event("re:2", "u1", "c1", "re", reply_to={"provider_message_id": "201"})
        assert re_part.json()["accepted"] is True
        lodge.clock.advance(5)
        second = (await connector.lease()).json()["deliveries"][0]
        await client.post(
            f"/connector/v1/deliveries/{partial}/ack",
            json={"attempt_id": second["attempt_id"], "ok": True, "provider_message_ids": ["201", "202"]},
            headers=connector.headers,
        )
    assert lodge.deliveries()[partial]["provider_message_ids"] == ["201", "202"]


# ----------------------------------------- round 3: claim identity


def test_an_owner_retry_during_an_old_dispatch_cannot_act_beside_it(lodge):
    """Old attempt in flight, owner retries, the new attempt is claimed: it acts on
    nothing while the old one routes, and the old attempt's effect is the receipt."""
    who = _principal(lodge)
    seen = []

    def old_router(event):
        seen.append("old started")
        assert lodge.store.retry_handoff("tg-main", "e1") is True
        lodge.store.router = lambda e: seen.append("new") or {"routed": "error", "error": "new attempt failed"}
        _elsewhere(lambda: lodge.store.lease(who, 1))
        return {"routed": "delivered", "session": "OLD"}

    lodge.store.router = old_router
    lodge.store.accept_event(who, _event())
    record = lodge.store._queue("tg-main").inbound["bot:1\x00e1"]
    assert seen == ["old started"]
    assert (record["state"], record["result"]["session"]) == ("handed_off", "OLD")
    # Nothing is left for a later attempt.
    lodge.clock.advance(LIMITS.handoff_retry_seconds + 1)
    lodge.store.lease(who, 1)
    assert seen == ["old started"]


def test_a_pause_and_resume_during_an_attempt_does_not_let_a_new_claim_act(lodge):
    who = _principal(lodge)
    claims = []

    def old_router(event):
        claims.append(lodge.store._queue("tg-main").inbound["bot:1\x00e1"]["claim"])
        # The owner pauses and resumes while this attempt is still running;
        # then a connector tick claims a new attempt with a new identity.
        lodge.store.disable("tg-main")
        lodge.store.enable("tg-main")
        lodge.clock.advance(LIMITS.handoff_retry_seconds + 1)
        lodge.store.router = lambda e: {"routed": "delivered", "session": "NEW"}
        _elsewhere(lambda: lodge.store.lease(who, 1))
        claims.append(lodge.store._queue("tg-main").inbound["bot:1\x00e1"]["claim"])
        return {"routed": "delivered", "session": "OLD"}

    lodge.store.router = old_router
    lodge.store.accept_event(who, _event())
    record = lodge.store._queue("tg-main").inbound["bot:1\x00e1"]
    # No new claim is made while the attempt routes; its delivery is the receipt.
    assert len(claims) == 2 and claims[0] == claims[1]
    assert (record["state"], record["result"]["session"]) == ("handed_off", "OLD")


def test_a_returned_claim_is_a_new_identity_when_made_again(lodge):
    who = _principal(lodge)
    lodge.store.router = lambda e: (_ for _ in ()).throw(RuntimeError("temporary"))
    lodge.store.accept_event(who, _event())
    first = lodge.store._queue("tg-main").inbound["bot:1\x00e1"]["claim"]
    lodge.store.disable("tg-main")
    lodge.clock.advance(LIMITS.handoff_retry_seconds + 1)
    lodge.store.lease(who, 1)
    lodge.store.enable("tg-main")
    lodge.store.router = lambda e: {"routed": "delivered"}
    lodge.store.lease(who, 1)
    record = lodge.store._queue("tg-main").inbound["bot:1\x00e1"]
    assert record["state"] == "handed_off" and record["claim"] != first
