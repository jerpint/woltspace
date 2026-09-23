"""One-room E2EE Matrix adapter for the chat MVP.

This is intentionally not a general Matrix bridge.  One configured wolt owns
one Matrix device and listens to one encrypted room.  The browser UI and
Element are ordinary clients in that room; the homeserver never sees message
bodies.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from nio import (
    AsyncClient,
    AsyncClientConfig,
    InviteMemberEvent,
    MatrixRoom,
    RoomMessageText,
    RoomSendError,
    SyncError,
    exceptions,
)

from bot.core import _bot_log

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from env_compat import get_env  # noqa: E402
from woltspace.matrix_outbox import pending, read_entry  # noqa: E402
from notify_prompt import notify_reply_instruction  # noqa: E402
from sessions import (  # noqa: E402
    deliver_message,
    resolve_active_session,
    start_session,
)


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def required(name: str) -> str:
    value = get_env(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def allowed_users() -> set[str]:
    return {
        value.strip()
        for value in get_env("MATRIX_ALLOWED_USERS", "").split(",")
        if value.strip()
    }


def trusted_devices() -> set[tuple[str, str]]:
    result = set()
    for value in get_env("MATRIX_TRUSTED_DEVICES", "").split(","):
        if not value.strip() or "|" not in value:
            continue
        user_id, device_id = value.strip().split("|", 1)
        if user_id and device_id:
            result.add((user_id, device_id))
    return result


def prompt_for(room_id: str, sender: str, body: str) -> str:
    return (
        f"[Matrix message from human, user_id={sender}, room_id={room_id}]: {body}\n"
        f"{notify_reply_instruction('--matrix', room_id)}"
    )


class MatrixAdapter:
    def __init__(self) -> None:
        self.homeserver = required("MATRIX_HOMESERVER")
        self.user_id = required("MATRIX_USER_ID")
        self.device_id = required("MATRIX_DEVICE_ID")
        self.access_token = required("MATRIX_ACCESS_TOKEN")
        self.room_id = required("MATRIX_ROOM_ID")
        self.wolt = required("MATRIX_WOLT")
        self.allowed = allowed_users()
        if not self.allowed:
            raise RuntimeError("MATRIX_ALLOWED_USERS must name at least one exact Matrix user ID")
        self.trusted = trusted_devices()
        if not self.trusted:
            raise RuntimeError(
                "MATRIX_TRUSTED_DEVICES must pin at least one @user:server|DEVICE"
            )
        self.store = Path(required("MATRIX_STORE_PATH"))
        self.outbox = Path(required("MATRIX_OUTBOX"))
        self.ready = asyncio.Event()
        self.store.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.store, 0o700)
        config = AsyncClientConfig(store_sync_tokens=True, encryption_enabled=True)
        self.client = AsyncClient(
            self.homeserver,
            self.user_id,
            device_id=self.device_id,
            store_path=str(self.store),
            config=config,
        )
        self.client.restore_login(self.user_id, self.device_id, self.access_token)

    async def on_invite(self, room: MatrixRoom, event: InviteMemberEvent) -> None:
        if room.room_id != self.room_id or event.sender not in self.allowed:
            logger.warning("refusing Matrix invite room=%s sender=%s", room.room_id, event.sender)
            return
        response = await self.client.join(room.room_id)
        logger.info("joined configured Matrix room %s: %s", room.room_id, type(response).__name__)

    async def on_message(self, room: MatrixRoom, event: RoomMessageText) -> None:
        if room.room_id != self.room_id or event.sender == self.user_id:
            return
        if event.sender not in self.allowed:
            logger.warning("ignoring Matrix sender outside allowlist: %s", event.sender)
            return
        if not room.encrypted or not event.decrypted or not event.verified:
            logger.warning("ignoring message that was not verified and decrypted in encrypted room")
            return
        text = event.body.strip()
        if not text:
            return
        session = resolve_active_session(self.wolt)
        routed = prompt_for(room.room_id, event.sender, text)
        if session:
            result = deliver_message(session, routed)
            if result.get("status") == "delivered":
                _bot_log("matrix_message_delivered", {
                    "room_id": room.room_id,
                    "sender": event.sender,
                    "session": session,
                })
                return
        created = start_session(
            wolt=self.wolt,
            prompt=routed,
            routing={"adapter": "matrix", "room_id": room.room_id, "chat_id": room.room_id},
        )
        _bot_log("matrix_session_started", {
            "room_id": room.room_id,
            "sender": event.sender,
            "session": created.get("name"),
        })

    async def flush_outbox(self) -> None:
        while True:
            for path in pending(self.outbox):
                try:
                    entry = read_entry(path)
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    logger.error("holding invalid Matrix outbox entry %s: %s", path.name, exc)
                    continue
                if entry["room_id"] != self.room_id:
                    logger.error("holding outbox entry for unconfigured room: %s", entry["room_id"])
                    continue
                try:
                    response = await self.client.room_send(
                        room_id=self.room_id,
                        message_type="m.room.message",
                        content={"msgtype": "m.text", "body": entry["message"]},
                    )
                except exceptions.OlmUnverifiedDeviceError as exc:
                    logger.error(
                        "holding %s until every recipient device is explicitly trusted: %s",
                        path.name, exc,
                    )
                    break
                if isinstance(response, RoomSendError):
                    logger.error("Matrix send failed; retaining %s: %s", path.name, response)
                    break
                path.unlink(missing_ok=True)
                _bot_log("matrix_notify_sent", {
                    "room_id": self.room_id,
                    "session": entry.get("session", ""),
                    "event_id": getattr(response, "event_id", ""),
                })
            await asyncio.sleep(0.25)

    async def run(self) -> None:
        # Establish room/device state without replaying old timeline entries into
        # a fresh agent session. Callbacks are installed only after this sync.
        initial = await self.client.sync(
            timeout=0,
            full_state=True,
            sync_filter={"room": {"timeline": {"limit": 0}}},
        )
        if isinstance(initial, SyncError):
            raise RuntimeError(f"initial Matrix sync failed: {initial}")
        room = self.client.rooms.get(self.room_id)
        if room is not None and not room.encrypted:
            raise RuntimeError("configured Matrix room is not encrypted")
        await self.client.keys_query()
        missing = []
        for user_id, device_id in sorted(self.trusted):
            device = self.client.device_store[user_id].get(device_id)
            if device is None:
                missing.append(f"{user_id}|{device_id}")
            else:
                self.client.verify_device(device)
        if missing:
            raise RuntimeError(
                "configured trusted Matrix devices were not found: " + ", ".join(missing)
            )
        self.client.add_event_callback(self.on_invite, InviteMemberEvent)
        self.client.add_event_callback(self.on_message, RoomMessageText)
        self.ready.set()
        logger.info("Matrix E2EE adapter ready as %s in %s", self.user_id, self.room_id)
        try:
            await asyncio.gather(self.client.sync_forever(30000), self.flush_outbox())
        finally:
            await self.client.close()


def main() -> None:
    asyncio.run(MatrixAdapter().run())


if __name__ == "__main__":
    main()
