"""Slack and the hold together: a first message with no wolt chosen waits for the pick,
whether the owner taps the picker's button or types !wolt in a thread."""

from test_slack_connector import DM, OWNER, dm, run

pytest_plugins = ["test_slack_connector"]


def _held_picker(slack) -> dict:
    """The picker's buttons, by wolt, as the last Block Kit post shows them."""
    blocks = next(post[2] for post in reversed(slack.api.posts) if post[2])
    buttons = [b for block in blocks if block["type"] == "actions" for b in block["elements"]]
    return {b["value"].split(":")[1]: b["value"] for b in buttons}


def test_a_held_slack_message_goes_to_the_wolt_picked_by_button_or_by_bang_command(slack):
    async def ack():
        pass

    spawned = []
    slack.side._save_chat_state(DM, {})
    run(slack.on_message(*dm("first for a wolt")))
    picker = _held_picker(slack)
    assert set(picker) == {"alpha", "beta"} and all(v.startswith("hold:") for v in picker.values())
    assert "your message is held" in slack.api.posts[-1][1]
    assert slack.pasted == []

    run(slack.on_action(ack, {"user": {"id": OWNER}, "channel": {"id": DM}, "message": {"ts": "1.000"},
                              "actions": [{"value": picker["beta"], "action_ts": "1"}]}))
    spawned.append(slack.pasted[-1])
    assert spawned[0][0] == "beta" and spawned[0][1].endswith("]: first for a wolt")

    slack.side._save_chat_state(DM, {})
    run(slack.on_message(*dm("second for a wolt", event_id="Ev0002")))
    run(slack.on_message(*dm("!wolt alpha", thread_ts="70.7", event_id="Ev0003")))
    assert slack.pasted[-1][0] == "alpha" and slack.pasted[-1][1].endswith("]: second for a wolt")
    assert len(slack.pasted) == 2  # each held message went once, and nowhere else
