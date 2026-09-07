"""Is the harness already logged in? — one answer, for everyone who asks.

There are two ways a colony can be authenticated, and for a long time boot only
knew about one of them:

  1. a credentials file, written by `/login` — `~/.claude/.credentials.json`
  2. a token handed to the process in its environment — `CLAUDE_CODE_OAUTH_TOKEN`
     (or an `ANTHROPIC_API_KEY`), which Claude Code reads as an override

Three places tested only (1): the container entrypoint's greeting, the
`/onboard-status` route, and the viewport's onboarding fallback. A colony booted
with a token in its environment therefore spawned sessions that authenticated
perfectly while the human's own window was sent to `wclaude /login` — and Claude
Code's login screen warns that continuing there *replaces* the working token. The
greeting invited a user to break a working install.

So the question gets asked once, here, and the answer is shared. Environment is
read at call time rather than import time: the entrypoint assembles its
environment during boot, and a stale snapshot would reintroduce the bug it fixes.

**Presence is not validity.** Nothing here can tell a live token from an expired
or revoked one — checking would cost a real API request on every boot — so a
colony whose token has died still gets the wolt greeting rather than a login
screen, and its sessions fail at the harness instead. That is the deliberate
trade: inviting `/login` against a *working* token is the worse failure, because
Claude Code's own login flow warns it will replace the credential you still had.
The recovery path for a dead token is to remove it and let the file path take
over — unset `CLAUDE_CODE_OAUTH_TOKEN` (or `ANTHROPIC_API_KEY`) wherever the
colony gets its environment and restart, and onboarding returns. `auth_source()`
exists so a user debugging exactly that can see which of the two answered.
"""

import os
from pathlib import Path
from typing import Mapping

# Claude Code's own precedence: an explicit token in the environment overrides
# whatever is on disk. Either one means a session will start logged in.
CLAUDE_TOKEN_VARS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")

CLAUDE_CREDENTIALS = Path(".claude") / ".credentials.json"


def claude_token_in_env(env: Mapping[str, str] | None = None) -> bool:
    """Whether a usable Claude token is present in the environment."""
    values = os.environ if env is None else env
    return any((values.get(name) or "").strip() for name in CLAUDE_TOKEN_VARS)


def auth_source(home: Path | str, env: Mapping[str, str] | None = None) -> str:
    """Which credential answers for this HOME: the file, the environment, or none.

    Returned by `/onboard-status` and `woltspace doctor` so "I am logged in but
    nothing works" has a first question with an answer: if this says
    `env-token`, the token in the environment is the thing to remove.
    """
    if (Path(home) / CLAUDE_CREDENTIALS).is_file():
        return "credentials-file"
    if claude_token_in_env(env):
        return "env-token"
    return "none"


def claude_authenticated(home: Path | str, env: Mapping[str, str] | None = None) -> bool:
    """Whether a Claude session launched from this HOME starts logged in.

    Presence, not validity — see the module docstring.
    """
    return auth_source(home, env) != "none"
