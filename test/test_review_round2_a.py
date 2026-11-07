"""Round 2 A standalone routing and staged-image lifetime checks."""

import os
from pathlib import Path

from test_slack_connector import DM, OWNER, dm, run

pytest_plugins = ["test_slack_connector"]


def test_connector_alone_picks_then_routes_only_the_resent_message(slack):
    slack.side._save_chat_state(DM, {})
    run(slack.on_message(*dm("original before pick")))
    assert slack.pasted == []
    assert any("send it again" in text for _, text, _ in slack.api.posts)
    run(slack.handle_action({"user": {"id": OWNER}, "channel": {"id": DM},
                            "actions": [{"value": "wolt:beta", "action_ts": "2"}]}))
    assert slack.pasted == []
    run(slack.on_message(*dm("resent after pick", event_id="Ev0002")))
    assert len(slack.pasted) == 1
    assert "resent after pick" in slack.pasted[0][1]
    assert "original before pick" not in slack.pasted[0][1]


def test_dog_image_path_sweeps_aged_staged_images_without_needing_notify(slack, tmp_path, monkeypatch):
    import outbox
    from paths import space_outbox_dir

    source = tmp_path / "alpha" / "wolt" / "images" / "generated.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"dummy image")
    box = space_outbox_dir(slack.side.wolts_dir)
    monkeypatch.setenv("WOLTSPACE_CONNECTOR_OUTBOX", str(box))
    old = box / outbox.stage(source, box)
    os.utime(old, (1, 1))

    class Out:
        async def say(self, reply, photo=None):
            assert photo and Path(photo).parent.parent == box  # in its own folder, under its own name
            assert Path(photo).read_bytes() == b"dummy image"
            await slack.on_say({"route": {"chat_id": DM},
                                "reply": {"text": reply.text}, "photo": photo})

    run(slack.side._send_result(Out(), {"type": "image", "path": str(source), "caption": "image"}))
    assert len(slack.api.uploads) == 1
    assert source.exists()  # canonical generated image is not disposable staging
    assert not old.exists(), "dog-only image use must not accumulate expired outbox copies forever"
