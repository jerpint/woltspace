"""`python -m woltspace_telegram`: development-only runner for the Telegram connector.

`WOLTSPACE_API` names the lodge, `TELEGRAM_BOT_TOKEN` the bot, and the
connector token arrives through `WOLTSPACE_CONNECTOR_TOKEN_FILE` (read
once, then unlinked) or `WOLTSPACE_CONNECTOR_TOKEN`; never on argv, never
in a log line (see `runner.configure_logging`). Not launched by the lodge
yet: the supervisor launch and the hardened handoff are the next slice.
"""

from __future__ import annotations

import asyncio
import sys

from .runner import main

if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
