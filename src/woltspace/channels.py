"""The `ChannelConnector` seam.

A connector is a long-running child process that carries conversations between
an outside channel and this control plane. The supervisor owns its lifetime;
the connector only has to describe how it starts and why it is (or is not)
enabled.

There are three: Telegram, which carries chat; the TUI bridge, which carries
the browser terminal's keystrokes to tmux; and the wolf, which carries the
clock. The last one talks to no outside channel — but it is a long-running
child with exactly the same lifetime, and one supervisor is better than two.
This is a seam, not a plugin framework: adding another means adding a class to
`CONNECTORS`.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Protocol, runtime_checkable

from .compatibility import TUI_SERVICE_BINARY, tui_spec
from .config import channel_config, config_path
from .layout import RuntimeLayout


def default_tui_port(layout: RuntimeLayout) -> str:
    """The bridge follows the instance it serves.

    A fixed 3001 meant every control plane on a machine wanted the same pty
    port, so a second instance (`--port 7778` beside the colony on 7777)
    crash-looped its bridge forever on EADDRINUSE. Deriving it from the API
    port gives each instance its own by default; every explicit knob still
    wins, so a colony that already configured one is untouched.
    """
    return str(layout.port + 1)


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


TUI_BRIDGE_INSTALL_REMEDY = (
    f"Install {tui_spec()} so `{TUI_SERVICE_BINARY}` is on PATH — from a checkout "
    "while it is unpublished: `cd tui && npm pack && npm install -g "
    "./woltspace-tui-<version>.tgz` — or run from a checkout that has `tui/node_modules` "
    "(`cd tui && npm install`). Then `woltspace start`."
)


def resolve_tui_service(
    layout: RuntimeLayout,
    values: Mapping[str, str],
    *,
    which: Callable[[str], str | None] = shutil.which,
    runner: Callable = subprocess.run,
) -> tuple[tuple[str, ...], str, str]:
    """Find the pty bridge: (command, detail, why-not).

    Order: an explicit `WOLTSPACE_TUI_SERVICE_BIN`; the checkout's own
    `tui/src/tui-service.js` when the install root is a source tree with node
    modules beside it (dev checkouts and the container); the exactly matching
    `woltspace-tui-service` that `npm install -g @woltspace/tui` puts on PATH.
    An empty command means "not found", and the third element says why.
    """
    from .tui import _probe

    configured = values.get("WOLTSPACE_TUI_SERVICE_BIN", "").strip()
    if configured:
        return (configured,), f"{TUI_SERVICE_BINARY} from WOLTSPACE_TUI_SERVICE_BIN", ""

    node = which("node")
    script = layout.install_root / "tui" / "src" / "tui-service.js"
    if script.is_file():
        modules = (
            layout.install_root / "tui" / "node_modules" / "node-pty",
            layout.install_root / "node_modules" / "node-pty",
        )
        if not node:
            return (), "", f"{script} needs node on PATH"
        if any(path.is_dir() for path in modules):
            return (node, str(script)), f"node {script}", ""
        return (), "", f"{script} has no node-pty beside it"

    binary = which(TUI_SERVICE_BINARY)
    if binary:
        probe = _probe(binary, expected_binary=TUI_SERVICE_BINARY, runner=runner)
        if probe["valid"]:
            return (str(Path(binary)),), f"{TUI_SERVICE_BINARY} at {binary}", ""
        return (), "", f"{binary}: {probe.get('error', 'version mismatch')}"
    return (), "", f"no {TUI_SERVICE_BINARY} on PATH and no checkout tui/src beside {layout.install_root}"


class TuiBridgeConnector:
    """The browser terminal's pty bridge.

    `server/app.py` proxies every `/tui` websocket to a small Node service that
    attaches to the session's tmux through node-pty. `container/start.sh` used
    to launch it by hand, so a native control plane served a split view whose
    terminal pane could only ever say "Connection refused". Now it is a
    supervised child like Telegram: starts with the API, restarts if it dies,
    reports through `woltspace status`.

    Enabled by default. A guest never binds the pty port — the real instance
    already has it.
    """

    name = "tui"

    def plan(
        self,
        layout: RuntimeLayout,
        env: Mapping[str, str] | None = None,
        *,
        which: Callable[[str], str | None] = shutil.which,
        runner: Callable = subprocess.run,
    ) -> ConnectorPlan:
        values = dict(os.environ if env is None else env)
        settings = channel_config(layout, self.name, values)
        path = config_path(layout, values)

        raw = values.get("WOLTSPACE_TUI_BRIDGE")
        enabled = _truthy(raw) if raw is not None else bool(settings.get("enabled", True))
        port = str(
            values.get("WOLTSPACE_TUI_PORT") or values.get("TUI_PORT")
            or settings.get("port") or default_tui_port(layout)
        )
        enable_remedy = (
            f'Set channels.tui = {{"enabled": true}} in {path} '
            "(or unset WOLTSPACE_TUI_BRIDGE). Without it the split view's terminal pane cannot connect."
        )
        if not enabled:
            return ConnectorPlan(self.name, False, "disabled", remedy=enable_remedy)
        if not _truthy(values.get("WOLTSPACE_ENTRYPOINT", "")):
            return ConnectorPlan(
                self.name,
                False,
                "not the platform entrypoint; a guest never binds the pty port",
                remedy="Run the control plane through `woltspace start` to own the browser terminal.",
            )

        command, detail, why_not = resolve_tui_service(layout, values, which=which, runner=runner)
        if not command:
            return ConnectorPlan(
                self.name, False, f"pty bridge not found: {why_not}",
                remedy=TUI_BRIDGE_INSTALL_REMEDY,
            )
        child_env = {
            "TUI_PORT": port,
            "WOLT_DIR": str(layout.wolts_dir),
            "WOLTS_DIR": str(layout.wolts_dir),
        }
        return ConnectorPlan(
            name=self.name,
            enabled=True,
            detail=f"pty bridge on 127.0.0.1:{port} · {detail}",
            command=command,
            env=child_env,
            remedy=(
                f"Something else holds port {port}; pick another with WOLTSPACE_TUI_PORT "
                f"or channels.tui.port in {path}, or read {layout.logs_dir / 'connector-tui.log'}."
            ),
            # The script or binary path is one argv token no other process
            # reproduces; `node` alone would match every Node program.
            process_signature=(command[-1],),
        )


WOLF_MODULE = "creatures.wolf"


class WolfConnector:
    """🐺 The cron scheduler, behind the same seam.

    `container/creatures/wolf.py` reads every wolt's `wolf.json`, fires the
    schedules that are due, and posts them back to this very control plane.
    `container/start.sh` launched it by hand, so a colony run natively simply
    had no scheduler — every cron silently stopped firing. It is a supervised
    child now: starts with the API, restarts if it dies, shows up in
    `woltspace status`, and goes down with `woltspace stop`.

    Enabled by default — schedules already written in `wolf.json` are a
    standing instruction, and a scheduler that needs opting into is a
    scheduler nobody remembers to turn on. A guest never runs it: two wolves
    on one data root would fire every cron twice.
    """

    name = "wolf"

    def plan(
        self, layout: RuntimeLayout, env: Mapping[str, str] | None = None
    ) -> ConnectorPlan:
        values = dict(os.environ if env is None else env)
        settings = channel_config(layout, self.name, values)
        path = config_path(layout, values)

        raw = values.get("WOLTSPACE_WOLF")
        enabled = _truthy(raw) if raw is not None else bool(settings.get("enabled", True))
        remedy = (
            f'Set channels.wolf = {{"enabled": true}} in {path} '
            "(or unset WOLTSPACE_WOLF). Without it no wolt's wolf.json schedules fire."
        )
        if not enabled:
            return ConnectorPlan(self.name, False, "disabled", remedy=remedy)
        if not _truthy(values.get("WOLTSPACE_ENTRYPOINT", "")):
            return ConnectorPlan(
                self.name,
                False,
                "not the platform entrypoint; a guest never fires the schedules",
                remedy="Run the control plane through `woltspace start` to own the schedules.",
            )

        container = layout.install_root / "container"
        if not (container / "creatures" / "wolf.py").is_file():
            return ConnectorPlan(
                self.name,
                False,
                f"no {WOLF_MODULE} under {container}",
                remedy=(
                    "Reinstall woltspace so the platform runtime ships beside it, "
                    "then `woltspace start`."
                ),
            )

        # Same interpreter rule as the adapter: the container keeps its uv
        # project, a native run stays inside the installed environment. Wolf is
        # stdlib-only either way — this is one mechanism, not a special case.
        interpreter = _interpreter(str(container), layout.isolation, layout.install_root)
        child_env = {
            "WOLTS_DIR": str(layout.wolts_dir),
            "WOLTSPACE_DIR": str(layout.install_root),
            "WOLTSPACE_ISOLATION": layout.isolation,
            "WOLTSPACE_HOST": layout.host,
            # Wolf builds its own endpoint from this; see `server_url` there.
            "WOLTSPACE_PORT": str(layout.port),
            "PYTHONPATH": os.pathsep.join(
                part
                for part in (
                    str(container),
                    str(layout.runtime_lib),
                    values.get("PYTHONPATH", ""),
                )
                if part
            ),
        }
        return ConnectorPlan(
            name=self.name,
            enabled=True,
            detail=f"cron scheduler on {layout.endpoint} · {WOLF_MODULE} from {container}",
            command=(*interpreter, "-m", WOLF_MODULE),
            cwd=str(container),
            env=child_env,
            remedy=remedy,
            # `-m creatures.wolf` as an adjacent pair — a log file *named* after
            # the module cannot forge it.
            process_signature=("-m", WOLF_MODULE),
        )


CONNECTORS: tuple[ChannelConnector, ...] = (
    TelegramConnector(), TuiBridgeConnector(), WolfConnector(),
)


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
