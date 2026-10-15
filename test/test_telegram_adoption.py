"""The connector is Telegram's default: an adapter-era setup is carried over once, at start."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "container" / "lib"))

from server.connector_commands import allowed_wolts  # noqa: E402
from server.connectors import ConnectorStore  # noqa: E402
from woltspace.channels import TelegramRuntimeConnector, adopt_telegram  # noqa: E402
from woltspace.layout import RuntimeLayout  # noqa: E402

TOKEN = "123456:configured-telegram-token"


@pytest.fixture
def layout(tmp_path):
    wolts = tmp_path / "wolts"
    for name, kind in (("softie", "raccoon"), ("astre", "otter"), ("rex", "dog")):
        (wolts / name / "wolt").mkdir(parents=True)
        (wolts / name / "wolt" / "wolt.json").write_text(json.dumps({"name": name, "type": kind}))
    return RuntimeLayout(wolts_dir=wolts, install_root=ROOT, host="127.0.0.1", port=7799, isolation="host")


def _configure(layout, **telegram):
    layout.platform_state.mkdir(parents=True, exist_ok=True)
    (layout.platform_state / "config.json").write_text(
        json.dumps({"channels": {"telegram": {"enabled": True, "token": TOKEN, **telegram}}})
    )


def _store(layout):
    return ConnectorStore(layout.wolts_dir / ".space" / "connectors")


def test_an_adapter_setup_becomes_an_owner_binding_per_allowed_user(layout):
    _configure(layout)
    chats = layout.wolts_dir / ".space" / "telegram" / "chats"
    chats.mkdir(parents=True)
    (chats / "101.json").write_text(json.dumps({"active_wolt": "astre", "active_session": "astre-calm-owl-1a2b3c"}))
    said = adopt_telegram(layout, {"TELEGRAM_ALLOWED_USERS": "101, 202", "PATH": "/usr/bin"})
    assert "bot 123456" in said and "101, 202" in said
    store = _store(layout)
    [instance] = store.list_instances()
    assert (instance["instance_id"], instance["channel"], instance["account"], instance["enabled"]) == (
        "telegram", "telegram", "123456", True,
    )
    bindings = {b["sender"]: b for b in store.list_bindings("telegram")}
    owner = bindings["101"]
    assert owner["conversation"] == "101"            # their own chat, not groups
    assert owner["default_wolt"] == "astre"          # where the adapter left the chat
    assert all(owner[g] for g in ("may_wake", "any_wolt", "may_create", "may_stop_own_sessions", "may_manage_apps", "may_transcribe"))
    assert store.routing_state("telegram", owner["binding_id"])["active_session"] == "astre-calm-owl-1a2b3c"
    # No saved chat: the first chat wolt.
    assert bindings["202"]["default_wolt"] == "astre"
    # Once: a second start changes nothing.
    assert adopt_telegram(layout, {"TELEGRAM_ALLOWED_USERS": "101,202,303"}) is None
    assert len(store.list_bindings("telegram")) == 2
    # And the connector now has what it needs to launch.
    assert TelegramRuntimeConnector().plan(layout, {"PATH": "/usr/bin"}).enabled is True


def test_any_wolt_is_every_chat_wolt_now_and_later_never_the_dog(layout):
    _configure(layout, allowed_users="101")
    adopt_telegram(layout, {})
    store = _store(layout)
    [binding] = store.list_bindings("telegram")
    assert allowed_wolts(store.binding_now(binding["binding_id"])) == {"softie", "astre"}
    (layout.wolts_dir / "newt" / "wolt").mkdir(parents=True)
    (layout.wolts_dir / "newt" / "wolt" / "wolt.json").write_text(json.dumps({"type": "beaver"}))
    assert "newt" in allowed_wolts(store.binding_now(binding["binding_id"]))


@pytest.mark.parametrize("case", ["adapter_chosen", "disabled", "ambient_only", "bad_token", "already_there"])
def test_nothing_is_carried_over_unless_the_adapter_would_have_run_here(layout, case):
    env = {"TELEGRAM_ALLOWED_USERS": "101"}
    if case == "adapter_chosen":
        _configure(layout)
        (layout.wolts_dir / "woltspace.json").write_text(json.dumps({"telegram": {"runtime": "adapter"}}))
    elif case == "disabled":
        _configure(layout, enabled=False)
    elif case == "ambient_only":
        # Inherited environment alone, not the entrypoint: a stray serve must not take the bot.
        env.update({"ENABLE_TELEGRAM_BOT": "true", "TELEGRAM_BOT_TOKEN": TOKEN})
    elif case == "bad_token":
        _configure(layout, token="not-a-token")
    else:
        _configure(layout)
        _store(layout).add_instance("mine", "telegram", "999")
    assert adopt_telegram(layout, env) is None
    instances = _store(layout).list_instances()
    assert [i["instance_id"] for i in instances] == (["mine"] if case == "already_there" else [])


def test_the_entrypoint_may_adopt_from_its_environment(layout):
    env = {"ENABLE_TELEGRAM_BOT": "true", "TELEGRAM_BOT_TOKEN": TOKEN, "WOLTSPACE_ENTRYPOINT": "1",
           "TELEGRAM_ALLOWED_USERS": "101"}
    assert adopt_telegram(layout, env) is not None
    assert [b["sender"] for b in _store(layout).list_bindings("telegram")] == ["101"]


def test_a_lodge_set_up_before_its_first_wolt_is_adopted_once_one_exists(tmp_path):
    """Astre's rc1 review: no wolt yet must not leave an enabled instance with nobody bound."""
    layout = RuntimeLayout(wolts_dir=tmp_path / "wolts", install_root=ROOT, host="127.0.0.1", port=7799, isolation="host")
    _configure(layout, allowed_users="101")
    assert adopt_telegram(layout, {}) is None
    assert _store(layout).list_instances() == []          # nothing written yet
    (layout.wolts_dir / "softie" / "wolt").mkdir(parents=True)
    (layout.wolts_dir / "softie" / "wolt" / "wolt.json").write_text(json.dumps({"type": "raccoon"}))
    assert adopt_telegram(layout, {}) is not None
    store = _store(layout)
    assert [b["sender"] for b in store.list_bindings("telegram")] == ["101"]
    assert store.list_instances()[0]["enabled"] is True


def test_no_allowed_user_writes_nothing(layout):
    _configure(layout)
    assert adopt_telegram(layout, {}) is None
    assert _store(layout).list_instances() == []


def _saved_chat(layout, user, wolt, session):
    chats = layout.wolts_dir / ".space" / "telegram" / "chats"
    chats.mkdir(parents=True, exist_ok=True)
    (chats / f"{user}.json").write_text(json.dumps({"active_wolt": wolt, "active_session": session}))


@pytest.mark.parametrize("step", ["adopt_first", "adopt_second", "after_route", "enable", "finish"])
def test_an_interrupted_adoption_resumes_at_the_next_start(layout, monkeypatch, step):
    _configure(layout, allowed_users=["101", "202"])
    _saved_chat(layout, "101", "astre", "astre-existing")
    real = {name: getattr(ConnectorStore, name) for name in ("adopt_chat", "enable", "finish_adoption", "_write_json")}
    seen = {"adopt": 0}

    def adopt(self, *args, **kwargs):
        seen["adopt"] += 1
        if (step, seen["adopt"]) in (("adopt_first", 1), ("adopt_second", 2)):
            raise OSError("injected interruption")
        if step == "after_route" and seen["adopt"] == 1:
            # The route is saved, then the registry write (binding + mark) fails.
            def failing_write(path, data):
                if path == self.registry_path and any(b["sender"] == "101" for b in data.get("bindings", ())):
                    raise OSError("injected interruption")
                return real["_write_json"](path, data)
            monkeypatch.setattr(ConnectorStore, "_write_json", staticmethod(failing_write))
            try:
                return real["adopt_chat"](self, *args, **kwargs)
            finally:
                monkeypatch.setattr(ConnectorStore, "_write_json", staticmethod(real["_write_json"]))
        return real["adopt_chat"](self, *args, **kwargs)

    def failing(name):
        def wrapper(self, *args, **kwargs):
            raise OSError("injected interruption")
        return wrapper

    monkeypatch.setattr(ConnectorStore, "adopt_chat", adopt)
    if step in ("enable", "finish"):
        monkeypatch.setattr(ConnectorStore, {"enable": "enable", "finish": "finish_adoption"}[step], failing(step))
    assert "next start resumes" in adopt_telegram(layout, {})
    store = _store(layout)
    assert store.list_instances()[0]["adoption"] == "pending"
    for name, fn in real.items():
        monkeypatch.setattr(ConnectorStore, name, staticmethod(fn) if name == "_write_json" else fn)
    assert "owner chats: 101, 202" in adopt_telegram(layout, {})
    [instance] = store.list_instances()
    assert (instance["adoption"], instance["enabled"]) == ("done", True)
    bindings = {b["sender"]: b for b in store.list_bindings("telegram")}
    assert sorted(bindings) == ["101", "202"]
    # Where the adapter left the chat survives every interruption.
    route = store.routing_state("telegram", bindings["101"]["binding_id"])
    assert (route["active_wolt"], route["active_session"]) == ("astre", "astre-existing")
    # Done is done: later starts change nothing.
    assert adopt_telegram(layout, {"TELEGRAM_ALLOWED_USERS": "101,202,303"}) is None


@pytest.mark.parametrize("change", ["narrowed", "removed"])
def test_resuming_never_brings_back_what_the_owner_changed(layout, monkeypatch, change):
    """Astre's round-2 review: an owner's withdrawal must survive a resumed carry-over."""
    _configure(layout, allowed_users=["101", "202"])
    real = ConnectorStore.adopt_chat

    def second_fails(self, *args, **kwargs):
        if kwargs.get("sender") == "202":
            raise OSError("injected interruption")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(ConnectorStore, "adopt_chat", second_fails)
    adopt_telegram(layout, {})
    monkeypatch.setattr(ConnectorStore, "adopt_chat", real)
    store = _store(layout)
    if change == "narrowed":
        store.bind("telegram", sender="101", conversation="101", default_wolt="softie")
    else:
        assert store.unbind("telegram", sender="101", conversation="101") is True
    adopt_telegram(layout, {})
    bindings = {b["sender"]: b for b in store.list_bindings("telegram")}
    if change == "narrowed":
        assert (bindings["101"]["default_wolt"], bindings["101"]["may_create"], bindings["101"]["any_wolt"]) == ("softie", False, False)
    else:
        assert "101" not in bindings
    assert bindings["202"]["may_create"] is True


def test_a_chat_the_owner_bound_first_is_left_as_it_is(layout):
    _configure(layout, allowed_users=["101"])
    store = _store(layout)
    store.add_instance("telegram", "telegram", "123456", adoption="pending")
    store.bind("telegram", sender="101", conversation="101", default_wolt="softie")
    adopt_telegram(layout, {})
    [binding] = store.list_bindings("telegram")
    assert (binding["default_wolt"], binding["may_create"]) == ("softie", False)
    assert store.list_instances()[0]["adoption"] == "done"
