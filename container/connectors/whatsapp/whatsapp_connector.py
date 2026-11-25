"""Text-only WhatsApp Agent Platform translation over the lodge socket.

The lodge handles commands, the dog, chat state and session routing. This
process holds the provider key, filters the creator, polls and renders text.
"""
from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import logging
import os
import time
from pathlib import Path

import connector_client
from connector_contract import parse_command, plain_text_reply
from connector_protocol import reply_from_wire
import provider

log = logging.getLogger("whatsapp_connector")
MAX_TEXT = 4096
PICKER_SECONDS = 300


class WhatsAppConnector:
    def __init__(self, token: str, data: Path, owners: list[str]):
        # The lodge also checks this explicit list. Never let provider-first
        # enrollment silently expand the lodge's owner permissions.
        if len(owners) != 1:
            raise provider.WhatsAppConfigurationError("WhatsApp requires exactly one configured creator owner id")
        provider.configure(token, data, owners[0])
        self.owner = owners[0]
        self.account = provider.ACCOUNT
        participant = base64.urlsafe_b64encode(self.owner.encode()).decode().rstrip("=")
        # The lodge persists chat_id on sessions. Keep both parts in that field.
        self.route = {"chat_id": f"{self.account}.{participant}"}
        self.commands = []
        self.choices = []
        self.choices_until = 0.0
        self.picker_event = None
        self.client = connector_client.Client(
            "whatsapp", on_say=self.on_say, on_edit=self.on_edit,
            on_ack=self.on_ack, on_send=self.on_send)

    def valid_route(self, route: dict) -> bool:
        # Account is part of the generic route, so key rotation cannot retarget
        # an old session's notification to the new creator/key.
        return route == self.route and provider._creator() == self.owner

    async def send_text(self, route: dict, text: str) -> dict:
        if not self.valid_route(route):
            return {"status": "refused", "error": "WhatsApp route is not bound to this creator and account"}
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            return {"status": "refused", "error": "WhatsApp supports nonempty text up to 4096 characters"}
        payload = {"messaging_product": "whatsapp", "to": self.owner,
                   "type": "text", "text": {"body": text}}
        try:
            status, body = await asyncio.to_thread(provider.api, "POST", "/messages", payload)
            if not 200 <= status < 300:
                return {"status": "ambiguous", "error": "WhatsApp delivery unconfirmed; do not retry blindly"}
            # Provider body is not logged or returned to the lodge.
            return {"status": "delivered"}
        except (provider.WhatsAppConfigurationError, provider.WhatsAppRateLimit):
            return {"status": "failed", "error": "WhatsApp rejected the send; check connector configuration or limits"}
        except provider.WhatsAppAPIError as exc:
            status = "failed" if exc.http_status and 400 <= exc.http_status < 500 else "ambiguous"
            return {"status": status, "error": "WhatsApp delivery unconfirmed; check before repeating"}
        except Exception:
            return {"status": "ambiguous", "error": "WhatsApp delivery unconfirmed; do not retry blindly"}

    def clear_picker(self) -> None:
        self.choices = []
        self.choices_until = 0.0
        self.picker_event = None

    async def on_send(self, frame: dict) -> dict:
        if frame.get("method") != "message" or frame.get("path"):
            return {"status": "refused", "error": "WhatsApp supports text only"}
        result = await self.send_text(frame["route"], frame.get("text", ""))
        if result["status"] == "delivered":
            # An unrelated notification supersedes a displayed picker, so a
            # later numeric reply cannot accidentally select an older action.
            self.clear_picker()
        return result

    async def on_say(self, frame: dict) -> None:
        if not self.valid_route(frame["route"]):
            return
        reply = reply_from_wire(frame["reply"])
        text = plain_text_reply(reply)
        choices = [c for row in reply.choices or () for c in row if c.action or c.url]
        event_id = frame.get("event")
        # The lodge may send a picker and then explanatory text for one event.
        # Only that follow-up keeps the current picker.
        if choices or not event_id or event_id != self.picker_event or frame.get("type") == "edit":
            self.clear_picker()
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT - 45] + "\n… (reply truncated; use a narrower command)"
            choices = []
        result = await self.send_text(frame["route"], text)
        if result["status"] != "delivered":
            log.warning("WhatsApp lodge reply: %s", result["status"])
        elif choices:
            self.choices = choices
            self.choices_until = time.monotonic() + PICKER_SECONDS
            self.picker_event = event_id

    async def on_edit(self, frame: dict) -> None:
        # edit/ack frames carry an event id, but no route. This process serves
        # exactly one creator; never infer a new destination from reply content.
        await self.on_say({**frame, "route": self.route})

    async def on_ack(self, frame: dict) -> None:
        text = (frame.get("ack") or {}).get("text")
        if text:
            await self.send_text(self.route, text)

    async def dispatch(self, item: dict) -> None:
        sender = item["from"]
        if sender != self.owner or provider._creator() != sender:
            return
        if item.get("type") != "text":
            await self.send_text(self.route, "I can handle text here; attachments are not supported yet.")
            return
        text = item["text"]["body"]
        # Opaque provider ids can contain characters or exceed the socket id
        # limit. Hash the complete id, namespaced by the account, deterministically.
        event_id = f"wa-{self.account}-" + hashlib.sha256(item["id"].encode()).hexdigest()
        kind, body = "message", {"text": text, "mention": text.startswith("@dog ") or text == "@dog"}
        command = parse_command(text, self.commands)
        if command:
            kind, body = "command", {"name": command.name, "args": list(command.args)}
            self.clear_picker()
        elif text.isascii() and text.isdecimal() and self.choices and time.monotonic() < self.choices_until:
            number = int(text) if len(text) < 4 else 0
            if not 1 <= number <= len(self.choices):
                await self.send_text(self.route, "Choose one of the displayed numbers, or send a command to leave the picker.")
                return
            choice = self.choices[number - 1]
            self.clear_picker()
            if choice.url:
                await self.send_text(self.route, choice.url)
                return
            kind, body = "selection", {"action": choice.action}
        else:
            self.clear_picker()
        status = await self.client.event(kind, self.route, sender, body, event_id=event_id)
        if status == "failed":
            log.warning("Lodge refused a checkpointed WhatsApp event")

    async def poll(self) -> None:
        state = provider.load(provider.CURSOR)
        activation = state.get("activation")
        if type(activation) is not int:
            activation = int(time.time())
            state = {"offset": 0, "activation": activation, "processed": []}
            provider.atomic_json(provider.CURSOR, state)
        failures = 0
        while True:
            batch = []
            try:
                # The blocking long poll and rate waits never block the socket.
                state = await asyncio.to_thread(provider.poll_once, state, activation, dispatch=batch.append)
                failures = 0
                for item in batch:
                    try:
                        await self.dispatch(item)
                    except Exception as exc:
                        # The batch is already checkpointed. A malformed frame
                        # must not discard unrelated later messages in it.
                        log.warning("WhatsApp item dispatch failed (%s)", type(exc).__name__)
            except provider.WhatsAppConfigurationError:
                raise
            except Exception as exc:
                failures += 1
                if failures >= 5:
                    raise RuntimeError("WhatsApp polling failed five consecutive times") from None
                log.warning("WhatsApp poll/dispatch failed (%s); attempt %d/5", type(exc).__name__, failures)
                await asyncio.sleep(min(60, 5 * 2 ** (failures - 1)))

    async def run(self) -> None:
        # run_forever handles a lodge that is not ready yet and reconnects.
        connection = asyncio.create_task(self.client.run_forever())
        poller = None
        try:
            while self.client.welcome is None:
                if connection.done():
                    await connection
                    raise RuntimeError("WhatsApp socket stopped before welcome")
                await asyncio.sleep(0.1)
            self.commands = [c[0] for c in self.client.welcome["commands"]]
            poller = asyncio.create_task(self.poll())
            done, pending = await asyncio.wait([connection, poller], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        finally:
            connection.cancel()
            if poller is not None:
                poller.cancel()
            await self.client.close()
            await asyncio.gather(connection, *([poller] if poller is not None else []), return_exceptions=True)


def main() -> None:
    token = os.environ.pop("WHATSAPP_AGENT_TOKEN", "")
    owners = [o.strip() for o in os.environ.get("WOLTSPACE_CONNECTOR_OWNERS", "").split(",") if o.strip()]
    data = Path(os.environ["WOLTSPACE_CONNECTOR_DATA"])
    app = WhatsAppConnector(token, data, owners)
    # Held until asyncio.run has joined any outstanding HTTP/rate worker.
    with (data / f"poll-{app.account}.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("WhatsApp key already has an active local poller") from None
        # Check under the lock that also protects first enrollment.
        try:
            provider._creator(expected=app.owner)
        except provider.WhatsAppConfigurationError as exc:
            raise SystemExit(str(exc)) from None
        try:
            asyncio.run(app.run())
        except KeyboardInterrupt:
            pass
        except Exception as exc:
            log.error("WhatsApp connector stopped (%s); check configuration and provider availability", type(exc).__name__)
            raise SystemExit(1) from None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
