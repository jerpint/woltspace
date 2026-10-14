"""Telegram on the connector protocol.

One process, two loops: `poll_once` turns Bot API updates into lodge events
and advances the cursor core keeps; `deliver_once` leases deliveries and
sends them. The connector decides nothing about who may talk to whom or
where a message goes; it carries bytes between the two APIs.

Ownership: the process holds the channel's run lock (the lodge names it
in `GET state`) from before its first provider call until it exits, so a
switch (which takes that lock too) can only happen while no call of this
process is in flight; neither loop runs without it. On top of that, a
fence: before every provider call (each getUpdates, each text part, each
file send and fallback, each confirmation) the connector asks the lodge
whether it still owns the channel and is enabled. If not, the call is not
made: nothing more is polled or confirmed, a send stops with the parts
that went out nacked as retryable, and a lease it holds expires on its own.

Inbound, per update, in the order fetched: text or caption becomes the
event text; a photo (its largest size), document, voice note, audio or
video is downloaded through `getFile` (streamed, capped) and uploaded to
the lodge first, then named in the event. A transient failure anywhere in
that (Telegram 429/5xx, network, a lodge 429/503) stops the batch at that
update: nothing after it is answered, the cursor stays, the next poll
fetches it again. Only a permanent failure (too big, 400/404) makes the
event text-only, with a note. An update the connector cannot carry (a
service message, nothing to say) is answered as an ignore so the cursor
can pass it. Slash commands are passed through as text in this slice: the
chat command set is core-mediated and comes with slice 2b. A reply to one
of the bot's messages carries that message's id, and core resolves it to
the delivery and session inside the exact binding and conversation.

The cursor: core hands it over at start (`GET state`); after each update
the lodge has answered with a final answer (accepted, dropped, ignored, or
409 conflict) the connector posts `update_id + 1`, in fetch order, and
stops at the first answer that is not final. Core refuses a cursor past
what it answered. Telegram itself confirms updates only when asked with a
higher offset, so as soon as a batch moved the cursor the connector asks
once more with the new offset (short, one item, result discarded): what
core holds durably is then confirmed at the provider too, and a runtime
that takes over later is not handed it again. No cursor at all (first
start) means asking Telegram for its unconfirmed backlog.

Outbound: text goes as `sendMessage` in parts of at most 4096 characters,
the reply footer on the last one; a `voice` attachment as `sendVoice`, an
.mp3/.m4a as `sendAudio`, anything else (and anything Telegram refuses as
voice or audio) as `sendDocument`. The ack lists every part's message id,
on a failed attempt too, so a reply to a part that did go out still routes.
Telegram 429 and 5xx, and network trouble, are nacked as retryable; 400
and 403 are terminal.

The bot token lives in `TELEGRAM_BOT_TOKEN`; it is redacted from every
error the connector reports or logs, and the runner silences the HTTP
client's own request logging (see `runner.py`). httpx only; the two
clients take any transport, which is how tests run it against a fake Bot
API and the real lodge app.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

PREFIX = "/connector/v1"
TELEGRAM_MAX_TEXT = 4096
VOICE_SUFFIXES = (".ogg", ".oga", ".opus")
AUDIO_SUFFIXES = (".mp3", ".m4a")
POLL_TIMEOUT = 25
LEASE_WAIT = 20
DOWNLOAD_CAP = 50 * 1024 * 1024
MAX_EVENT_TEXT = 16 * 1024

logger = logging.getLogger(__name__)


class LodgeRefused(RuntimeError):
    """The lodge answered 401 or 403: this process must stop."""


class NotOwner(RuntimeError):
    """The run lock is not held by this process: no provider call is allowed."""


class StartRefused(RuntimeError):
    """The lodge did not admit the connector at start (no state, or no run lock named)."""


class OwnershipLost(RuntimeError):
    """The fence tripped between two provider calls of one operation."""


class Transient(RuntimeError):
    """A failure to try again later; the update stays unanswered."""


class Permanent(RuntimeError):
    """A failure that will not change; the update goes on without its file."""


@dataclass
class Report:
    """What one turn of a loop did, for tests and the log."""

    events: list[dict] = field(default_factory=list)
    acks: list[dict] = field(default_factory=list)
    cursor: int | None = None
    paused: bool = False
    fenced: bool = False
    stopped_at: int | None = None


def split_text(text: str, limit: int = TELEGRAM_MAX_TEXT) -> list[str]:
    """Telegram-sized parts, cut at line ends when there is one in reach."""
    if not text:
        return [""]
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    parts.append(text)
    return parts


def redact(text: object, token: str) -> str:
    """`text` with the bot token replaced, wherever it appears."""
    value = str(text)
    return value.replace(token, "<token>") if token else value


class TelegramConnector:
    def __init__(
        self, lodge: httpx.AsyncClient, bot: httpx.AsyncClient, *, bot_token: str, connector_token: str,
        activation: dict | None = None,
    ):
        self.lodge = lodge
        self.bot = bot
        self._bot_token = bot_token
        self._connector_token = connector_token
        # {lodge_id, instance_id, generation} the supervisor told this process
        # to expect: a token that came by handoff is pending until the lodge
        # confirms exactly these at `POST activate`.
        self._activation = activation
        self.cursor: int | None = None
        self.started = False
        self.run_lock: str | None = None
        self._run_lock_handle = None
        # Once stopped, nothing starts again on its own: only an explicit start().
        self._stopped = False

    # ----------------------------------------------------------------- lodge

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._connector_token}"}

    async def _lodge(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = {**self._headers, **kwargs.pop("headers", {})}
        response = await self.lodge.request(method, f"{PREFIX}{path}", headers=headers, **kwargs)
        if response.status_code in (401, 403):
            raise LodgeRefused(f"lodge answered {response.status_code}")
        return response

    async def start(self) -> dict:
        """Take the cursor core kept and the channel's run lock; nothing is polled before this.

        The lock is an exclusive flock on the file the lodge names, held by
        this process until it exits (never released earlier, so an in-flight
        provider call always ends before the lock does). Already held means
        another connector runs for this channel: refuse to start.
        """
        if self._activation is not None:
            activated = await self._lodge("POST", "/activate", json=self._activation)
            if activated.status_code != 200:
                raise StartRefused(f"the lodge did not activate this generation ({activated.status_code})")
            self._activation = None
        response = await self._lodge("GET", "/state")
        if response.status_code != 200:
            raise StartRefused(f"the lodge answered {response.status_code} to the state request")
        try:
            state = response.json()
        except ValueError:
            raise StartRefused("the lodge's state answer is not JSON") from None
        run_lock = state.get("run_lock") if isinstance(state, dict) else None
        if not isinstance(run_lock, str) or not run_lock:
            raise StartRefused("the lodge named no run lock; not starting")
        if self._run_lock_handle is None:
            import fcntl
            from pathlib import Path

            try:
                Path(run_lock).parent.mkdir(parents=True, exist_ok=True)
                handle = open(run_lock, "a+")
            except OSError as exc:
                raise StartRefused(f"cannot open the run lock {run_lock}: {exc}") from None
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                raise NotOwner(f"another connector already runs for this channel ({run_lock})") from None
            except OSError as exc:
                handle.close()
                raise StartRefused(f"cannot take the run lock {run_lock}: {exc}") from None
            self._run_lock_handle = handle
        self.run_lock = run_lock
        self.cursor = state.get("cursor")
        self.started = True
        self._stopped = False
        return state

    async def _ensure_started(self) -> None:
        if self._stopped:
            raise NotOwner("stopped; start() again before any call")
        if not self.started:
            await self.start()

    def holds_run_lock(self) -> bool:
        """Only an actually held handle counts; no path, no handle, no exemption."""
        return self._run_lock_handle is not None

    def stop(self) -> None:
        """Give the run lock back. Only once both loops and every call of theirs have ended:
        the runner calls it last, after both tasks were cancelled and awaited; tests call
        it to run two connectors. After it, no provider call is allowed until `start()` again."""
        if self._run_lock_handle is not None:
            import fcntl

            fcntl.flock(self._run_lock_handle.fileno(), fcntl.LOCK_UN)
            self._run_lock_handle.close()
            self._run_lock_handle = None
        self.started = False
        self._stopped = True

    def _require_lock(self) -> None:
        if not self.holds_run_lock():
            raise NotOwner("the run lock is not held")

    async def owns_channel(self) -> bool:
        """The fence: the lodge says this instance is enabled and the connector owns the channel."""
        try:
            state = (await self._lodge("GET", "/state")).json()
        except (httpx.HTTPError, ValueError):
            return False
        if state.get("cursor") is not None:
            self.cursor = state["cursor"]
        return bool(state.get("runtime_active")) and not state.get("paused")

    # --------------------------------------------------------------- telegram

    async def _call(self, method: str, **params) -> httpx.Response:
        """One Bot API call, fenced: the lock must be held and the lodge must still say we own the channel."""
        self._require_lock()
        if not await self.owns_channel():
            raise OwnershipLost(f"not sending {method}: the channel is no longer ours")
        return await self.bot.post(f"/bot{self._bot_token}/{method}", json=params)

    async def _upload_call(self, method: str, field_name: str, data: bytes, name: str, params: dict) -> httpx.Response:
        self._require_lock()
        if not await self.owns_channel():
            raise OwnershipLost(f"not sending {method}: the channel is no longer ours")
        return await self.bot.post(
            f"/bot{self._bot_token}/{method}", data=params, files={field_name: (name, data)},
        )

    def _error(self, body: dict) -> tuple[str, bool]:
        """(redacted description, retryable) from a not-ok Bot API answer."""
        code = body.get("error_code", 500)
        description = redact(body.get("description", "telegram error"), self._bot_token)[:500]
        return description, code == 429 or code >= 500

    async def _download(self, file_id: str) -> bytes:
        """The file's bytes, streamed under the cap; Transient or Permanent otherwise."""
        try:
            info = await self._call("getFile", file_id=file_id)
            body = info.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise Transient(redact(exc, self._bot_token)) from None
        if not body.get("ok"):
            description, retryable = self._error(body)
            raise (Transient if retryable else Permanent)(description)
        path = (body.get("result") or {}).get("file_path")
        if not path:
            raise Permanent("telegram gave no file path")
        received = bytearray()
        if not await self.owns_channel():
            raise OwnershipLost("not downloading: the channel is no longer ours")
        try:
            async with self.bot.stream("GET", f"/file/bot{self._bot_token}/{path}") as response:
                if response.status_code == 429 or response.status_code >= 500:
                    raise Transient(f"file download answered {response.status_code}")
                if response.status_code != 200:
                    raise Permanent(f"file download answered {response.status_code}")
                async for chunk in response.aiter_bytes():
                    received.extend(chunk)
                    if len(received) > DOWNLOAD_CAP:
                        # Enforced while reading: the rest is never pulled.
                        raise Permanent(f"file is over {DOWNLOAD_CAP} bytes")
        except httpx.HTTPError as exc:
            raise Transient(redact(exc, self._bot_token)) from None
        return bytes(received)

    # ---------------------------------------------------------------- inbound

    @staticmethod
    def _attachment_of(message: dict) -> tuple[str, str, str] | None:
        """(file_id, name, kind) of the one file a message carries, or None."""
        if message.get("photo"):
            largest = max(message["photo"], key=lambda size: size.get("file_size", 0))
            return largest["file_id"], f"photo_{largest['file_unique_id']}.jpg", "file"
        if message.get("voice"):
            return message["voice"]["file_id"], f"voice_{message['voice']['file_unique_id']}.ogg", "voice"
        if message.get("audio"):
            audio = message["audio"]
            return audio["file_id"], audio.get("file_name") or f"audio_{audio['file_unique_id']}.mp3", "file"
        if message.get("video"):
            video = message["video"]
            return video["file_id"], video.get("file_name") or f"video_{video['file_unique_id']}.mp4", "file"
        if message.get("document"):
            document = message["document"]
            return document["file_id"], document.get("file_name") or f"document_{document['file_unique_id']}", "file"
        return None

    async def _upload(self, data: bytes, name: str) -> str:
        """The lodge's upload id; Transient on 429/503, Permanent on anything else refused."""
        response = await self._lodge(
            "POST", "/uploads", params={"name": name}, content=data,
            headers={"Content-Type": "application/octet-stream"},
        )
        if response.status_code == 200:
            return response.json()["upload_id"]
        try:
            reason = response.json().get("reason", str(response.status_code))
        except ValueError:
            reason = str(response.status_code)
        if response.status_code in (429, 503):
            raise Transient(f"lodge refused the upload: {reason}")
        raise Permanent(f"lodge refused the upload: {reason}")

    async def _event_for(self, update: dict) -> dict:
        """The lodge payload for one update: an event, or an ignore disposition.

        Raises Transient when a file could not be fetched or uploaded for a
        reason that may clear; the caller then stops the batch here.
        """
        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict) or not message.get("from") or not message.get("chat"):
            return {"event_id": f"tg:{update['update_id']}", "cursor": update["update_id"], "ignore": "unsupported_update"}
        text = message.get("text") or message.get("caption") or ""
        attachments = []
        found = self._attachment_of(message)
        if found is not None:
            file_id, name, _kind = found
            try:
                data = await self._download(file_id)
                attachments.append({"upload_id": await self._upload(data, name)})
            except Permanent as exc:
                text = f"{text}\n[a file came with this message and could not be carried: {exc}]".strip()
        if not text and not attachments:
            return {"event_id": f"tg:{update['update_id']}", "cursor": update["update_id"], "ignore": "empty_message"}
        event = {
            "event_id": f"tg:{update['update_id']}",
            "sender": {
                "provider_id": str(message["from"]["id"]),
                "display": (message["from"].get("username") or message["from"].get("first_name") or "")[:256],
            },
            "conversation": {
                "provider_id": str(message["chat"]["id"]),
                "thread": str(message["message_thread_id"]) if message.get("message_thread_id") else None,
            },
            "text": text[:MAX_EVENT_TEXT],
            "attachments": attachments,
            "cursor": update["update_id"],
        }
        replied = message.get("reply_to_message")
        if isinstance(replied, dict) and replied.get("message_id") is not None:
            event["reply_to"] = {"provider_message_id": str(replied["message_id"])}
        return event

    async def poll_once(self, *, timeout: int = POLL_TIMEOUT) -> Report:
        """One getUpdates, fenced; updates are answered in order and the cursor follows.

        The batch stops at the first update without a final answer (a
        transient file failure, a lodge 429/503/5xx, a refused cursor):
        nothing after it is answered, so the posted cursor never passes an
        unanswered fetched update.
        """
        await self._ensure_started()
        report = Report(cursor=self.cursor)
        if not await self.owns_channel():
            report.fenced = True
            return report
        params = {"timeout": timeout, "allowed_updates": ["message", "edited_message"]}
        if self.cursor is not None:
            params["offset"] = self.cursor
        try:
            response = await self._call("getUpdates", **params)
            body = response.json()
        except OwnershipLost:
            report.fenced = True
            return report
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("getUpdates failed: %s", redact(exc, self._bot_token))
            return report
        if not body.get("ok"):
            logger.warning("getUpdates not ok: %s", redact(body.get("description", ""), self._bot_token)[:200])
            return report
        for update in sorted(body.get("result", []), key=lambda u: u["update_id"]):
            try:
                payload = await self._event_for(update)
            except OwnershipLost:
                report.fenced = True
                report.stopped_at = update["update_id"]
                break
            except Transient as exc:
                logger.warning("update %s not carried yet: %s", update["update_id"], exc)
                report.stopped_at = update["update_id"]
                break
            response = await self._lodge("POST", "/events", json=payload)
            try:
                answer = response.json()
            except ValueError:
                answer = {}
            report.events.append({"status": response.status_code, "body": answer})
            # 200 (accepted, dropped or ignored) and 409 (conflict) are final.
            # 429 and 5xx are not: stop here, the next poll re-fetches it.
            if response.status_code not in (200, 409):
                report.stopped_at = update["update_id"]
                break
            cursor = await self._lodge("POST", "/cursor", json={"cursor": update["update_id"] + 1})
            if cursor.status_code != 200:
                logger.warning("cursor refused: %s", cursor.json().get("reason"))
                report.stopped_at = update["update_id"]
                break
            self.cursor = cursor.json()["cursor"]
            report.cursor = self.cursor
        if report.cursor is not None and report.cursor != params.get("offset"):
            await self._confirm()
        return report

    async def _confirm(self) -> None:
        """Tell Telegram the cursor: everything below it is answered and held by core. Fenced like any call."""
        if self.cursor is None:
            return
        try:
            await self._call("getUpdates", offset=self.cursor, limit=1, timeout=0)
        except OwnershipLost:
            return
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("could not confirm the cursor: %s", redact(exc, self._bot_token))

    # --------------------------------------------------------------- outbound

    async def _send_delivery(self, delivery: dict) -> tuple[list[str], str | None, bool]:
        """Send one delivery; returns (message ids that went out, error, retryable)."""
        chat_id = delivery["conversation"]["provider_id"]
        extra = {}
        if delivery["conversation"].get("thread"):
            extra["message_thread_id"] = int(delivery["conversation"]["thread"])
        message_ids: list[str] = []
        footer = delivery.get("reply_footer") or ""
        text = delivery["text"] or ""
        parts = split_text(text + footer) if (text or footer) else []
        for part in parts:
            try:
                response = await self._call("sendMessage", chat_id=chat_id, text=part, **extra)
                body = response.json()
            except OwnershipLost as exc:
                return message_ids, str(exc), True
            except (httpx.HTTPError, ValueError) as exc:
                return message_ids, redact(exc, self._bot_token), True
            if not body.get("ok"):
                return message_ids, *self._error(body)
            message_ids.append(str(body["result"]["message_id"]))
        for attachment in delivery.get("attachments", []):
            fetched = await self._lodge(
                "GET", f"/files/{attachment['file_id']}", params={"attempt_id": delivery["attempt_id"]},
            )
            if fetched.status_code != 200:
                return message_ids, f"lodge did not hand over {attachment['name']}", True
            data = fetched.content
            name = attachment["name"]
            lowered = name.lower()
            if attachment.get("kind") == "voice" or lowered.endswith(VOICE_SUFFIXES):
                order = (("sendVoice", "voice"), ("sendDocument", "document"))
            elif lowered.endswith(AUDIO_SUFFIXES):
                order = (("sendAudio", "audio"), ("sendDocument", "document"))
            else:
                order = (("sendDocument", "document"),)
            for method, field_name in order:
                try:
                    response = await self._upload_call(method, field_name, data, name, {"chat_id": chat_id, **extra})
                    body = response.json()
                except OwnershipLost as exc:
                    return message_ids, str(exc), True
                except (httpx.HTTPError, ValueError) as exc:
                    return message_ids, redact(exc, self._bot_token), True
                if body.get("ok"):
                    message_ids.append(str(body["result"]["message_id"]))
                    break
                if method == order[-1][0]:
                    return message_ids, *self._error(body)
        return message_ids, None, False

    async def lease_batch(self, *, max_items: int = 10, wait: float = 0) -> dict:
        await self._ensure_started()
        self._require_lock()
        return (await self._lodge("POST", "/deliveries/lease", json={"max": max_items, "wait": wait})).json()

    async def send_leased(self, deliveries: list[dict], report: Report) -> Report:
        """Send what was leased, fencing before every delivery; acks carry every id that went out."""
        self._require_lock()
        for delivery in deliveries:
            if not await self.owns_channel():
                # Nothing more is sent or acked; the leases expire on their own.
                report.fenced = True
                return report
            message_ids, error, retryable = await self._send_delivery(delivery)
            if error and "no longer ours" in error:
                # The fence tripped mid-send: nack with what went out (best
                # effort; if the lodge is gone the lease expires), then stop.
                report.fenced = True
                try:
                    await self._lodge("POST", f"/deliveries/{delivery['delivery_id']}/ack", json={
                        "attempt_id": delivery["attempt_id"], "ok": False, "retry": True,
                        "error": error[:1000], "provider_message_ids": message_ids,
                    })
                except (httpx.HTTPError, LodgeRefused):
                    pass
                return report
            body = {"attempt_id": delivery["attempt_id"], "provider_message_ids": message_ids}
            if error is None:
                body["ok"] = True
            else:
                body.update({"ok": False, "error": redact(error, self._bot_token)[:1000], "retry": retryable})
            ack = await self._lodge("POST", f"/deliveries/{delivery['delivery_id']}/ack", json=body)
            try:
                answer = ack.json()
            except ValueError:
                answer = {}
            report.acks.append({"delivery_id": delivery["delivery_id"], "status": ack.status_code, "body": answer})
        return report

    async def deliver_once(self, *, max_items: int = 10, wait: float = 0) -> Report:
        """Lease, send, ack: one turn. Partial sends are acked as failures and may repeat."""
        leased = await self.lease_batch(max_items=max_items, wait=wait)
        report = Report(cursor=self.cursor, paused=bool(leased.get("paused")))
        return await self.send_leased(leased.get("deliveries", []), report)
