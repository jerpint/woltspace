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
import time

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


HANDOFF_MAX_BYTES = 4096
RUN_DIR = "run"


def read_handoff(path: str, *, root: str, expected: dict, window: float, now: float | None = None) -> dict:
    """Consume the handoff file below the anchored runtime root, validating everything first.

    `root` is the runtime root the supervisor named, independently of the
    file: the file must be exactly `<root>/run/<name>`, spelled with that
    root (an alias of the root is not the root). The root is opened as a
    directory we own; `run` is opened relative to it `O_DIRECTORY|
    O_NOFOLLOW` (a link anywhere below the root is refused) and must be
    ours and 0700. The file is then *claimed* by an atomic rename to a name
    of this process's own, so exactly one consumer ever reads it; the
    claimed file is opened `O_NOFOLLOW|O_NONBLOCK` relative to `run` and
    must be a small private regular file (a FIFO cannot block us). The
    envelope must name the lodge, instance and generation this process was
    told to expect, and be issued within `window` seconds. Whatever the
    outcome, the claimed file is removed: a refused handoff is gone too,
    and the lodge sees "taken but never activated" and revokes.
    Raises `SystemExit` with the reason otherwise: no guess runs.
    """
    import stat

    directory, name = os.path.split(path)
    if not root or directory != os.path.join(root, RUN_DIR) or os.sep in name or name in ("", ".", ".."):
        raise SystemExit("woltspace_telegram: the handoff is not below the runtime root; not starting")
    if os.path.realpath(root) != root:
        # The supervisor names the root by its canonical name; an alias is not it.
        raise SystemExit("woltspace_telegram: the runtime root is not canonical; not starting")
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise SystemExit(f"woltspace_telegram: cannot open the runtime root: {exc}") from None
    try:
        if os.fstat(root_fd).st_uid != os.getuid():
            raise SystemExit("woltspace_telegram: the runtime root is not ours; not starting")
        try:
            dir_fd = os.open(RUN_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        except OSError as exc:
            raise SystemExit(f"woltspace_telegram: cannot open the handoff directory: {exc}") from None
    except BaseException:
        os.close(root_fd)
        raise
    try:
        dir_info = os.fstat(dir_fd)
        if dir_info.st_uid != os.getuid() or dir_info.st_mode & 0o077:
            raise SystemExit("woltspace_telegram: the handoff directory is not private; not starting")
        claimed = f".{name}.{os.getpid()}.{threading_ident()}.taken"
        try:
            os.rename(name, claimed, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except FileNotFoundError:
            raise SystemExit("woltspace_telegram: the handoff was already consumed; not starting") from None
        except OSError as exc:
            raise SystemExit(f"woltspace_telegram: cannot claim the handoff: {exc}") from None
        try:
            try:
                descriptor = os.open(claimed, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
            except OSError as exc:
                raise SystemExit(f"woltspace_telegram: cannot open the handoff: {exc}") from None
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode):
                    raise SystemExit("woltspace_telegram: the handoff is not a regular file; not starting")
                if info.st_size > HANDOFF_MAX_BYTES:
                    raise SystemExit("woltspace_telegram: the handoff is too large; not starting")
                if info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise SystemExit("woltspace_telegram: the handoff is not private; not starting")
                raw = os.read(descriptor, HANDOFF_MAX_BYTES)
            finally:
                os.close(descriptor)
            try:
                data = json.loads(raw)
            except ValueError:
                raise SystemExit("woltspace_telegram: the handoff does not parse; not starting") from None
            if not isinstance(data, dict) or not isinstance(data.get("token"), str) or not data["token"]:
                raise SystemExit("woltspace_telegram: the handoff carries no token; not starting")
            for key in ("lodge_id", "instance_id", "generation"):
                if str(data.get(key)) != str(expected.get(key)):
                    raise SystemExit(f"woltspace_telegram: the handoff's {key} is not the one expected; not starting")
            issued = data.get("issued")
            current = time.time() if now is None else now
            if not isinstance(issued, (int, float)) or not (current - window <= issued <= current + 1):
                raise SystemExit("woltspace_telegram: the handoff is stale; not starting")
            return data
        finally:
            try:
                os.unlink(claimed, dir_fd=dir_fd)
            except OSError:
                pass
    finally:
        os.close(dir_fd)
        os.close(root_fd)


def threading_ident() -> int:
    import threading

    return threading.get_ident()


def connector_token() -> tuple[str, dict | None]:
    """(token, activation): the handoff file the supervisor named, consumed and validated
    against the expectations in the environment, with those expectations to activate it;
    or, for development, a live token from the environment and no activation."""
    path = os.environ.get("WOLTSPACE_CONNECTOR_TOKEN_FILE")
    if path:
        expected = {
            "lodge_id": os.environ.get("WOLTSPACE_LODGE_ID", ""),
            "instance_id": os.environ.get("WOLTSPACE_CONNECTOR_INSTANCE", ""),
            "generation": os.environ.get("WOLTSPACE_CONNECTOR_GENERATION", ""),
        }
        try:
            window = float(os.environ.get("WOLTSPACE_HANDOFF_WINDOW", "10"))
        except ValueError:
            window = 10.0
        token = read_handoff(
            path, root=os.environ.get("WOLTSPACE_HANDOFF_ROOT", ""), expected=expected, window=window,
        )["token"]
        return token, {**expected, "generation": int(expected["generation"])}
    token = os.environ.get("WOLTSPACE_CONNECTOR_TOKEN", "")
    if not token:
        sys.exit("woltspace_telegram: no connector token (WOLTSPACE_CONNECTOR_TOKEN_FILE)")
    return token, None


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
        token, activation = connector_token()
        connector = TelegramConnector(
            lodge, bot, bot_token=bot_token, connector_token=token, activation=activation,
        )
        try:
            return await run(connector, bot_token=bot_token)
        finally:
            # Both loops are over and so are their calls: the lock may go.
            connector.stop()
