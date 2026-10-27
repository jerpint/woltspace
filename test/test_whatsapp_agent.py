import asyncio
import importlib.util
import io
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from woltspace.channels import WhatsAppConnector, connector_secrets
from woltspace.layout import RuntimeLayout
from woltspace.whatsapp_rate_limit import pace_request
from server.notification_senders import whatsapp_sender
from server.notification_types import OutboundMessage, SendContext


def _load_adapter(monkeypatch, tmp_path):
    container = ROOT / "container"
    sys.path.insert(0, str(container))
    core = types.ModuleType("bot.core")
    core.message_session = lambda *a, **k: {"ok": True}
    core.start_claude_session = lambda *a, **k: {"name": "session-test", "url": ""}
    monkeypatch.setitem(sys.modules, "bot.core", core)
    wolts = types.ModuleType("wolts")
    wolts.list_wolts = lambda: [{"name": "beaver", "type": "beaver"}]
    wolts.wolt_exists = lambda name: name == "beaver"
    monkeypatch.setitem(sys.modules, "wolts", wolts)
    paths = types.ModuleType("lib.paths")
    paths.space_dir = lambda _root=None: tmp_path / "space"
    monkeypatch.setitem(sys.modules, "lib.paths", paths)
    prompts = types.ModuleType("lib.notify_prompt")
    prompts.notify_reply_instruction = lambda *args: f"notify {args}"
    monkeypatch.setitem(sys.modules, "lib.notify_prompt", prompts)
    monkeypatch.setenv("WHATSAPP_AGENT_TOKEN", "test-key")
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(tmp_path / "wolts"))
    import bot.whatsapp_adapter as adapter
    # Each fixture owns its synthetic account/state; never inherit a prior
    # imported module's per-account creator binding or filesystem paths.
    state = tmp_path / "space/whatsapp"
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    monkeypatch.setattr(adapter, "STATE", state)
    monkeypatch.setattr(adapter, "TOKEN", "test-key")
    monkeypatch.setattr(adapter, "ACCOUNT", __import__('hashlib').sha256(b"test-key").hexdigest()[:20])
    monkeypatch.setattr(adapter, "ROUTES", state / f"routes-{adapter.ACCOUNT}.json")
    adapter.atomic_json(adapter._owner_path(), {"account":adapter.ACCOUNT, "participant":"user:1"})
    return adapter


def test_connector_is_opt_in_and_never_publishes_key(tmp_path):
    layout = RuntimeLayout(tmp_path / "wolts", ROOT, "127.0.0.1", 7777, "host")
    off = WhatsAppConnector().plan(layout, {})
    assert not off.enabled
    env = {"ENABLE_WHATSAPP_AGENT": "true", "WHATSAPP_AGENT_TOKEN": "secret-key",
           "WOLTSPACE_ENTRYPOINT": "1"}
    plan = WhatsAppConnector().plan(layout, env)
    assert plan.enabled
    assert plan.command[-1] == "bot.whatsapp_adapter"
    assert connector_secrets([plan])["WHATSAPP_AGENT_TOKEN"] == "secret-key"
    assert "secret-key" not in json.dumps(plan.to_record())


def test_sender_uses_exact_participant_and_refuses_attachments():
    sent = []
    async def transport(*args):
        sent.append(args)
    sender = whatsapp_sender(transport)
    ctx = SendContext({"WHATSAPP_AGENT_TOKEN": "token"}, {})
    asyncio.run(sender(OutboundMessage("hello", "https://lodge/tui?session=x"),
                       {"participant": "user:123"}, ctx))
    assert sent == [("token", "user:123", "hello\n\nOpen session: https://lodge/tui?session=x")]


def test_shared_request_budget_waits_for_expiration(tmp_path):
    now = [0.0]
    sleeps = []
    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
    for _ in range(3):
        pace_request(tmp_path, "token", 2, "poll", clock=lambda: now[0], sleep=sleep)
    assert sleeps and now[0] >= 60


def test_poll_204_preserves_cursor_and_malformed_batch_does_not_advance(monkeypatch, tmp_path):
    adapter = _load_adapter(monkeypatch, tmp_path)
    path = tmp_path / "cursor.json"
    state = {"offset": 7, "activation": 100, "processed": []}
    result = adapter.poll_once(state.copy(), {}, 100, cursor_path=path,
                               request=lambda *a: (204, {}), dispatch=lambda *a: None)
    assert result["offset"] == 7 and not path.exists()
    with __import__("pytest").raises(ValueError):
        adapter.poll_once(state.copy(), {}, 100, cursor_path=path,
                          request=lambda *a: (200, {"entry": []}), dispatch=lambda *a: None)
    assert not path.exists()


def test_dispatch_failure_is_marked_before_recovery_without_replay(monkeypatch, tmp_path):
    adapter = _load_adapter(monkeypatch, tmp_path)
    path = tmp_path / "cursor.json"
    body = {"next_offset": 2, "entry": [{"changes": [{"value": {"messages": [
        {"id": "m1", "from": "user:1", "timestamp": "100", "type": "text", "text": {"body": "synthetic"}}
    ]}}]}]}
    state = {"offset": 0, "processed": []}
    def interrupted(*args):
        raise KeyboardInterrupt("simulated process interruption")
    with __import__("pytest").raises(KeyboardInterrupt, match="interruption"):
        adapter.poll_once(state, {}, 100, cursor_path=path,
                          request=lambda *a: (200, body), dispatch=interrupted)
    recovered = json.loads(path.read_text())
    delivered = []
    adapter.poll_once(recovered, {}, 100, cursor_path=path,
                      request=lambda *a: (200, body),
                      dispatch=lambda *args: delivered.append(args[0]["id"]))
    assert delivered == []
    assert recovered["processed"] == ["m1"]


def test_item_failure_does_not_drop_later_messages_in_checkpointed_batch(monkeypatch, tmp_path):
    adapter = _load_adapter(monkeypatch, tmp_path)
    path = tmp_path / "cursor.json"
    batch = {"next_offset": 3, "entry": [{"changes": [{"value": {"messages": [
        {"id": "bad", "from": "user:1", "timestamp": "100", "type": "text", "text": {"body": "synthetic"}},
        None,
        {"id": "good", "from": "user:1", "timestamp": "100", "type": "text", "text": {"body": "synthetic"}},
    ]}}]}]}
    delivered = []
    def dispatch(item, routes, activation):
        if item["id"] == "bad":
            raise RuntimeError("item-specific failure")
        delivered.append(item["id"])
    state = adapter.poll_once({"offset": 0, "processed": []}, {}, 100,
                              cursor_path=path, request=lambda *a: (200, batch), dispatch=dispatch)
    assert delivered == ["good"]
    assert state["processed"] == ["bad", "good"]
    assert state["offset"] == 3


@__import__("pytest").mark.parametrize(("code", "expected"), [(401, "API key"), (409, "one active poller")])
def test_authentication_and_poll_conflict_are_actionable_terminal_errors(monkeypatch, tmp_path, code, expected):
    adapter = _load_adapter(monkeypatch, tmp_path)
    monkeypatch.setattr(adapter, "pace_request", lambda *a: None)
    def rejected(request, timeout):
        raise __import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
            request.full_url, code, "rejected", {}, None
        )
    monkeypatch.setattr(adapter.urllib.request, "urlopen", rejected)
    with __import__("pytest").raises(adapter.WhatsAppConfigurationError, match=expected):
        adapter.api("GET", "/updates?offset=0")


def test_documented_bad_request_error_100_is_terminal_and_redacted(monkeypatch, tmp_path):
    adapter = _load_adapter(monkeypatch, tmp_path)
    monkeypatch.setattr(adapter, "pace_request", lambda *a: None)
    private_detail = "secret response detail"
    def rejected(request, timeout):
        raise __import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
            request.full_url, 400, "rejected", {}, io.BytesIO(
                json.dumps({"error": {"code": 100, "message": private_detail}}).encode()
            )
        )
    monkeypatch.setattr(adapter.urllib.request, "urlopen", rejected)
    with __import__("pytest").raises(adapter.WhatsAppConfigurationError, match="HTTP 400, error 100") as error:
        adapter.api("GET", "/updates?offset=0")
    assert "API key" in str(error.value)
    assert private_detail not in str(error.value)


def test_activation_filter_and_persisted_dedup(monkeypatch, tmp_path):
    adapter = _load_adapter(monkeypatch, tmp_path)
    path = tmp_path / "cursor.json"
    delivered = []
    batch = {"next_offset": 11, "entry": [{"changes": [{"value": {"messages": [
        {"id": "old", "from": "user:1", "timestamp": "99", "type": "text", "text": {"body": "old"}},
        {"id": "new", "from": "user:1", "timestamp": "100", "type": "text", "text": {"body": "new"}},
        {"id": "new", "from": "user:1", "timestamp": "100", "type": "text", "text": {"body": "new"}},
    ]}}]}]}
    state = {"offset": 0, "activation": 100, "processed": []}
    req = lambda *a: (200, batch)
    dispatch = lambda item, routes, activated: delivered.append(item["id"])
    adapter.poll_once(state, {}, 100, cursor_path=path, request=req, dispatch=dispatch)
    assert delivered == ["new"]
    restarted = json.loads(path.read_text())
    adapter.poll_once(restarted, {}, 100, cursor_path=path, request=req, dispatch=dispatch)
    assert delivered == ["new"]
    assert restarted["offset"] == 11


def test_stale_wolt_selection_is_cleared(monkeypatch, tmp_path):
    adapter = _load_adapter(monkeypatch, tmp_path)
    outgoing = []
    monkeypatch.setattr(adapter, "api", lambda *args: (200, outgoing.append(args) or {}))
    monkeypatch.setattr(adapter, "eligible_wolts", lambda: {})
    routes = {"user:1": {"wolt": "beaver", "session": "old"}}
    adapter.handle({"id": "m", "from": "user:1", "timestamp": "100",
                    "type": "text", "text": {"body": "hello"}}, routes, 100)
    assert routes == {}
    assert "no longer available" in outgoing[-1][2]["text"]["body"]


def test_server_sender_holds_shared_lock_without_blocking_event_loop(monkeypatch, tmp_path):
    import httpx
    import server.config as config
    import server.notification_senders as senders
    monkeypatch.setattr(config, "SPACE_DIR", tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    class Response:
        status_code = 200
        def json(self): return {"ok": True}

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def post(self, *args, **kwargs):
            calls.append(args[0])
            if len(calls) == 1:
                entered.set()
                await release.wait()
            return Response()

    monkeypatch.setattr(httpx, "AsyncClient", Client)

    async def run():
        first = asyncio.create_task(senders.whatsapp_send("same-key", "user:1", "one"))
        await entered.wait()
        second = asyncio.create_task(senders.whatsapp_send("same-key", "user:1", "two"))
        await asyncio.sleep(0.03)
        assert len(calls) == 1
        release.set()
        await asyncio.gather(first, second)
    asyncio.run(run())
    assert len(calls) == 2


def test_cancellation_while_waiting_for_async_lock_does_not_orphan_it(tmp_path):
    from woltspace.whatsapp_rate_limit import async_request_slot

    async def run():
        first_ready = asyncio.Event()
        release = asyncio.Event()
        async def holder():
            async with async_request_slot(tmp_path, "key", 12, "send"):
                first_ready.set()
                await release.wait()
        first = asyncio.create_task(holder())
        await first_ready.wait()
        waiting = asyncio.create_task(async_request_slot(tmp_path, "key", 12, "send").__aenter__())
        await asyncio.sleep(0.03)
        waiting.cancel()
        first.cancel()
        with __import__("pytest").raises(asyncio.CancelledError):
            await first
        with __import__("pytest").raises(asyncio.CancelledError):
            await waiting
        async with async_request_slot(tmp_path, "key", 12, "send"):
            return True
    assert asyncio.run(run())


def test_real_session_registry_keeps_account_fingerprint(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "container" / "lib"))
    source = ROOT / "container" / "lib" / "sessions.py"
    spec = importlib.util.spec_from_file_location("whatsapp_test_sessions", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    SessionRegistry = module.SessionRegistry
    registry = SessionRegistry(tmp_path)
    registry.create("wa-session", wolt="beaver", adapter="whatsapp",
                    chat_id="user:7", whatsapp_account="account-hash")
    stored = registry.get("wa-session", check_alive=False)
    assert stored["routing"] == [{
        "adapter": "whatsapp", "chat_id": "user:7",
        "whatsapp_account": "account-hash",
    }]


def test_session_notify_rejects_rotated_key_and_missing_recipient(monkeypatch):
    import server.notify as notify
    env = {"WHATSAPP_AGENT_TOKEN": "rotated-key", "TELEGRAM_BOT_TOKEN": "tg",
           "TELEGRAM_ALLOWED_USERS": "123"}
    monkeypatch.setattr(notify, "dotenv_env", lambda key: env.get(key, ""))
    record = {"adapter": "whatsapp", "chat_id": "user:7", "whatsapp_account": "old-account"}
    with __import__("pytest").raises(RuntimeError, match="different Agent API key"):
        asyncio.run(notify._send("whatsapp", "wa-session", "reply", "user:7",
                                routing=record, session_record=record))
    monkeypatch.setattr(notify, "read_session_registry", lambda _name: {
        "adapter": "whatsapp", "chat_id": "", "whatsapp_account": "x"
    })
    with __import__("pytest").raises(RuntimeError, match="no bound participant"):
        asyncio.run(notify.send_notification("wa-session", "reply"))
    with __import__("pytest").raises(RuntimeError, match="participant is required"):
        asyncio.run(notify.send_notification("", "reply", {
            "adapter": "whatsapp", "participant": "",
        }))


def test_registry_to_notify_to_mocked_whatsapp_sender(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / "container" / "lib"))
    spec = importlib.util.spec_from_file_location(
        "whatsapp_integration_sessions", ROOT / "container" / "lib" / "sessions.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    key = "the-active-key"
    account = __import__("hashlib").sha256(key.encode()).hexdigest()[:20]
    registry = module.SessionRegistry(tmp_path)
    registry.create("wa-integration", wolt="beaver", adapter="whatsapp",
                    chat_id="user:99", whatsapp_account=account)
    import server.notify as notify
    monkeypatch.setattr(notify, "WOLTS_DIR", tmp_path)
    monkeypatch.setattr(notify, "dotenv_env", lambda name: key if name == "WHATSAPP_AGENT_TOKEN" else "")
    monkeypatch.setattr(notify, "append_chat_history", lambda *args: None)
    sent = []
    async def transport(token, destination, text):
        sent.append((token, destination, text))
    monkeypatch.setattr(notify, "whatsapp_send", transport)
    result = asyncio.run(notify.send_notification("wa-integration", "session reply"))
    assert result == {"adapter": "whatsapp", "participant": "user:99"}
    assert sent == [(key, "user:99", "session reply")]


def test_successful_session_start_with_ack_timeout_does_not_claim_start_failed(monkeypatch, tmp_path):
    adapter = _load_adapter(monkeypatch, tmp_path)
    starts = []
    def start(prompt, *, wolt, routing):
        starts.append((prompt, wolt, routing))
        return {"name": "session-new", "url": "https://lodge/session-new"}
    monkeypatch.setattr(adapter, "start_claude_session", start)
    monkeypatch.setattr(adapter, "eligible_wolts", lambda: {"beaver": {}})
    sends = []
    def fail_ack(method, path, payload):
        sends.append(payload["text"]["body"])
        raise TimeoutError("ambiguous acknowledgement timeout")
    monkeypatch.setattr(adapter, "api", fail_ack)
    routes = {"user:1": {"wolt": "beaver"}}
    adapter.handle({"id": "m", "from": "user:1", "timestamp": "100", "type": "text",
                    "text": {"body": "please do this once"}}, routes, 100)
    assert len(starts) == 1
    assert routes["user:1"]["session"] == "session-new"
    assert sends == ["Session started: https://lodge/session-new"]


def test_route_persistence_failure_after_session_start_does_not_invite_retry(monkeypatch, tmp_path):
    adapter = _load_adapter(monkeypatch, tmp_path)
    starts = []
    def start(prompt, *, wolt, routing):
        starts.append((prompt, wolt, routing))
        return {"name": "session-persisted-nowhere", "url": "https://lodge/session"}
    monkeypatch.setattr(adapter, "start_claude_session", start)
    monkeypatch.setattr(adapter, "eligible_wolts", lambda: {"beaver": {}})
    monkeypatch.setattr(adapter, "atomic_json", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    replies = []
    monkeypatch.setattr(adapter, "api", lambda method, path, body: replies.append(body["text"]["body"]) or (200, {}))
    routes = {"user:1": {"wolt": "beaver"}}
    adapter.handle({"id": "m", "from": "user:1", "timestamp": "100", "type": "text",
                    "text": {"body": "do this once"}}, routes, 100)
    assert len(starts) == 1
    assert routes["user:1"]["session"] == "session-persisted-nowhere"
    assert len(replies) == 1
    assert "session started" in replies[0].lower()
    assert "before sending another message" in replies[0].lower()
    assert "try again" not in replies[0].lower()


def test_ambiguous_session_start_timeout_does_not_invite_blind_resubmission(monkeypatch, tmp_path):
    adapter = _load_adapter(monkeypatch, tmp_path)
    monkeypatch.setattr(adapter, "eligible_wolts", lambda: {"beaver": {}})
    monkeypatch.setattr(adapter, "start_claude_session", lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError()))
    replies = []
    monkeypatch.setattr(adapter, "api", lambda method, path, body: replies.append(body["text"]["body"]) or (200, {}))
    adapter.handle({"id": "m", "from": "user:1", "timestamp": "100", "type": "text",
                    "text": {"body": "do this once"}}, {"user:1": {"wolt": "beaver"}}, 100)
    assert len(replies) == 1
    assert "could not confirm" in replies[0].lower()
    assert "check your sessions" in replies[0].lower()
    assert "try again" not in replies[0].lower()


def test_adapter_removes_whatsapp_token_from_its_environment(monkeypatch, tmp_path):
    adapter = _load_adapter(monkeypatch, tmp_path)
    sentinel = "synthetic-whatsapp-secret"
    monkeypatch.setenv("WHATSAPP_AGENT_TOKEN", sentinel)
    importlib.reload(adapter)
    assert adapter.TOKEN == sentinel
    assert "WHATSAPP_AGENT_TOKEN" not in __import__("os").environ


def test_repeated_cancellation_closes_late_file_lock(monkeypatch, tmp_path):
    import threading
    import woltspace.whatsapp_rate_limit as limiter

    acquired = threading.Event()
    release_worker = threading.Event()
    closed = threading.Event()
    class Lock:
        def close(self):
            closed.set()
    def slow_acquire(*args, **kwargs):
        acquired.set()
        release_worker.wait(2)
        return Lock()
    monkeypatch.setattr(limiter, "acquire_slot", slow_acquire)

    async def run():
        from woltspace.whatsapp_rate_limit import async_request_slot
        waiting = asyncio.create_task(
            async_request_slot(tmp_path, "key", 12, "send").__aenter__()
        )
        assert await asyncio.to_thread(acquired.wait, 2)
        waiting.cancel()
        await asyncio.sleep(0.02)
        waiting.cancel()
        with __import__("pytest").raises(asyncio.CancelledError):
            await waiting
        release_worker.set()
        assert await asyncio.to_thread(closed.wait, 2)
    asyncio.run(run())




def test_invalid_key_get_metadata_is_typed_without_body(monkeypatch, tmp_path):
    import urllib.error
    adapter = _load_adapter(monkeypatch, tmp_path)
    def transport(*args, **kwargs):
        raise urllib.error.HTTPError("https://private.invalid", 400, "private", {},
            io.BytesIO(b'{"error":{"code":100,"message":"private-key"}}'))
    monkeypatch.setattr(adapter.urllib.request, "urlopen", transport)
    with __import__("pytest").raises(adapter.WhatsAppConfigurationError) as caught:
        adapter._api_request("GET", "/updates?offset=0")
    assert caught.value.http_status == 400 and caught.value.provider_code == 100
    assert "private-key" not in str(caught.value)
