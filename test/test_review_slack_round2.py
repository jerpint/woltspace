"""Round 2 connector boundary cases, usable on the connector-only branch."""

import pytest
from connector_contract import Reply

from test.test_slack_connector import DM, dm, run

pytest_plugins = ["test.test_slack_connector"]


@pytest.mark.parametrize("encoded, expected", [
    ("a &lt; b", "a < b"),
    ("<https://example.invalid|example>", "https://example.invalid"),
    ("<@U0000000002>", "@U0000000002"),
])
def test_owner_text_is_normalized_before_forwarding(slack, encoded, expected):
    run(slack.on_message(*dm(encoded)))
    assert slack.events[0]["body"]["text"] == expected


def test_caption_is_normalized_before_attachment_forwarding(slack):
    slack.api.files = {"https://files.slack.com/example": b"dummy"}
    run(slack.on_message(*dm("a &lt; b", subtype="file_share", files=[{
        "name": "report.txt", "size": 5, "mimetype": "text/plain",
        "url_private": "https://files.slack.com/example",
    }])))
    assert slack.events[0]["body"]["caption"] == "a < b"


def test_photo_say_cannot_upload_a_file_outside_the_staged_outbox(slack, tmp_path, monkeypatch):
    import connector_protocol as wire

    box = tmp_path / "outbox"
    box.mkdir()
    monkeypatch.setenv("WOLTSPACE_CONNECTOR_OUTBOX", str(box))
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"dummy image")
    try:
        run(slack.on_say({"route": {"chat_id": DM}, "reply": wire.reply_to_wire(Reply("image")),
                          "photo": str(outside)}))
    except PermissionError:
        pass  # refusing to render the frame is acceptable; uploading is not
    assert slack.api.uploads == []


def test_typed_command_arguments_are_normalized_before_command_parsing(slack):
    run(slack.on_message(*dm("!help &lt;topic&gt;")))
    assert slack.events[0]["kind"] == "command"
    assert slack.events[0]["body"]["args"] == ["<topic>"]


def test_photo_say_accepts_a_file_inside_the_staged_outbox(slack, tmp_path, monkeypatch):
    import connector_protocol as wire

    box = tmp_path / "outbox"
    box.mkdir()
    monkeypatch.setenv("WOLTSPACE_CONNECTOR_OUTBOX", str(box))
    photo = box / "image.png"
    photo.write_bytes(b"dummy image")
    run(slack.on_say({"route": {"chat_id": DM}, "reply": wire.reply_to_wire(Reply("image")),
                      "photo": str(photo)}))
    assert len(slack.api.uploads) == 1
