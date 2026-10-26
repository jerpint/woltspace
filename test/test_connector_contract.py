"""The chat connector contract (RFC #617): manifests, typed commands, neutral replies."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "container" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "container"))

import lodge_commands  # noqa: E402
from connector_contract import (  # noqa: E402
    Choice, Command, Manifest, ManifestError, Reply, Route, parse_command, plain_text_reply,
)

NAMES = lodge_commands.COMMAND_NAMES


# --- manifests ---------------------------------------------------------------

def test_a_manifest_declares_what_a_connector_can_do():
    m = Manifest("matrix", capabilities=frozenset({"text", "reactions"}),
                 command_input=frozenset({"text"}), command_prefix="!",
                 route_fields=("room_id",), secrets=("MATRIX_TOKEN",))
    assert m.can("reactions") and not m.can("files")


@pytest.mark.parametrize("kwargs", [
    {"protocol": 2},
    {"capabilities": frozenset({"telepathy"})},
    {"command_input": frozenset({"voice"})},
    {"events": frozenset({"poke"})},
    {"command_prefix": "#"},
])
def test_an_unknown_manifest_fails_closed(kwargs):
    with pytest.raises(ManifestError):
        Manifest("matrix", **kwargs)


@pytest.mark.parametrize("name", ["", "Telegram", "9lives", "a b", "x" * 40])
def test_a_connector_name_is_a_short_slug(name):
    with pytest.raises(ManifestError):
        Manifest(name)


def test_routes_are_tagged_by_connector():
    a = Route("telegram", "42")
    b = Route("slack", "42")
    assert a.key() != b.key()
    assert Route("slack", "C1", "1.2").key() != Route("slack", "C1").key()


# --- typed commands from text: the six safety rules ---------------------------

def test_an_exact_registered_command_is_a_command():
    assert parse_command("/wolt softie", NAMES) == Command("wolt", ("softie",))
    assert parse_command("/sessions", NAMES) == Command("sessions")


@pytest.mark.parametrize("text", [
    "/Users/me/notes.md",       # a path is not a command
    "/compact",                 # an engine command goes to the wolt
    "/wolts",                   # only exact names
    "hello /wolt softie",       # only at the very start
    " /wolt softie",            # a leading space escapes
    "//wolt softie",            # a doubled prefix escapes
    "",
])
def test_anything_else_stays_plain_text(text):
    assert parse_command(text, NAMES) is None


def test_a_command_addressed_to_another_bot_is_not_ours():
    assert parse_command("/wolt@otherbot pal", NAMES, bot_username="lodgebot") is None
    assert parse_command("/wolt@LodgeBot pal", NAMES, bot_username="lodgebot") == Command("wolt", ("pal",))


def test_a_connector_can_use_its_own_prefix():
    assert parse_command("!wolt pal", NAMES, prefix="!") == Command("wolt", ("pal",))
    assert parse_command("/wolt pal", NAMES, prefix="!") is None


# --- neutral replies ---------------------------------------------------------

def test_without_buttons_choices_become_a_numbered_list():
    reply = Reply("**2 apps** · 1 running", ((Choice("📝 blog", url="https://b"), Choice("⏹", action="astop:blog")),))
    assert plain_text_reply(reply) == "2 apps · 1 running\n\n1. 📝 blog (https://b)\n2. ⏹"


def test_the_registry_is_the_menu():
    assert NAMES == ("sessions", "holds", "idle", "wolt", "new", "apps", "kill", "help", "start")
    assert set(lodge_commands.HELP_ORDER) <= set(NAMES)


def test_builtin_manifests_match_the_lodge():
    import connector_registry
    from bot import telegram_adapter
    assert telegram_adapter.MANIFEST is connector_registry.TELEGRAM
    assert connector_registry.manifest("telegram").secrets == ("TELEGRAM_BOT_TOKEN",)
    assert connector_registry.manifest("discord") is None


def test_every_builtin_manifest_has_a_way_out():
    import connector_registry
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from server import notify
    assert set(connector_registry.BUILTIN) <= set(notify.OUTBOUND) <= set(notify.SENDERS)


def test_route_keys_cannot_collide_through_separators():
    a = Route("slack", conversation_id="c", account_id="a:b")
    b = Route("slack", conversation_id="b:c", account_id="a")
    assert a.key() != b.key()
