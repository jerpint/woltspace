"""`python -m woltspace_connector_fake`: echo every delivery to stdout and ack it.

Fixture only. This is the shape of the runner contract in one screen, not
the production runner: `WOLTSPACE_API` names the lodge and the token comes
from the file `WOLTSPACE_CONNECTOR_TOKEN_FILE` names (read once, then
unlinked) or, for tests, from `WOLTSPACE_CONNECTOR_TOKEN`; never from argv.
The hardened handoff (exclusive create, no-follow, bounded size, lodge id
and generation checked) belongs to the supervisor slice and is not done
here. There is no provider: a delivery is "sent" by printing it.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import httpx

from . import FakeConnector, FakeProvider


def _token() -> str:
    # Plain open/json/unlink: enough for a fixture, not the hardened handoff.
    path = os.environ.get("WOLTSPACE_CONNECTOR_TOKEN_FILE")
    if path:
        with open(path, encoding="utf-8") as handle:
            token = json.load(handle)["token"]
        os.unlink(path)
        return token
    token = os.environ.get("WOLTSPACE_CONNECTOR_TOKEN", "")
    if not token:
        sys.exit("woltspace_connector_fake: no token (WOLTSPACE_CONNECTOR_TOKEN_FILE)")
    return token


async def _main() -> int:
    api = os.environ.get("WOLTSPACE_API", "http://127.0.0.1:7777")
    provider = FakeProvider()
    async with httpx.AsyncClient(base_url=api, timeout=60.0) as client:
        connector = FakeConnector(client, _token(), provider)
        while True:
            try:
                leased = await connector.lease(10, wait=30)
            except httpx.HTTPError as exc:
                print(f"lease failed: {exc}", file=sys.stderr)
                await asyncio.sleep(2)
                continue
            if leased.status_code == 401:
                print("token refused; exiting", file=sys.stderr)
                return 1
            for delivery in leased.json().get("deliveries", []):
                print(json.dumps({k: delivery[k] for k in ("delivery_id", "conversation", "text")}))
                await connector.ack(
                    delivery["delivery_id"], delivery["attempt_id"],
                    provider_message_id=provider.send(delivery, []),
                )


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(_main()))
    except KeyboardInterrupt:
        sys.exit(0)
