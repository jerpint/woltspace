"""The outbound boundary carries files without disturbing text-only senders."""

from pathlib import Path

from server.notification_types import Attachment, OutboundMessage, Plugin


async def _send(message, destination, context):
    return None


def test_text_message_has_no_attachments():
    assert OutboundMessage("hi").attachments == ()


def test_message_carries_attachments_as_a_tuple():
    attachment = Attachment(Path("/outbox/abc"), "weekly-update.html", 48211, "text/html")
    message = OutboundMessage("hi", "https://lodge.test/tui?session=s", (attachment,))
    assert message.attachments == (attachment,)
    assert message.attachments[0].filename == "weekly-update.html"


def test_a_sender_that_declares_nothing_cannot_carry_files():
    assert Plugin("quiet", _send).max_attachment_bytes == 0
