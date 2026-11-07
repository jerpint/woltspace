"""The Slack connector and the lodge's ChatLodge, wired through the loopback (no socket).

Slack is a fake API object at the edge, fed payloads shaped like Socket Mode's;
the lodge's sessions and the dog are faked at theirs. Covered: the owner-only DM
filter, thread replies under this bot's own messages, typed and slash commands,
button clicks, files, Block Kit rendering and notifications out.
"""

import asyncio
import importlib
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from connector_loopback import Loopback

ROOT = Path(__file__).resolve().parents[1]
for part in ("container", "container/lib", "container/connectors/slack"):
    sys.path.insert(0, str(ROOT / part))

from connector_contract import Choice, Reply  # noqa: E402

OWNER, DM, LIVE = "U0000000001", "D0000000001", "alpha-live-grove-000001"
FOOTER = ("---\n↩️ reply to this message to talk to this session directly\n"
          "https://lodge.invalid/tui?session=beta-old-marsh-000002")


class FakeSlack:
    def __init__(self):
        self.posts, self.updates, self.ephemerals, self.uploads = [], [], [], []
        self.parents, self.files = {}, {}

    async def post(self, channel, text, blocks=None):
        self.posts.append((channel, text, blocks))
        return f"{len(self.posts)}.000"

    async def update(self, channel, ts, text, blocks=None):
        self.updates.append((channel, ts, text, blocks))

    async def ephemeral(self, channel, user, text):
        self.ephemerals.append((channel, user, text))

    async def parent(self, channel, ts):
        return self.parents.get(ts)

    async def download(self, url):
        return self.files[url]

    async def upload(self, channel, path, filename, comment):
        self.uploads.append((channel, Path(path).name, filename, comment))


@pytest.fixture
def slack(tmp_path, monkeypatch):
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(tmp_path))
    monkeypatch.setenv("SLACK_OWNER_USER", OWNER)
    import chat_lodge
    import connector_registry
    import lodge_commands
    sc = importlib.import_module("slack_connector")
    for name in ("alpha", "beta"):
        (tmp_path / name / "wolt").mkdir(parents=True)
        (tmp_path / name / "wolt" / "wolt.json").write_text('{"type": "beaver"}')
    pasted: list = []
    for name, value in [
        ("message_session", lambda name, text: pasted.append((name, text)) or {"ok": True, "url": ""}),
        ("start_claude_session", lambda **kw: pasted.append((kw["wolt"], kw["prompt"])) or {"name": "beta-new-pond-000003"}),
        ("get_tunnel_url", lambda: ""), ("dog_available", lambda: False), ("_bot_log", lambda *a: None),
        ("transcribe_audio", lambda path: f"heard {Path(path).name}"),
        ("list_wolts", lambda: [{"name": "alpha", "type": "beaver"}, {"name": "beta", "type": "beaver"}]),
    ]:
        monkeypatch.setattr(chat_lodge, name, value)
    side = chat_lodge.ChatLodge(connector_registry.spec("slack"), [OWNER])
    side._save_chat_state(DM, {"active_wolt": "alpha", "active_session": LIVE})
    api = FakeSlack()
    uploads = tmp_path / ".space" / "slack" / "uploads"
    for name, value in [("API", api), ("CLIENT", Loopback(side, sc)), ("BOT", {"user_id": "UBOT", "bot_id": "BBOT"}),
                        ("COMMANDS", tuple(n for n in lodge_commands.COMMAND_NAMES if n != "start")),
                        ("PENDING", type(sc.PENDING)()), ("_SEEN", type(sc._SEEN)()), ("UPLOADS_DIR", uploads)]:
        monkeypatch.setattr(sc, name, value)
    world: Any = sc
    world.api, world.side, world.pasted, world.events = api, side, pasted, sc.CLIENT.events
    return world


def dm(text="hello", *, user=OWNER, channel=DM, event_id="Ev0001", **extra):
    event = {"type": "message", "channel": channel, "channel_type": "im", "user": user,
             "text": text, "ts": "100.1", **extra}
    return event, {"event_id": event_id}


def run(coro):
    return asyncio.run(coro)


def test_an_owner_dm_goes_to_the_chat_s_session_and_the_answer_back_to_the_dm(slack):
    run(slack.on_message(*dm("hello alpha")))
    [(session, text)] = slack.pasted
    assert session == LIVE and "[slack message from human, chat_id=D0000000001]: hello alpha" in text
    assert "notify --slack D0000000001 <<'WOLTSPACE_NOTIFY_" in text  # flat: no thread
    assert slack.api.posts[-1][0] == DM and "sent to" in slack.api.posts[-1][1]


@pytest.mark.parametrize("case", ["another user", "a channel", "a bot", "an edit", "no owner", "a resend"])
def test_anything_but_the_owner_s_own_new_dm_is_dropped(slack, monkeypatch, case):
    if case == "a resend":
        run(slack.on_message(*dm()))
    event, body = dm(
        user="U0000000002" if case == "another user" else OWNER,
        channel="C0000000001" if case == "a channel" else DM,
        **({"bot_id": "BOTHER"} if case == "a bot" else {}),
        **({"subtype": "message_changed"} if case == "an edit" else {}))
    if case == "no owner":
        monkeypatch.setenv("SLACK_OWNER_USER", "")
    before = len(slack.events)
    run(slack.on_message(event, body))
    assert len(slack.events) == before


def test_a_thread_reply_under_this_bot_s_message_goes_to_that_message_s_session(slack):
    slack.api.parents["50.5"] = {"bot_id": "BBOT", "text": f"🦫 beta: done?\n\n{FOOTER}"}
    slack.api.parents["60.6"] = {"user": OWNER, "text": f"pasted\n\n{FOOTER}"}
    run(slack.on_message(*dm("yes", thread_ts="50.5")))
    run(slack.on_message(*dm("and this", thread_ts="60.6", event_id="Ev0002")))
    assert [name for name, _ in slack.pasted] == ["beta-old-marsh-000002", "beta-old-marsh-000002"]
    # the second hangs under the owner's own paste: the chat's wolt (now beta) takes it like any message
    assert "[replying to: 🦫 beta: done?\n]\nyes" in slack.pasted[0][1]
    assert "[replying to" not in slack.pasted[1][1]


def test_commands_typed_with_a_bang_work_anywhere_and_a_double_bang_is_text(slack):
    run(slack.on_message(*dm("!wolt beta", thread_ts="70.7")))
    assert slack.side._load_chat_state(DM)["active_wolt"] == "beta"
    run(slack.on_message(*dm("!!wolt alpha", event_id="Ev0002")))
    assert [(e["kind"], e["body"].get("name") or e["body"].get("text")) for e in slack.events] == [
        ("command", "wolt"), ("message", "!!wolt alpha")]


def test_a_slash_command_and_a_click_are_acknowledged_before_the_lodge_hears_them(slack):
    order = []

    async def ack():
        order.append(("ack", len(slack.events)))

    run(slack.on_command(ack, {"command": "/wolt", "text": "", "user_id": OWNER, "channel_id": DM,
                               "trigger_id": "t1"}))
    picker = slack.api.posts[-1]
    buttons = [e for block in picker[2] if block["type"] == "actions" for e in block["elements"]]
    assert [b["value"] for b in buttons] == ["wolt:beta"]
    run(slack.on_action(ack, {"user": {"id": OWNER}, "channel": {"id": DM}, "trigger_id": "t2",
                              "message": {"ts": "9.9"}, "actions": [{"value": "wolt:beta", "action_ts": "1"}]}))
    assert order == [("ack", 0), ("ack", 1)]
    assert slack.side._load_chat_state(DM)["active_wolt"] == "beta"
    assert slack.api.updates[-1][:2] == (DM, "9.9")  # the picker message, edited in place
    assert slack.api.ephemerals == [(DM, OWNER, "switched to beta")]


def test_a_command_or_click_from_anyone_else_is_dropped(slack):
    async def ack():
        pass
    run(slack.on_command(ack, {"command": "/wolt", "text": "beta", "user_id": "U0000000002", "channel_id": DM}))
    run(slack.on_action(ack, {"user": {"id": OWNER}, "channel": {"id": "C0000000001"},
                              "actions": [{"value": "wolt:beta"}]}))
    assert slack.events == [] and slack.side._load_chat_state(DM)["active_wolt"] == "alpha"


def test_a_shared_file_is_downloaded_into_the_connector_s_folder_and_passed_on(slack):
    slack.api.files = {"https://files.invalid/a": b"%PDF-", "https://files.invalid/v": b"OggS"}
    files = [{"name": "report.pdf", "mimetype": "application/pdf", "size": 5, "url_private": "https://files.invalid/a"},
             {"name": "note.m4a", "mimetype": "audio/mp4", "size": 4, "url_private": "https://files.invalid/v"},
             {"name": "huge.bin", "size": 60 * 1024 * 1024, "url_private": "https://files.invalid/h"}]
    run(slack.on_message(*dm("see this", subtype="file_share", files=files)))
    first, second = (text for _, text in slack.pasted)
    assert "[file received] report.pdf (application/pdf, 5 bytes) saved at" in first and "Caption: see this" in first
    assert str(slack.UPLOADS_DIR) in first
    assert "[voice message] heard" in second and "note.m4a" in second
    assert slack.api.posts[-1] == (DM, "I couldn't get huge.bin from Slack. Please send it again.", None)


def test_replies_render_as_block_kit():
    sc = importlib.import_module("slack_connector")
    text, blocks = sc.render(Reply("**pick** one", ((Choice("🦫 beta", action="wolt:beta"),
                                                      Choice("open", url="https://lodge.invalid")),),
                                   markdown=True))
    assert text == "*pick* one"
    assert blocks[0] == {"type": "section", "text": {"type": "mrkdwn", "text": "*pick* one"}}
    assert [{k: v for k, v in b.items() if k != "text"} for b in blocks[1]["elements"]] == [
        {"type": "button", "action_id": "c0-0", "value": "wolt:beta"},
        {"type": "button", "action_id": "c0-1", "url": "https://lodge.invalid"}]
    assert sc.render(Reply("plain")) == ("plain", None)


def test_a_notification_is_sent_and_a_failure_is_classified(slack, tmp_path, monkeypatch):
    from slack_sdk.errors import SlackApiError

    box = tmp_path / ".space" / "outbox"
    box.mkdir(parents=True)
    (box / "e1").write_bytes(b"hi")
    monkeypatch.setenv("WOLTSPACE_CONNECTOR_OUTBOX", str(box))
    frame = {"route": {"chat_id": DM}}
    assert run(slack.on_send({**frame, "method": "message", "text": "done"}))["status"] == "delivered"
    sent = run(slack.on_send({**frame, "method": "document", "path": str(box / "e1"), "filename": "a.txt",
                              "text": "here"}))
    assert sent["status"] == "delivered" and slack.api.uploads == [(DM, "e1", "a.txt", "here")]
    outside = tmp_path / "secret.txt"
    outside.write_text("x")
    assert run(slack.on_send({**frame, "method": "document", "path": str(outside), "filename": "s"}))[
        "status"] == "refused"

    async def refuse(*a):
        raise SlackApiError("no", {"ok": False, "error": "missing_scope", "needed": "files:write"})

    async def hang(*a):
        raise TimeoutError
    for broken, status in ((refuse, {"status": "refused", "error": "missing_scope (files:write)"}),
                           (hang, {"status": "ambiguous", "error": "timed out"})):
        monkeypatch.setattr(slack.api, "post", broken)
        assert run(slack.on_send({**frame, "method": "message", "text": "x"})) == status


@pytest.mark.parametrize("url", ["https://files.invalid/F1/a.pdf", "http://files.slack.com/F1/a.pdf",
                                 "https://files.slack.com.invalid/F1/a.pdf"])
def test_the_bot_token_goes_only_to_slack_s_own_file_host(url):
    sc = importlib.import_module("slack_connector")
    with pytest.raises(ValueError, match="not a Slack file link"):
        run(sc.SlackAPI(None, "xoxb-test").download(url))  # refused before any request


def test_the_setup_manifest_registers_every_lodge_command_under_a_name_slack_allows():
    """Slack refuses a manifest that registers one of its built-ins; /apps is one, so it is /wapps.

    /start is Telegram's own greeting, not a Slack command."""
    import json

    import lodge_commands
    sc = importlib.import_module("slack_connector")
    manifest = json.loads((ROOT / "container/skills/setup-slack/references/manifest.json").read_text())
    registered = {c["command"].lstrip("/") for c in manifest["features"]["slash_commands"]}
    assert "apps" not in registered
    assert registered == {sc.SLASH_NAMES.get(n, n) for n in lodge_commands.COMMAND_NAMES if n != "start"}


def test_slash_wapps_runs_the_lodge_s_apps_command(slack):
    async def ack():
        pass
    run(slack.on_command(ack, {"command": "/wapps", "text": "", "user_id": OWNER, "channel_id": DM,
                               "trigger_id": "t3"}))
    assert [(e["kind"], e["body"]["name"]) for e in slack.events] == [("command", "apps")]


@pytest.mark.parametrize("typed, read", [("a &lt; b &amp; c", "a < b & c"), ("<https://x.invalid|x>", "https://x.invalid"),
                                         ("<@U0000000002> hi", "@U0000000002 hi")])
def test_the_owner_s_text_reaches_the_wolt_as_typed_not_as_slack_stores_it(slack, typed, read):
    run(slack.on_message(*dm(typed)))
    assert slack.pasted[-1][1].split("\n")[0].endswith(f"]: {read}")


def test_a_dog_image_is_staged_in_the_outbox_and_only_an_outbox_file_is_sent(slack, tmp_path, monkeypatch):
    from connector_loopback import _Out

    monkeypatch.setenv("WOLTSPACE_CONNECTOR_OUTBOX", str(tmp_path / ".space" / "outbox"))
    image = tmp_path / "alpha" / "wolt" / "images" / "dog.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"PNG")
    aged = tmp_path / ".space" / "outbox" / "image-0000000000000000"
    aged.mkdir(parents=True)
    os.utime(aged, (1, 1))  # an image folder staged long ago
    out = _Out(slack, {"id": "e1", "route": {"chat_id": DM}})
    run(slack.side._send_result(out, {"type": "image", "text": "a dog", "path": str(image)}))
    assert not aged.exists()
    [(channel, staged, name, comment)] = slack.api.uploads
    assert (channel, staged, name, comment) == (DM, "dog.png", "dog.png", "a dog")  # its own name
    assert [p.read_bytes() for p in (tmp_path / ".space" / "outbox").glob("image-*/dog.png")] == [b"PNG"]
    with pytest.raises(PermissionError, match="not a staged file"):
        run(slack.on_say({"route": {"chat_id": DM}, "reply": {"text": "x"}, "photo": str(image)}))
    assert len(slack.api.uploads) == 1
