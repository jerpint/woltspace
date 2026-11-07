"""Independent Slack boundary regressions: setup failure and retrieved mrkdwn."""

import pytest
from test_channel_connectors import write_config
from test_slack_connector import FOOTER, dm, run

from woltspace import channels

pytest_plugins = ["test_slack_connector", "test_channel_connectors"]


@pytest.mark.parametrize("owner", ["", "not-a-member-id"])
def test_slack_plan_refuses_an_unusable_owner_with_a_remedy(layout, owner):
    write_config(layout, {"channels": {"slack": {
        "enabled": True, "bot_token": "xoxb-test", "app_token": "xapp-test", "owner_user": owner,
    }}})
    plan = channels.FolderConnector(channels._registry().spec("slack")).plan(layout, {})
    assert not plan.enabled, "a connector that cannot receive any owner DM must not report enabled"
    assert "owner" in plan.detail.lower()
    assert "owner_user" in plan.remedy


@pytest.mark.parametrize("suffix", [">", "|Open session>"])
def test_thread_reply_normalizes_slack_retrieved_link_markup(slack, suffix):
    url = "https://lodge.invalid/tui?session=beta-old-marsh-000002"
    slack.api.parents["50.5"] = {"bot_id": "BBOT", "text": FOOTER.replace(url, "<" + url + suffix)}
    run(slack.on_message(*dm("yes", thread_ts="50.5")))
    assert [name for name, _ in slack.pasted] == ["beta-old-marsh-000002"]
