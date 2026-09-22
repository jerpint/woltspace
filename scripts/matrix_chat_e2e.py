"""Disposable two-device E2EE proof used by test-matrix-chat-e2e.sh."""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
from pathlib import Path

from nio import AsyncClient, AsyncClientConfig, RoomMessageText


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "container"), str(ROOT / "container" / "lib"), str(ROOT / "src")]

import bot.matrix_adapter as adapter_module  # noqa: E402
from bot.matrix_adapter import MatrixAdapter  # noqa: E402
from woltspace.matrix_outbox import enqueue  # noqa: E402


SERVER = os.environ["MATRIX_TEST_URL"]
STATE = Path(os.environ["MATRIX_TEST_STATE"])
PASSWORD = secrets.token_urlsafe(24)
MARKER_IN = "matrix-e2e-owner-to-wolt-4d8346"
MARKER_OUT = "matrix-e2e-wolt-to-owner-751ca9"
MARKER_RESTART = "matrix-e2e-after-restart-25df80"


async def register(name: str) -> tuple[AsyncClient, Path]:
    store = STATE / f"{name}-crypto"
    store.mkdir(parents=True, exist_ok=True)
    client = AsyncClient(
        SERVER,
        store_path=str(store),
        config=AsyncClientConfig(store_sync_tokens=True, encryption_enabled=True),
    )
    response = await client.register(name, PASSWORD, device_name=f"{name}-proof")
    assert response.__class__.__name__ == "RegisterResponse", response
    await client.keys_upload()
    return client, store


async def wait_until(predicate, *, timeout: float = 15.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("Matrix proof condition timed out")
        await asyncio.sleep(0.05)


async def start_adapter(env: dict[str, str]) -> tuple[MatrixAdapter, asyncio.Task]:
    os.environ.update(env)
    adapter = MatrixAdapter()
    task = asyncio.create_task(adapter.run())
    await asyncio.wait_for(adapter.ready.wait(), timeout=15)
    return adapter, task


async def stop_adapter(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def main() -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    suffix = secrets.token_hex(4)
    owner, _ = await register(f"owner{suffix}")
    wolt, wolt_store = await register(f"wolt{suffix}")
    owner_sync = None
    adapter_task = None
    try:
        created = await owner.room_create(
            name="Woltspace Matrix acceptance proof",
            invite=[wolt.user_id],
            is_direct=True,
            initial_state=[{
                "type": "m.room.encryption",
                "state_key": "",
                "content": {"algorithm": "m.megolm.v1.aes-sha2"},
            }],
        )
        assert created.__class__.__name__ == "RoomCreateResponse", created
        room_id = created.room_id
        joined = await wolt.join(room_id)
        assert joined.__class__.__name__ == "JoinResponse", joined
        await owner.sync(timeout=0, full_state=True)
        await wolt.sync(timeout=0, full_state=True)
        await owner.keys_query()
        await wolt.keys_query()
        owner.verify_device(owner.device_store[wolt.user_id][wolt.device_id])
        token, device, user = wolt.access_token, wolt.device_id, wolt.user_id
        await wolt.close()

        delivered = []
        inbound = asyncio.Event()

        def fake_deliver(session, text):
            delivered.append((session, text))
            inbound.set()
            return {"status": "delivered"}

        adapter_module.resolve_active_session = lambda _wolt: "proof-session"
        adapter_module.deliver_message = fake_deliver
        adapter_module._bot_log = lambda *_args, **_kwargs: None

        outbox = STATE / "outbox"
        env = {
            "MATRIX_HOMESERVER": SERVER,
            "MATRIX_USER_ID": user,
            "MATRIX_DEVICE_ID": device,
            "MATRIX_ACCESS_TOKEN": token,
            "MATRIX_ROOM_ID": room_id,
            "MATRIX_WOLT": "proof-wolt",
            "MATRIX_ALLOWED_USERS": owner.user_id,
            "MATRIX_TRUSTED_DEVICES": f"{owner.user_id}|{owner.device_id}",
            "MATRIX_STORE_PATH": str(wolt_store),
            "MATRIX_OUTBOX": str(outbox),
        }
        adapter, adapter_task = await start_adapter(env)

        outbound_bodies = []
        outbound = asyncio.Event()

        async def capture(_room, event):
            if event.decrypted and event.sender == user:
                outbound_bodies.append(event.body)
                outbound.set()

        owner.add_event_callback(capture, RoomMessageText)
        owner_sync = asyncio.create_task(owner.sync_forever(500))

        sent = await owner.room_send(
            room_id,
            "m.room.message",
            {"msgtype": "m.text", "body": MARKER_IN},
        )
        assert sent.__class__.__name__ == "RoomSendResponse", sent
        await asyncio.wait_for(inbound.wait(), timeout=12)
        assert delivered and MARKER_IN in delivered[0][1]
        assert "notify --matrix" in delivered[0][1]

        first = enqueue(outbox, room_id=room_id, message=MARKER_OUT, session="proof-session")
        await asyncio.wait_for(outbound.wait(), timeout=12)
        assert MARKER_OUT in outbound_bodies
        assert not first.exists()

        await stop_adapter(adapter_task)
        adapter_task = None
        outbound.clear()
        adapter, adapter_task = await start_adapter(env)
        second = enqueue(outbox, room_id=room_id, message=MARKER_RESTART, session="proof-session")
        await asyncio.wait_for(outbound.wait(), timeout=12)
        assert MARKER_RESTART in outbound_bodies
        assert not second.exists()
        assert not list(outbox.glob("*.json"))
        print("PASS: encrypted inbound, encrypted reply, and connector restart recovery")
    finally:
        if adapter_task:
            await stop_adapter(adapter_task)
        if owner_sync:
            owner_sync.cancel()
            try:
                await owner_sync
            except asyncio.CancelledError:
                pass
        await owner.close()
        if wolt.client_session:
            await wolt.close()


asyncio.run(main())
