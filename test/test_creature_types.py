"""Custom creatures: a new name and emoji on one of the three sizes.

Covers the acceptance checks agreed for the feature: nothing changes for a
lodge without custom creatures, a custom creature behaves like its size but
keeps its own identity everywhere it is shown, model precedence, refused
writes leave the settings file intact, and an assignment applies from the
next session only.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))

import creature_types  # noqa: E402
import harnesses  # noqa: E402
from creature_types import (  # noqa: E402
    add_custom, base_type, creature_emoji, custom_creatures, emoji_from, remove_custom,
)
from harnesses import creature_model, resolve_model, tier_default_model  # noqa: E402

STAR = {"emoji": "⭐", "size": "raccoon", "models": {"codex": "gpt-5.5"}, "role": "Reviewer"}


@pytest.fixture
def lodge(tmp_path, monkeypatch):
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(tmp_path))
    (tmp_path / "woltspace.json").write_text(json.dumps({
        "harness": {"default": "claude"},
        "creatures": {"active_wolf": "beta"},
    }))
    return tmp_path


def _wolt(root, name, **cfg):
    home = root / name / "wolt"
    home.mkdir(parents=True)
    (home / "wolt.json").write_text(json.dumps({"name": name, **cfg}, indent=2))
    return home / "wolt.json"


def _settings(root):
    return json.loads((root / "woltspace.json").read_text())


# --- no custom creatures: nothing changes ---------------------------------

def test_without_custom_creatures_built_ins_resolve_as_before(lodge):
    assert custom_creatures() == {}
    for t in ("raccoon", "beaver", "otter", "rodent", "wolf", "dog"):
        assert base_type(t) == t
    for harness in ("claude", "codex"):
        for tier in ("raccoon", "beaver", "otter", "wolf"):
            assert creature_model(harness, tier) == tier_default_model(harness, tier)


def test_built_in_emoji_stay_what_each_surface_always_said(lodge):
    add_custom("star", STAR)
    telegram = {"raccoon": "🦝", "beaver": "🦫", "otter": "🦦", "rodent": "🦫", "wolf": "🐺", "dog": "🐶"}
    for t, emoji in telegram.items():
        assert emoji_from(telegram, t, "🦫") == emoji
    assert emoji_from(telegram, "spider", "🦫") == "🦫"
    assert emoji_from(telegram, "star", "🦫") == "⭐"
    assert creature_emoji("raccoon") == "🦝"


def test_wolts_module_without_custom_creatures(lodge):
    from wolts import is_rodent, is_valid_type

    assert is_rodent("beaver") and is_rodent("rodent")
    assert not is_rodent("wolf") and not is_rodent("star")
    assert not is_valid_type("star")


# --- identity vs behaviour ------------------------------------------------

def test_custom_creature_behaves_like_its_size_and_keeps_its_identity(lodge):
    from wolts import is_rodent, is_valid_type

    add_custom("star", STAR)
    assert base_type("star") == "raccoon"
    assert creature_emoji("star") == "⭐"
    assert is_rodent("star") and is_valid_type("star")


def test_model_precedence_pin_then_creature_then_size(lodge):
    add_custom("star", STAR)
    # the creature's own model on codex
    assert resolve_model("codex", "star") == "gpt-5.5"
    # the wolt's own pin wins over it
    assert resolve_model("codex", "star", "gpt-5.6-sol") == "gpt-5.6-sol"
    # an engine the creature names no model for: its size's default
    assert resolve_model("claude", "star") == tier_default_model("claude", "raccoon")


def test_a_creature_model_the_engine_no_longer_offers_falls_back_to_the_size(lodge):
    settings = _settings(lodge)
    settings["creatures"]["custom"] = {"star": {"emoji": "⭐", "size": "raccoon", "models": {"codex": "gone"}}}
    (lodge / "woltspace.json").write_text(json.dumps(settings))
    assert creature_model("codex", "star") == tier_default_model("codex", "raccoon")


def test_a_hand_edited_bad_entry_is_ignored(lodge):
    settings = _settings(lodge)
    settings["creatures"]["custom"] = {"wolf": {"emoji": "⭐", "size": "raccoon"},
                                       "odd": {"emoji": "⭐", "size": "eagle"}}
    (lodge / "woltspace.json").write_text(json.dumps(settings))
    assert custom_creatures() == {}
    assert base_type("odd") == "odd"


# --- writes ---------------------------------------------------------------

@pytest.mark.parametrize("cid, entry", [
    ("Star", STAR),
    ("raccoon", STAR),
    ("wolf", STAR),
    ("star", {**STAR, "size": "wolf"}),
    ("star", {**STAR, "emoji": "x"}),
    ("star", {**STAR, "emoji": "⭐ ⭐"}),
    ("star", {**STAR, "models": {"codex": "not-a-model"}}),
    ("star", {**STAR, "models": {"nope": "gpt-5.5"}}),
    ("star", {**STAR, "lore": "new"}),
])
def test_invalid_creatures_are_refused_without_touching_settings(lodge, cid, entry):
    before = (lodge / "woltspace.json").read_bytes()
    with pytest.raises(ValueError):
        add_custom(cid, entry)
    assert (lodge / "woltspace.json").read_bytes() == before


def test_add_keeps_every_other_setting_and_refuses_overwrite(lodge):
    add_custom("star", STAR)
    settings = _settings(lodge)
    assert settings["creatures"]["active_wolf"] == "beta"
    assert settings["harness"] == {"default": "claude"}
    assert settings["creatures"]["custom"]["star"]["size"] == "raccoon"
    with pytest.raises(ValueError, match="already exists"):
        add_custom("star", {**STAR, "emoji": "🌟"})
    assert custom_creatures()["star"]["emoji"] == "⭐"


def test_an_unreadable_settings_file_is_never_replaced(lodge):
    (lodge / "woltspace.json").write_text("{not json")
    with pytest.raises(ValueError, match="could not be read"):
        add_custom("star", STAR)
    assert (lodge / "woltspace.json").read_text() == "{not json"


def test_remove_refuses_a_creature_a_wolt_still_is(lodge):
    add_custom("star", STAR)
    _wolt(lodge, "alpha", type="star")
    with pytest.raises(ValueError, match="alpha"):
        remove_custom("star")
    assert "star" in custom_creatures()
    (lodge / "alpha" / "wolt" / "wolt.json").write_text(json.dumps({"name": "alpha", "type": "raccoon"}))
    remove_custom("star")
    assert "star" not in custom_creatures()
    assert _settings(lodge)["creatures"]["active_wolf"] == "beta"


# --- creating a wolt of a custom creature ---------------------------------

def test_create_a_custom_creature_wolt_gets_a_site_its_role_and_base_lore(lodge):
    import wolts

    add_custom("star", STAR)
    with patch("wolts.WOLTS_DIR", lodge), patch("wolts.setup_wolt_claude_config"), \
            patch("wolts.subprocess.run"):
        wolts.create_creature_wolt("alpha", "star")
    cfg = json.loads((lodge / "alpha" / "wolt" / "wolt.json").read_text())
    assert cfg["type"] == "star" and cfg["role"] == "Reviewer"
    index = (lodge / "alpha" / "wolt" / "site" / "index.html").read_text()
    assert "⭐" in index
    claude_md = (lodge / "alpha" / "CLAUDE.md").read_text()
    assert "raccoon-sized: it works like a raccoon" in claude_md
    assert "## Native wolts" in claude_md  # the platform lore every wolt boots with


# --- the lodge API --------------------------------------------------------

@pytest.fixture
def client(lodge, monkeypatch):
    from server import app as server_app

    monkeypatch.setattr(server_app, "WOLTS_DIR", lodge)
    monkeypatch.setattr(server_app, "get_default_harness", lambda: "claude")
    monkeypatch.setattr(harnesses, "_woltspace_json_path", lambda: lodge / "woltspace.json")
    return TestClient(server_app.app, base_url="http://localhost:7777")


def test_api_add_list_remove(client, lodge):
    r = client.post("/creatures", json={"id": "star", **STAR})
    assert r.status_code == 200, r.text
    listed = client.get("/creatures").json()
    assert [s["id"] for s in listed["sizes"]] == ["otter", "beaver", "raccoon"]
    assert listed["custom"][0]["id"] == "star" and listed["custom"][0]["wolts"] == []
    assert client.post("/creatures", json={"id": "star", **STAR}).status_code == 400
    assert client.delete("/creatures/star").status_code == 200
    assert client.get("/creatures").json()["custom"] == []


def test_assign_changes_only_the_type_and_reports_what_the_wolt_keeps(client, lodge):
    add_custom("star", STAR)
    path = _wolt(lodge, "alpha", type="raccoon", role="Planner", harness="codex", model="gpt-6-astra")
    r = client.post("/wolts/alpha/creature", json={"type": "star"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["emoji"] == "⭐" and body["previous_type"] == "raccoon"
    assert body["kept"] == {"harness": "codex", "model": "gpt-6-astra", "role": "Planner"}
    assert body["applies"] == "next session"
    cfg = json.loads(path.read_text())
    assert cfg == {"name": "alpha", "type": "star", "role": "Planner",
                   "harness": "codex", "model": "gpt-6-astra"}


def test_assign_without_a_pin_uses_the_creatures_model(client, lodge):
    add_custom("star", STAR)
    _wolt(lodge, "alpha", type="raccoon", harness="codex")
    body = client.post("/wolts/alpha/creature", json={"type": "star"}).json()
    assert body["model"] == "gpt-5.5"


def test_assign_follows_a_starter_site_emoji_but_not_a_chosen_one(client, lodge):
    add_custom("star", STAR)
    _wolt(lodge, "alpha", type="raccoon")
    _wolt(lodge, "kit", type="raccoon")
    for name, emoji in (("alpha", "🦝"), ("kit", "🦊")):
        site = lodge / name / "wolt" / "site"
        site.mkdir()
        (site / "site.json").write_text(json.dumps({"title": name, "tokens": {"emoji": emoji}}))
    assert client.post("/wolts/alpha/creature", json={"type": "star"}).json()["site_emoji_updated"]
    assert not client.post("/wolts/kit/creature", json={"type": "star"}).json()["site_emoji_updated"]
    assert json.loads((lodge / "alpha/wolt/site/site.json").read_text())["tokens"]["emoji"] == "⭐"
    assert json.loads((lodge / "kit/wolt/site/site.json").read_text())["tokens"]["emoji"] == "🦊"


def test_assign_refuses_unknown_types_and_leaves_wolf_and_dog_alone(client, lodge):
    _wolt(lodge, "beta", type="wolf")
    path = _wolt(lodge, "alpha", type="raccoon")
    before = path.read_bytes()
    assert client.post("/wolts/alpha/creature", json={"type": "dragon"}).status_code == 400
    assert client.post("/wolts/alpha/creature", json={"type": "wolf"}).status_code == 400
    assert client.post("/wolts/beta/creature", json={"type": "beaver"}).status_code == 400
    assert path.read_bytes() == before


def test_wolts_list_shows_the_custom_emoji_and_base_type(client, lodge):
    add_custom("star", STAR)
    _wolt(lodge, "alpha", type="star")
    _wolt(lodge, "pal", type="raccoon")
    by_name = {w["name"]: w for w in client.get("/wolts").json()}
    assert (by_name["alpha"]["emoji"], by_name["alpha"]["base_type"]) == ("⭐", "raccoon")
    assert (by_name["pal"]["emoji"], by_name["pal"]["base_type"]) == ("🦝", "raccoon")


def test_lodge_create_route_accepts_a_custom_creature(client, lodge, monkeypatch):
    from server import app as server_app

    add_custom("star", STAR)
    made = {}
    monkeypatch.setattr(server_app, "harness_install_problem", lambda *a, **k: None)
    monkeypatch.setattr("wolts.create_creature_wolt", lambda name, t, **k: made.update(name=name, type=t))
    monkeypatch.setattr(server_app, "start_session", lambda **k: {"name": "alpha-x"})
    r = client.post("/sessions/new/create", json={"name": "alpha", "type": "star"})
    assert r.status_code == 200, r.text
    assert made == {"name": "alpha", "type": "star"}
    assert client.post("/sessions/new/create", json={"name": "rex", "type": "dragon"}).status_code == 400


# --- next session only ----------------------------------------------------

def test_a_running_session_keeps_its_model_and_the_next_one_gets_the_change(
    lodge, monkeypatch, fake_runtime
):
    """The model is chosen once, at spawn, and stored on the session; resume
    builds its command from that record, not from wolt.json. So assigning a
    creature only changes what a new session gets."""
    import paths
    import sessions

    monkeypatch.setattr(sessions, "WOLTS_DIR", lodge)
    monkeypatch.setattr(paths, "WOLTS_DIR", lodge)
    monkeypatch.setenv("WOLTSPACE_ISOLATION", "host")
    add_custom("quick", {"emoji": "⚡", "size": "otter"})
    path = _wolt(lodge, "alpha", type="raccoon", harness="claude", role="Planner")
    (lodge / "alpha" / "wolt" / "site").mkdir()

    running = sessions.start_session(wolt="alpha")
    sessions.prepare_session_command(running["name"], "spawn")  # stamps the conversation id
    raccoon_model = tier_default_model("claude", "raccoon")
    assert running["model"] == raccoon_model

    cfg = json.loads(path.read_text())
    cfg["type"] = "quick"
    path.write_text(json.dumps(cfg))

    resumed = sessions.prepare_session_command(running["name"], "resume")
    assert f"--model {raccoon_model}" in resumed
    record = sessions.SessionRegistry(lodge).get(running["name"], check_alive=False)
    assert (record["creature"], record["model"]) == ("raccoon", raccoon_model)
    assert json.loads(path.read_text())["role"] == "Planner"  # the wolt's role is its own
    following = sessions.start_session(wolt="alpha")
    assert following["model"] == tier_default_model("claude", "otter") != raccoon_model
    assert following["creature"] == "quick"


# --- concurrent writers ---------------------------------------------------
# Each test pauses one writer inside its read-modify-write and starts another
# writer of the same file. The second must wait, and both changes must land.

import threading  # noqa: E402


def _paused(target, *, first_only=True):
    """Wrap target so its first call stops until released."""
    inside, release = threading.Event(), threading.Event()
    calls = {"n": 0}

    def wrapper(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1 or not first_only:
            inside.set()
            assert release.wait(5)
        return target(*args, **kwargs)
    return wrapper, inside, release


def _run(fn, *args):
    box = {}

    def go():
        try:
            box["result"] = fn(*args)
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc
    t = threading.Thread(target=go)
    t.start()
    return t, box


def test_removing_a_creature_waits_for_an_assignment_in_flight(client, lodge, monkeypatch):
    from server import app as server_app

    add_custom("star", STAR)
    _wolt(lodge, "alpha", type="raccoon")
    wrapped, inside, release = _paused(server_app._write_wolt_settings)
    monkeypatch.setattr(server_app, "_write_wolt_settings", wrapped)

    assign, _ = _run(server_app._assign_creature, "alpha", "star")
    assert inside.wait(5)
    remove, removed = _run(remove_custom, "star")
    remove.join(0.3)
    assert remove.is_alive()  # waits for the assignment
    release.set()
    assign.join(5)
    remove.join(5)
    assert "still the creature of: alpha" in str(removed["error"])
    assert "star" in custom_creatures()


def test_adding_a_creature_keeps_a_concurrent_default_harness_change(lodge, monkeypatch):
    wrapped, inside, release = _paused(creature_types._custom_section)
    monkeypatch.setattr(creature_types, "_custom_section", wrapped)

    add, _ = _run(add_custom, "star", STAR)
    assert inside.wait(5)
    other, _ = _run(harnesses.set_default_harness, "codex")
    other.join(0.3)
    assert other.is_alive()
    release.set()
    add.join(5)
    other.join(5)
    settings = _settings(lodge)
    assert settings["harness"]["default"] == "codex"
    assert "star" in settings["creatures"]["custom"]
    assert not list(lodge.glob("*.tmp"))


def test_assignment_and_a_settings_change_both_land(client, lodge, monkeypatch):
    from server import app as server_app

    add_custom("star", STAR)
    path = _wolt(lodge, "alpha", type="raccoon", harness="codex")
    wrapped, inside, release = _paused(server_app._write_wolt_settings)
    monkeypatch.setattr(server_app, "_write_wolt_settings", wrapped)

    assign, _ = _run(server_app._assign_creature, "alpha", "star")
    assert inside.wait(5)
    patch_, _ = _run(server_app._update_wolt_settings, "alpha", {"model": "gpt-5.6-sol"})
    patch_.join(0.3)
    assert patch_.is_alive()
    release.set()
    assign.join(5)
    patch_.join(5)
    cfg = json.loads(path.read_text())
    assert (cfg["type"], cfg["model"]) == ("star", "gpt-5.6-sol")


def test_two_assignments_leave_the_site_with_the_last_creatures_emoji(client, lodge, monkeypatch):
    from server import app as server_app

    add_custom("star", STAR)
    add_custom("quick", {"emoji": "⚡", "size": "otter"})
    _wolt(lodge, "alpha", type="raccoon")
    site = lodge / "alpha" / "wolt" / "site"
    site.mkdir()
    (site / "site.json").write_text(json.dumps({"tokens": {"emoji": "🦝"}}))
    wrapped, inside, release = _paused(server_app._follow_site_emoji)
    monkeypatch.setattr(server_app, "_follow_site_emoji", wrapped)

    first, _ = _run(server_app._assign_creature, "alpha", "star")
    assert inside.wait(5)
    second, _ = _run(server_app._assign_creature, "alpha", "quick")
    second.join(0.3)
    assert second.is_alive()
    release.set()
    first.join(5)
    second.join(5)
    assert json.loads((lodge / "alpha/wolt/wolt.json").read_text())["type"] == "quick"
    assert json.loads((site / "site.json").read_text())["tokens"]["emoji"] == "⚡"


def test_a_name_a_page_keeps_its_own_emoji_for_is_reserved(lodge):
    with pytest.raises(ValueError, match="built-in"):
        add_custom("eagle", STAR)


@pytest.mark.parametrize("failure", ["replace", "serialize"])
def test_failed_custom_settings_write_keeps_bytes_and_releases_lock(lodge, monkeypatch, failure):
    import settings_files
    from concurrent.futures import ThreadPoolExecutor

    path = lodge / "woltspace.json"
    before = path.read_bytes()
    with monkeypatch.context() as scoped:
        if failure == "replace":
            def refuse_replace(*args):
                raise OSError("dummy replacement failure")
            scoped.setattr(settings_files.os, "replace", refuse_replace)
            expected = OSError
        else:
            def refuse_serialization(*args, **kwargs):
                raise TypeError("dummy serialization failure")
            scoped.setattr(settings_files.json, "dumps", refuse_serialization)
            expected = TypeError
        with pytest.raises(expected):
            add_custom("star", STAR)
    assert path.read_bytes() == before
    assert not list(lodge.glob(".woltspace.json.*.tmp"))
    # A different thread must acquire the flock after the failed writer exits.
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(add_custom, "star", STAR).result(timeout=5)
    assert custom_creatures()["star"]["emoji"] == "⭐"
    assert _settings(lodge)["harness"] == {"default": "claude"}
    remove_custom("star")
    assert "star" not in custom_creatures()
    assert _settings(lodge)["creatures"]["active_wolf"] == "beta"
