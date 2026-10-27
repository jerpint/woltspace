"""The Telegram connector and the lodge's ChatLodge, wired without a socket, for tests.

`Loopback` stands in for connector_client.Client: an event goes straight to
the ChatLodge and returns once it is dispatched, and every frame the lodge
answers with is encoded and checked as it would be on the wire, then handed
to the connector. Deterministic, so transcripts can be compared. The socket
itself is tested in test_connector_socket.py and test_connector_e2e.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for part in ("container", "container/lib", "container/connectors/telegram"):
    if str(ROOT / part) not in sys.path:
        sys.path.insert(0, str(ROOT / part))

import connector_protocol as wire  # noqa: E402


def _through_wire(frame: dict) -> dict:
    return wire.decode(wire.encode(frame), wire.FROM_LODGE)


class _Out:
    def __init__(self, connector, event: dict):
        self.connector, self.event = connector, event

    async def say(self, reply, photo=None):
        frame = {"type": "say", "event": self.event["id"], "route": dict(self.event["route"]),
                 "reply": wire.reply_to_wire(reply)}
        if photo:
            frame["photo"] = str(photo)
        await self.connector.on_say(_through_wire(frame))

    async def edit(self, reply):
        await self.connector.on_edit(_through_wire(
            {"type": "edit", "event": self.event["id"], "reply": wire.reply_to_wire(reply)}))

    async def ack(self, ack):
        await self.connector.on_ack(_through_wire(
            {"type": "ack", "event": self.event["id"], "ack": wire.ack_to_wire(ack)}))


class Loopback:
    def __init__(self, lodge, connector):
        self.lodge, self.connector = lodge, connector
        self.events: list[dict] = []

    async def event(self, kind, route, sender, body, event_id=None, timeout=None):
        frame = {"type": "event", "id": event_id or f"e{len(self.events)}", "kind": kind,
                 "route": route, "sender": sender, "body": body}
        frame = wire.decode(wire.encode(frame), wire.FROM_CONNECTOR)
        self.events.append(frame)
        await self.lodge.handle(frame, _Out(self.connector, frame))
        return "accepted"


def telegram(monkeypatch, wolts_dir: Path, owners=("1",)):
    """(connector module, ChatLodge) for Telegram, wired by a Loopback."""
    import chat_lodge
    import connector_registry
    import telegram_connector

    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(wolts_dir))
    lodge = chat_lodge.ChatLodge(connector_registry.spec("telegram"), owners,
                                 help_tips=connector_registry.spec("telegram").help)
    monkeypatch.setattr(telegram_connector, "CLIENT", Loopback(lodge, telegram_connector))
    monkeypatch.setattr(telegram_connector, "PENDING", type(telegram_connector.PENDING)())
    monkeypatch.setattr(telegram_connector, "UPLOADS_DIR", Path(wolts_dir) / ".space" / "telegram" / "uploads")
    return telegram_connector, lodge


class FakeBroker:
    """Stands in for the lodge's connector broker on the notify side: records each
    `send` frame (with the file's bytes read at send time) and answers with `answer`."""

    def __init__(self, answer=None, connected=True):
        self.frames: list[dict] = []
        self.answer = answer or (lambda frame: {"status": "delivered", "message_id": "7"})
        self._connected = connected

    def connected(self, name):
        return self._connected

    async def send(self, name, route, method, text="", *, path=None, filename=None,
                   content_type=None, timeout=None):
        import connector_broker

        if not self._connected:
            raise connector_broker.ConnectorUnavailable(f"{name} is not connected to the lodge")
        frame = {"connector": name, "route": dict(route), "method": method, "text": text}
        if method != "message":
            frame.update(path=str(path), filename=filename, content_type=content_type,
                         bytes=Path(path).read_bytes())
        self.frames.append(frame)
        result = self.answer(frame)
        if isinstance(result, BaseException):
            raise result
        if result["status"] != "delivered":
            raise connector_broker.SendFailed(result["status"], result.get("error") or result["status"])
        return result


def install_fake_broker(monkeypatch, **kwargs) -> FakeBroker:
    import connector_broker

    fake = FakeBroker(**kwargs)
    monkeypatch.setattr(connector_broker, "_BROKER", fake)
    return fake


class Pair:
    """Both halves read as one, for tests written against the old in-process adapter:
    a name is looked up on the connector module first, then on the lodge side.
    Patch on the half that owns the name (monkeypatch.setattr(pair.side, ...))."""

    def __init__(self, connector, side):
        self.connector, self.side = connector, side

    def __getattr__(self, name):
        if hasattr(self.connector, name):
            return getattr(self.connector, name)
        return getattr(self.side, name)


_OLD_COMMAND_HANDLERS = {"handle_setwolt": "wolt"}


class _Everyone:
    def __contains__(self, item):
        return True


class TelegramFacade:
    """The connector, its lodge side and the lodge's services under the old
    adapter's names, for tests written against `bot.telegram_adapter`.

    Reading a name finds it on the connector, then the lodge side, then the
    lodge's service module (chat_lodge); setting it (monkeypatch.setattr)
    changes it where it lives. The connector's allowlist stays the gate: the
    lodge side here takes any sender, so a test's own is_allowed decides.
    """

    def __init__(self, monkeypatch, wolts_dir: Path):
        import chat_lodge

        connector, side = telegram(monkeypatch, wolts_dir)
        monkeypatch.setattr(side, "owners", _Everyone())
        object.__setattr__(self, "_monkeypatch", monkeypatch)
        object.__setattr__(self, "connector", connector)
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "services", chat_lodge)

    def _render(self, reply):
        text, kwargs = self.connector._render(reply)
        return text, kwargs.get("reply_markup")

    def __getattr__(self, name):
        import lodge_commands as commands

        if name.startswith("handle_") and name != "handle_callback" and not hasattr(self.connector, name):
            return self.connector.command_handler(_OLD_COMMAND_HANDLERS.get(name, name[len("handle_"):]))
        ports = lambda: self.side.ports({"chat_id": "1"})  # noqa: E731
        builders = {
            "_holds_message": lambda sessions, url: self._render(commands.holds_list(ports(), sessions, url)),
            "_sessions_picker_message": lambda sessions, url: self._render(
                commands.sessions_picker(ports(), sessions, url)),
            "_apps_picker_message": lambda apps: self._render(commands.apps_list(apps)),
            "_wolt_picker_keyboard": lambda wolts, active: self.connector._keyboard(
                commands.wolt_picker_choices(ports(), wolts, active)),
            "_wolt_picker_header": lambda wolts, active: commands.wolt_picker(ports(), wolts, active).text,
        }
        if name in builders:
            return builders[name]
        for owner in (self.connector, self.side, self.services):
            if hasattr(owner, name):
                return getattr(owner, name)
        raise AttributeError(name)

    def __setattr__(self, name, value):
        # Patched where it lives, through the test's own monkeypatch, so its undo
        # restores the owner's value whatever pytest recorded for the facade.
        for owner in (self.connector, self.side, self.services):
            if hasattr(owner, name):
                self._monkeypatch.setattr(owner, name, value)
                return
        raise AttributeError(name)

    def __delattr__(self, name):
        # monkeypatch's undo for a name the facade only forwards: the owner's
        # own entry, recorded in __setattr__, puts the value back.
        pass
