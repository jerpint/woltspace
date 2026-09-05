"""The `ChannelConnector` seam.

A connector is a long-running child process that carries conversations between
an outside channel and this control plane. The supervisor owns its lifetime;
the connector only has to describe how it starts and why it is (or is not)
enabled.

There is exactly one real connector — Telegram. This is a seam, not a plugin
framework: adding a second one means adding a class to `CONNECTORS`.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Protocol, runtime_checkable

from .config import channel_config, config_path
from .layout import RuntimeLayout


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _bot_project(bot_dir: str, install_root: Path) -> Path | None:
    """Locate the uv project that owns python-telegram-bot.

    A wolt with its own adapter has `TELEGRAM_BOT_DIR=<wolt_dir>` and the
    module at `<wolt_dir>/wolt/bot/`, so probing only `<bot_dir>/bot` misses it
    and silently falls back to an interpreter with no telegram library — which
    then reports a remedy about the wrong thing. Fall back to the platform's
    own bot project, which is what actually has the dependency.
    """
    candidates = (
        Path(bot_dir) / "bot" / "pyproject.toml",
        Path(bot_dir) / "wolt" / "bot" / "pyproject.toml",
        install_root / "container" / "bot" / "pyproject.toml",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.parent
    return None


def _interpreter(
    bot_dir: str, isolation: str, install_root: Path
) -> tuple[str, ...]:
    """The interpreter that owns the adapter's dependencies.

    Container runs keep the `bot` uv project they have always used. Native runs
    stay inside the installed environment — never provisioning a venv under an
    installed package.
    """
    if isolation != "host":
        uv = shutil.which("uv")
        project = _bot_project(bot_dir, install_root)
        if uv and project is not None:
            return (uv, "run", "--project", str(project), "python")
    return (sys.executable,)


@dataclass(frozen=True)
class ConnectorPlan:
    """Everything the supervisor needs, with no secret in any public field."""

    name: str
    enabled: bool
    detail: str
    command: tuple[str, ...] = ()
    cwd: str = ""
    env: Mapping[str, str] = field(default_factory=dict)
    remedy: str = ""
    # An adjacent run of argv tokens that identifies this connector's process.
    # Neither `command[-1]` nor a bare substring will do: the dev-reload form
    # ends in "bot/", and the module name appears inside any file *named* after
    # it (`tail -f bot.telegram_adapter.log`). A token pair cannot.
    process_signature: tuple[str, ...] = ()

    def to_record(self) -> dict:
        """Public description. Never includes `env` — that carries the token."""
        return {
            "name": self.name,
            "enabled": self.enabled,
            "detail": self.detail,
            "command": list(self.command),
            "cwd": self.cwd or None,
            "remedy": self.remedy or None,
            "process_signature": list(self.process_signature) or None,
        }


@runtime_checkable
class ChannelConnector(Protocol):
    name: str

    def plan(
        self, layout: RuntimeLayout, env: Mapping[str, str] | None = None
    ) -> ConnectorPlan:
        """Describe how to run this connector under the current configuration."""


class TelegramConnector:
    """The existing Telegram adapter, behind the seam.

    Token resolution order: `TELEGRAM_BOT_TOKEN` in the environment, then
    `channels.telegram.token` in the data-root config. Enablement follows the
    same shape via `ENABLE_TELEGRAM_BOT` / `channels.telegram.enabled`, and a
    connector configured without a token stays disabled with a named remedy
    rather than crash-looping.
    """

    name = "telegram"

    def plan(
        self, layout: RuntimeLayout, env: Mapping[str, str] | None = None
    ) -> ConnectorPlan:
        values = dict(os.environ if env is None else env)
        settings = channel_config(layout, self.name, values)
        path = config_path(layout, values)

        # Two independent sources, deliberately not equal in authority.
        # The data root's own config is a *deliberate* statement: someone wrote
        # that file for this directory. The environment is ambient — the
        # container exports ENABLE_TELEGRAM_BOT and the production token into
        # every process it hosts, so any stray `woltspace serve` inherits them.
        configured = bool(settings.get("enabled", False))
        raw_enabled = values.get("ENABLE_TELEGRAM_BOT")
        from_env = _truthy(raw_enabled) if raw_enabled is not None else False
        env_disables = raw_enabled is not None and not from_env

        token = (values.get("TELEGRAM_BOT_TOKEN") or settings.get("token") or "").strip()
        remedy = (
            f'Set channels.telegram = {{"enabled": true, "token": "<bot token>"}} '
            f"in {path} (or export TELEGRAM_BOT_TOKEN)."
        )

        if env_disables or not (configured or from_env):
            return ConnectorPlan(self.name, False, "disabled", remedy=remedy)

        # Read the entrypoint marker from the same mapping as everything else,
        # so a plan is a pure function of (layout, environment).
        entrypoint = _truthy(values.get("WOLTSPACE_ENTRYPOINT", ""))
        if not configured and not entrypoint:
            # Ambient environment alone. Spawning here would put a second
            # long-poller on a token whose first poller is, by construction,
            # already running in the process that exported it — and Telegram
            # answers the loser of that race with 409.
            return ConnectorPlan(
                self.name,
                False,
                "enabled only by the ambient environment, and this is not the "
                "platform entrypoint",
                remedy=(
                    f"This process inherited ENABLE_TELEGRAM_BOT from its parent. To "
                    f"run a connector here, declare it for this data root in {path} "
                    f"— and use a different bot token, because one token can only be "
                    f"polled by one process."
                ),
            )
        if not token:
            return ConnectorPlan(
                self.name, False, "enabled without a bot token", remedy=remedy
            )

        bot_dir = values.get("TELEGRAM_BOT_DIR") or str(layout.install_root / "container")
        module = values.get("TELEGRAM_BOT_MODULE") or "bot.telegram_adapter"
        child_env = {
            "TELEGRAM_BOT_TOKEN": token,
            "WOLTS_DIR": str(layout.wolts_dir),
            "WOLTSPACE_DIR": str(layout.install_root),
            "WOLTSPACE_ISOLATION": layout.isolation,
            "WOLTSPACE_HOST": layout.host,
            "WOLTSPACE_PORT": str(layout.port),
            "PYTHONPATH": os.pathsep.join(
                part
                for part in (
                    bot_dir,
                    str(layout.runtime_lib),
                    values.get("PYTHONPATH", ""),
                )
                if part
            ),
        }
        allowed = values.get("TELEGRAM_ALLOWED_USERS") or settings.get("allowed_users")
        if isinstance(allowed, (list, tuple)):
            allowed = ",".join(str(item) for item in allowed)
        if allowed:
            child_env["TELEGRAM_ALLOWED_USERS"] = str(allowed)

        # In the container the adapter has always run from the `bot` uv project,
        # which owns python-telegram-bot. Natively that dependency comes from
        # the `connectors` extra of this very interpreter — check it here so a
        # missing extra is a named remedy instead of a crash loop.
        interpreter = _interpreter(bot_dir, layout.isolation, layout.install_root)
        if interpreter == (sys.executable,) and not _module_available("telegram"):
            return ConnectorPlan(
                self.name,
                False,
                "enabled but python-telegram-bot is not installed",
                remedy=(
                    "Reinstall with the connectors extra — from a checkout while "
                    "the package is unpublished: `uv tool install '.[connectors]'` "
                    "(after release: `uv tool install 'woltspace[connectors]'`), "
                    "then `woltspace start`."
                ),
            )

        detail = f"{module} from {bot_dir}"
        if _truthy(values.get("DEV_MODE", "")) and _module_available("watchfiles"):
            # Keep the container's dev hot-reload, still as one supervised child.
            command = (
                *interpreter, "-m", "watchfiles", "--filter", "python",
                f"{' '.join(interpreter)} -m {module}", "bot/",
            )
            detail += " (dev reload)"
        else:
            command = (*interpreter, "-m", module)

        return ConnectorPlan(
            name=self.name,
            enabled=True,
            detail=detail,
            command=command,
            cwd=bot_dir,
            env=child_env,
            remedy=remedy,
            # `-m <module>` is present as an adjacent pair in both the plain
            # and the dev-reload argv, and no filename can produce it.
            process_signature=("-m", module),
        )


TOKEN_BUSY_DETAIL = "another process is already polling this bot token"
TOKEN_BUSY_REMEDY = (
    "One bot token can only be polled by one process. Stop the other poller, or "
    "give this instance its own bot token in channels.telegram.token."
)


def telegram_token_is_busy(token: str, *, timeout: float = 3.0, opener=None) -> bool:
    """Ask Telegram whether someone else already owns this token's long poll.

    A 409 here is the whole answer: getUpdates is exclusive, so if another
    poller holds it we learn that *before* spawning a child that would fight it
    and leave one of the two deaf. Any other outcome — network down, timeout,
    an unexpected status — means we do not know, and not-knowing must not block
    a legitimate start.
    """
    import json as _json
    import urllib.error
    import urllib.request

    if not token:
        return False
    url = f"https://api.telegram.org/bot{token}/getUpdates?limit=1&timeout=0"
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(url, timeout=timeout) as response:
            response.read()
        return False
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            return True
        return False
    except Exception:
        return False


CONNECTORS: tuple[ChannelConnector, ...] = (TelegramConnector(),)


def plan_connectors(
    layout: RuntimeLayout, env: Mapping[str, str] | None = None
) -> list[ConnectorPlan]:
    return [connector.plan(layout, env) for connector in CONNECTORS]


def connector_secrets(plans: list[ConnectorPlan]) -> dict[str, str]:
    """Secrets an enabled connector resolved, for in-process reuse (e.g. notify).

    Returned for the control plane's own environment only. Never persisted.
    """
    secrets: dict[str, str] = {}
    for plan in plans:
        if not plan.enabled:
            continue
        for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS"):
            value = plan.env.get(key)
            if value:
                secrets[key] = value
    return secrets
