"""The fake-transport connector: speaks the whole connector protocol, to nobody.

Tests drive it against the lodge's ASGI app to exercise every endpoint,
refusal and limit without a provider. `FakeProvider` stands in for the chat
service: it remembers what the connector "sent" and can be told to fail.

httpx only; it is a client of the API and knows nothing about core's files.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

PREFIX = "/connector/v1"


class ProviderDown(RuntimeError):
    """The pretend chat service refused or timed out."""


@dataclass
class FakeProvider:
    sent: list[dict] = field(default_factory=list)
    # Exceptions to raise on the next sends, in order. A ProviderDown is a
    # retryable failure; anything else the connector reports as permanent.
    failures: list[Exception] = field(default_factory=list)
    _counter: int = 0

    def send(self, delivery: dict, files: list[bytes]) -> str:
        if self.failures:
            raise self.failures.pop(0)
        self._counter += 1
        message_id = f"msg-{self._counter}"
        self.sent.append({
            "provider_message_id": message_id, "delivery_id": delivery["delivery_id"],
            "conversation": delivery["conversation"], "text": delivery["text"],
            "files": files, "reply_footer": delivery.get("reply_footer", ""),
        })
        return message_id


class FakeConnector:
    """One connector process: a client, a token, and the provider it fronts."""

    def __init__(
        self, client: httpx.AsyncClient, token: str, provider: FakeProvider | None = None,
        *, crash_before_ack: bool = False,
    ):
        self.client = client
        self.token = token
        self.provider = provider or FakeProvider()
        # Simulate a process that sends, then dies before telling the lodge.
        self.crash_before_ack = crash_before_ack

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    # ---------------------------------------------------------------- inbound

    async def send_event(
        self, event_id: str, sender: str, conversation: str, text: str = "", *,
        thread: str | None = None, attachments: tuple[str, ...] = (),
        reply_to: str | None = None, display: str = "", **extra,
    ) -> httpx.Response:
        payload = {
            "event_id": event_id,
            "sender": {"provider_id": sender, "display": display},
            "conversation": {"provider_id": conversation, "thread": thread},
            "text": text,
            "attachments": [{"upload_id": upload_id} for upload_id in attachments],
        }
        if reply_to is not None:
            payload["reply_to"] = reply_to if isinstance(reply_to, dict) else {"delivery_id": reply_to}
        payload.update(extra)
        return await self.client.post(f"{PREFIX}/events", json=payload, headers=self.headers)

    async def upload(self, data: bytes, name: str = "file.bin", *, chunk: int = 0) -> httpx.Response:
        content = data
        if chunk:
            async def stream():
                for offset in range(0, len(data), chunk):
                    yield data[offset:offset + chunk]
            content = stream()
        return await self.client.post(
            f"{PREFIX}/uploads", params={"name": name}, content=content,
            headers={**self.headers, "Content-Type": "application/octet-stream"},
        )

    # --------------------------------------------------------------- outbound

    async def lease(self, max_items: int = 10, wait: float = 0) -> httpx.Response:
        return await self.client.post(
            f"{PREFIX}/deliveries/lease", json={"max": max_items, "wait": wait},
            headers=self.headers,
        )

    async def ack(
        self, delivery_id: str, attempt_id: str, *, ok: bool = True,
        provider_message_id: str | None = None, error: str = "", retry: bool = True,
    ) -> httpx.Response:
        body: dict = {"attempt_id": attempt_id, "ok": ok}
        if ok:
            body["provider_message_id"] = provider_message_id
        else:
            body.update({"error": error, "retry": retry})
        return await self.client.post(
            f"{PREFIX}/deliveries/{delivery_id}/ack", json=body, headers=self.headers,
        )

    async def fetch_file(self, file_id: str, attempt_id: str | None) -> httpx.Response:
        params = {} if attempt_id is None else {"attempt_id": attempt_id}
        return await self.client.get(f"{PREFIX}/files/{file_id}", params=params, headers=self.headers)

    async def run_once(self, max_items: int = 10) -> list[dict]:
        """Lease, send through the provider, ack: one turn of a real connector's loop.

        Returns the ack results, one per delivery. A provider failure is acked
        as retryable when it is a ProviderDown, permanent otherwise.
        """
        leased = await self.lease(max_items)
        leased.raise_for_status()
        results = []
        for delivery in leased.json()["deliveries"]:
            attempt_id = delivery["attempt_id"]
            files = []
            for attachment in delivery["attachments"]:
                response = await self.fetch_file(attachment["file_id"], attempt_id)
                response.raise_for_status()
                files.append(response.content)
            try:
                message_id = self.provider.send(delivery, files)
            except Exception as exc:
                ack = await self.ack(
                    delivery["delivery_id"], attempt_id, ok=False, error=str(exc),
                    retry=isinstance(exc, ProviderDown),
                )
                results.append(ack.json())
                continue
            if self.crash_before_ack:
                results.append({"crashed": True, "delivery_id": delivery["delivery_id"]})
                continue
            ack = await self.ack(delivery["delivery_id"], attempt_id, provider_message_id=message_id)
            results.append(ack.json())
        return results
