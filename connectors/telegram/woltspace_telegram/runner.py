"""The connector process: logging that cannot leak the token, then the two loops.

Development-only entry point for now: the lodge does not launch it (the
supervisor launch and the hardened token handoff are the next slice), and
the switch it depends on refuses to flip while the lodge runs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

import httpx

from . import LEASE_WAIT, LodgeRefused, NotOwner, StartRefused, TelegramConnector, redact

logger = logging.getLogger("woltspace_telegram")


def configure_logging(bot_token: str, level: int = logging.INFO) -> None:
    """Root logging for this process, with the token unable to reach a line.

    Two layers: the HTTP client's own loggers (`httpx`, `httpcore`) say
    every request URL at INFO/DEBUG, and the Bot API URL embeds the token,
    so they are kept at WARNING; and every record of every logger goes
    through a factory that redacts the token from its message and
    arguments, so a description or an exception that quotes the URL is
    cleaned wherever it is logged from.
    """
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        logging.basicConfig(level=level, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    for name in ("httpx", "httpcore", "httpcore.http11", "httpcore.connection"):
        logging.getLogger(name).setLevel(logging.WARNING)
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        if bot_token:
            record.msg = redact(record.msg, bot_token)
            if isinstance(record.args, dict):
                record.args = {k: redact(v, bot_token) for k, v in record.args.items()}
            elif record.args:
                record.args = tuple(redact(a, bot_token) for a in record.args)
            if record.exc_info and record.exc_info[1] is not None:
                exc = record.exc_info[1]
                exc.args = tuple(redact(a, bot_token) for a in exc.args)
        return record

    logging.setLogRecordFactory(factory)


def connector_token() -> str:
    """Plain file or environment handoff: fixture grade, not the hardened design."""
    path = os.environ.get("WOLTSPACE_CONNECTOR_TOKEN_FILE")
    if path:
        with open(path, encoding="utf-8") as handle:
            token = json.load(handle)["token"]
        os.unlink(path)
        return token
    token = os.environ.get("WOLTSPACE_CONNECTOR_TOKEN", "")
    if not token:
        sys.exit("woltspace_telegram: no connector token (WOLTSPACE_CONNECTOR_TOKEN_FILE)")
    return token


async def run(connector: TelegramConnector, *, bot_token: str) -> int:
    """Both loops, owned together: neither outlives the other.

    Whatever ends the run (a refusal from the lodge, an unexpected error in
    one loop, or this task being cancelled), both loop tasks are cancelled
    and awaited to completion before `run` returns, so every provider call
    and its cleanup has ended by the time the caller releases the run lock.
    """
    try:
        state = await connector.start()
    except LodgeRefused:
        logger.error("the lodge refused the connector token; exiting")
        return 1
    except (NotOwner, StartRefused) as exc:
        logger.error("%s; exiting", exc)
        return 1
    logger.info("started: cursor=%s paused=%s owned=%s", state.get("cursor"), state.get("paused"), state.get("runtime_active"))

    async def inbound():
        while True:
            try:
                report = await connector.poll_once()
                if report.fenced:
                    await asyncio.sleep(5)
            except LodgeRefused:
                raise
            except Exception as exc:  # noqa: BLE001 - keep polling
                logger.warning("poll failed: %s", redact(exc, bot_token))
                await asyncio.sleep(2)

    async def outbound():
        while True:
            try:
                report = await connector.deliver_once(wait=LEASE_WAIT)
                if report.paused or report.fenced:
                    await asyncio.sleep(5)
            except LodgeRefused:
                raise
            except Exception as exc:  # noqa: BLE001 - keep delivering
                logger.warning("deliver failed: %s", redact(exc, bot_token))
                await asyncio.sleep(2)

    tasks = [asyncio.create_task(inbound(), name="inbound"), asyncio.create_task(outbound(), name="outbound")]
    code = 0
    try:
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            if task.cancelled():
                continue
            error = task.exception()
            if isinstance(error, LodgeRefused):
                logger.error("the lodge refused the connector token; exiting")
                code = 1
            elif error is not None:
                logger.error("%s loop failed: %s", task.get_name(), redact(error, bot_token))
                code = 1
    finally:
        # On every way out, cancellation of this task included: both loops
        # are cancelled and awaited, so no provider call is in flight after here.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return code


async def main() -> int:
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        sys.exit("woltspace_telegram: TELEGRAM_BOT_TOKEN is not set")
    configure_logging(bot_token)
    api = os.environ.get("WOLTSPACE_API", "http://127.0.0.1:7777")
    async with httpx.AsyncClient(base_url=api, timeout=60.0) as lodge, httpx.AsyncClient(
        base_url="https://api.telegram.org", timeout=httpx.Timeout(30.0, read=120.0),
    ) as bot:
        connector = TelegramConnector(lodge, bot, bot_token=bot_token, connector_token=connector_token())
        try:
            return await run(connector, bot_token=bot_token)
        finally:
            # Both loops are over and so are their calls: the lock may go.
            connector.stop()
