"""The `ChannelConnector` seam.

A connector is a long-running child process that carries conversations between
an outside channel and this control plane. The supervisor owns its lifetime;
the connector only has to describe how it starts and why it is (or is not)
enabled.

Telegram and Slack carry chat; the wolf carries the clock. The last one talks
to no outside channel — but it is a long-running child with exactly the same
lifetime, and one supervisor is better than two.
This is a seam, not a plugin framework: adding another means adding a class to
`CONNECTORS`.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Protocol, runtime_checkable

from .config import channel_config, config_path
from .envvars import export_both
from .layout import RuntimeLayout


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _bot_project(bot_dir: str, install_root: Path) -> Path | None:
    """Locate the uv project that owns python-telegram-bot (the container's)."""
    candidates = (
        Path(bot_dir) / "bot" / "pyproject.toml",
        install_root / "container" / "bot" / "pyproject.toml",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.parent
    return None


def _interpreter(
    bot_dir: str, isolation: str, install_root: Path, dependency: str = "telegram"
) -> tuple[str, ...]:
    """The interpreter that owns the adapter's dependencies.

    This interpreter wins whenever it already has them: the slim container
    installs `woltspace`, so its own environment owns
    python-telegram-bot exactly as a native install does — and the `bot` uv
    project it would otherwise use now lives *inside* the installed wheel,
    where syncing a venv means writing a `.venv` into site-packages.

    A container built some other way (a checkout with missing dependencies)
    still falls back to that uv project, which is what it has always used.
    """
    if isolation != "host" and not _module_available(dependency):
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
    # When set, the child starts from a small system allowlist plus `env`, not
    # from the control plane's environment (which holds every connector's
    # secrets). Declared connectors run this way: they get only their own.
    isolated_env: bool = False
    # A folder connector talks to the lodge over a socket in this directory;
    # the supervisor mints a fresh token for it at every launch.
    socket_dir: str = ""
    # The owner ids the lodge checks this connector's events against.
    owners: tuple[str, ...] = ()

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


class SlackConnector:
    """A fail-closed, owner-only Slack DM connector."""

    name = "slack"

    def plan(
        self, layout: RuntimeLayout, env: Mapping[str, str] | None = None
    ) -> ConnectorPlan:
        values = dict(os.environ if env is None else env)
        settings = channel_config(layout, self.name, values)
        path = config_path(layout, values)
        configured = bool(settings.get("enabled", False))
        raw_enabled = values.get("ENABLE_SLACK_BOT")
        from_env = _truthy(raw_enabled) if raw_enabled is not None else False
        env_disables = raw_enabled is not None and not from_env
        bot_token = (values.get("SLACK_BOT_TOKEN") or settings.get("bot_token") or "").strip()
        app_token = (values.get("SLACK_APP_TOKEN") or settings.get("app_token") or "").strip()
        owner_user = (values.get("SLACK_OWNER_USER") or settings.get("owner_user") or "").strip()
        remedy = (
            f'Set channels.slack = {{"enabled": true, "bot_token": "<xoxb token>", '
            f'"app_token": "<xapp token>", "owner_user": "<Slack user ID>"}} in {path}.'
        )
        if env_disables or not (configured or from_env):
            return ConnectorPlan(self.name, False, "disabled", remedy=remedy)
        if not _truthy(values.get("WOLTSPACE_ENTRYPOINT", "")):
            return ConnectorPlan(
                self.name, False,
                "not the platform entrypoint; a guest never opens Socket Mode",
                remedy="Run the control plane through `woltspace start` to own Slack.",
            )
        missing = [
            label for label, value in (
                ("bot token", bot_token),
                ("app token", app_token),
                ("owner user ID", owner_user),
            ) if not value
        ]
        if missing:
            return ConnectorPlan(
                self.name, False, f"enabled without {', '.join(missing)}", remedy=remedy
            )
        if re.fullmatch(r"[UW][A-Z0-9]{8,}", owner_user) is None:
            return ConnectorPlan(
                self.name, False, "enabled with a malformed owner user ID", remedy=remedy
            )

        bot_dir = str(layout.install_root / "container")
        module = "bot.slack_adapter"
        child_env = export_both({
            "SLACK_BOT_TOKEN": bot_token,
            "SLACK_APP_TOKEN": app_token,
            "SLACK_OWNER_USER": owner_user,
            "WOLTSPACE_WOLTS_DIR": str(layout.wolts_dir),
            "WOLTSPACE_DIR": str(layout.install_root),
            "WOLTSPACE_ISOLATION": layout.isolation,
            "WOLTSPACE_HOST": layout.host,
            "WOLTSPACE_PORT": str(layout.port),
            "WOLTSPACE_API": layout.endpoint,
            "PYTHONPATH": os.pathsep.join(
                part for part in (
                    bot_dir, str(layout.runtime_lib), values.get("PYTHONPATH", "")
                ) if part
            ),
        })
        interpreter = _interpreter(
            bot_dir, layout.isolation, layout.install_root, "slack_bolt"
        )
        if interpreter == (sys.executable,) and not _module_available("slack_bolt"):
            return ConnectorPlan(
                self.name, False, "enabled but slack-bolt is not installed",
                remedy=(
                    "Reinstall woltspace to restore its dependencies: "
                    "`uv tool install --force woltspace` "
                    "(from a checkout: `uv tool install --force .`), then `woltspace start`."
                ),
            )
        detail = f"owner-only DMs · {module} from {bot_dir}"
        if _truthy(values.get("DEV_MODE", "")) and _module_available("watchfiles"):
            command = (
                *interpreter, "-m", "watchfiles", "--filter", "python",
                f"{' '.join(interpreter)} -m {module}", "bot/",
            )
            detail += " (dev reload)"
        else:
            command = (*interpreter, "-m", module)
        return ConnectorPlan(
            self.name, True, detail, command, bot_dir, child_env, remedy,
            ("-m", module),
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


WOLF_MODULE = "creatures.wolf"
VULTURE_MODULE = "creatures.vulture"


class _CreatureConnector:
    """A platform creature run as a supervised child of the control plane.

    Subclasses name the creature, its module, its env switch and what is lost
    when it is off; the plan is the same for all of them.
    """

    name: str
    module: str
    env_var: str
    role: str
    without: str

    def plan(
        self, layout: RuntimeLayout, env: Mapping[str, str] | None = None
    ) -> ConnectorPlan:
        values = dict(os.environ if env is None else env)
        settings = channel_config(layout, self.name, values)
        path = config_path(layout, values)

        raw = values.get(self.env_var)
        enabled = _truthy(raw) if raw is not None else bool(settings.get("enabled", True))
        remedy = (
            f'Set channels.{self.name} = {{"enabled": true}} in {path} '
            f"(or unset {self.env_var}). Without it {self.without}."
        )
        if not enabled:
            return ConnectorPlan(self.name, False, "disabled", remedy=remedy)
        if not _truthy(values.get("WOLTSPACE_ENTRYPOINT", "")):
            return ConnectorPlan(
                self.name,
                False,
                "not the platform entrypoint; a guest never runs the lodge's creatures",
                remedy=f"Run the control plane through `woltspace start` to own the {self.role}.",
            )

        container = layout.install_root / "container"
        module_file = container / Path(*self.module.split(".")).with_suffix(".py")
        if not module_file.is_file():
            return ConnectorPlan(
                self.name,
                False,
                f"no {self.module} under {container}",
                remedy=(
                    "Reinstall woltspace so the platform runtime ships beside it, "
                    "then `woltspace start`."
                ),
            )

        # Same interpreter rule as the adapter: the container keeps its uv
        # project, a native run stays inside the installed environment. The
        # creatures are stdlib-only either way — one mechanism, not a special case.
        interpreter = _interpreter(str(container), layout.isolation, layout.install_root)
        child_env = export_both({
            "WOLTSPACE_WOLTS_DIR": str(layout.wolts_dir),
            "WOLTSPACE_DIR": str(layout.install_root),
            "WOLTSPACE_ISOLATION": layout.isolation,
            "WOLTSPACE_HOST": layout.host,
            "WOLTSPACE_PORT": str(layout.port),
            # The whole address, not just the port. The wolf used to assume
            # loopback, so an instance bound to a LAN or IPv6 address got no
            # callbacks at all — every cron fired into a closed socket. This is
            # already the dialable form (see `RuntimeLayout.dialable_host`),
            # IPv6 brackets included; `server_url` there still falls back to
            # host+port and then to loopback.
            "WOLTSPACE_API": layout.endpoint,
            "PYTHONPATH": os.pathsep.join(
                part
                for part in (
                    str(container),
                    str(layout.runtime_lib),
                    values.get("PYTHONPATH", ""),
                )
                if part
            ),
        })
        return ConnectorPlan(
            name=self.name,
            enabled=True,
            detail=f"{self.role} on {layout.endpoint} · {self.module} from {container}",
            command=(*interpreter, "-m", self.module),
            cwd=str(container),
            env=child_env,
            remedy=remedy,
            # `-m <module>` as an adjacent pair — a log file *named* after
            # the module cannot forge it.
            process_signature=("-m", self.module),
        )


class WolfConnector(_CreatureConnector):
    """🐺 The cron scheduler, behind the same seam.

    `container/creatures/wolf.py` reads every wolt's `wolf.json`, fires the
    schedules that are due, and posts them back to this very control plane.
    The container's boot launched it by hand, so a colony run natively simply
    had no scheduler — every cron silently stopped firing. It is a supervised
    child now: starts with the API, restarts if it dies, shows up in
    `woltspace status`, and goes down with `woltspace stop`.

    Enabled by default — schedules already written in `wolf.json` are a
    standing instruction, and a scheduler that needs opting into is a
    scheduler nobody remembers to turn on. A guest never runs it: two wolves
    on one data root would fire every cron twice.
    """

    name = "wolf"
    module = WOLF_MODULE
    env_var = "WOLTSPACE_WOLF"
    role = "cron scheduler"
    without = "no wolt's wolf.json schedules fire"


class VultureConnector(_CreatureConnector):
    """🦅 The session reaper, behind the same seam.

    `container/creatures/vulture.py` rests sessions idle for longer than the
    lodge's "close idle sessions after" setting, marks sessions whose tmux is
    gone, and stops lodge sessions whose agent has exited. The container's
    boot used to launch it; once that went, nothing did, natively or in the
    container, and idle sessions piled up. It is supervised like the wolf now.

    A guest never runs it, for the same reason as the wolf: one data root,
    one reaper.
    """

    name = "vulture"
    module = VULTURE_MODULE
    env_var = "WOLTSPACE_VULTURE"
    role = "session reaper"
    without = "idle sessions are never closed and dead ones are never cleaned up"


class FolderConnector:
    """A chat connector that runs from its folder and talks over a socket (RFC #617).

    Bundled (`container/connectors/<name>/`, Telegram) or local
    (`<data root>/.space/connectors/<name>/`). Enabled by
    `channels.<name>.enabled` in the data root's config; a bundled connector
    may also be enabled by its `enable_env`, at the platform entrypoint only
    (an inherited environment never starts a second poller). It needs every
    secret its manifest declares, from the environment or its config key, and
    its process receives only those, plus its socket.

    Its owners are the lodge's setting, `channels.<name>.owners`; a bundled
    connector also reads where its owners were kept before (Telegram:
    TELEGRAM_ALLOWED_USERS, channels.telegram.allowed_users).
    """

    def __init__(self, spec):
        self.spec = spec
        self.name = spec.name

    def _owners(self, values: Mapping[str, str], settings: dict) -> tuple[str, ...]:
        spec = self.spec
        raw = settings.get("owners")
        if raw in (None, "", []) and spec.bundled:
            raw = (values.get(spec.owners_env) if spec.owners_env else None) or (
                settings.get(spec.owners_config) if spec.owners_config else None)
        if isinstance(raw, (list, tuple)):
            items = [str(item) for item in raw]
        else:
            items = str(raw or "").split(",")
        return tuple(item.strip() for item in items if item.strip())

    def plan(
        self, layout: RuntimeLayout, env: Mapping[str, str] | None = None
    ) -> ConnectorPlan:
        spec = self.spec
        values = dict(os.environ if env is None else env)
        settings = channel_config(layout, self.name, values)
        path = config_path(layout, values)
        keys = dict(spec.config_keys)
        example = ", ".join(f'"{keys.get(name, name.lower())}": "<{name}>"'
                            for name in spec.manifest.secrets)
        remedy = (f'Set channels.{self.name} = {{"enabled": true'
                  f'{", " + example if example else ""}}} in {path}.')
        configured = bool(settings.get("enabled", False))
        raw_enabled = values.get(spec.enable_env) if spec.enable_env else None
        from_env = _truthy(raw_enabled) if raw_enabled is not None else False
        if (raw_enabled is not None and not from_env) or not (configured or from_env):
            return ConnectorPlan(self.name, False, "disabled", remedy=remedy)
        if not configured and not _truthy(values.get("WOLTSPACE_ENTRYPOINT", "")):
            # Ambient environment alone: the process that exported it already
            # runs this connector, and one bot token can only be polled once.
            return ConnectorPlan(
                self.name, False,
                "enabled only by the ambient environment, and this is not the platform entrypoint",
                remedy=(f"This process inherited {spec.enable_env} from its parent. To run a "
                        f"connector here, declare it for this data root in {path} — and use a "
                        f"different bot token, because one token can only be polled by one process."),
            )
        secrets = {}
        for name in spec.manifest.secrets:
            value = values.get(name) or settings.get(keys.get(name, name.lower())) or ""
            secrets[name] = str(value).strip()
        missing = [name for name, value in secrets.items() if not value]
        if missing:
            return ConnectorPlan(self.name, False, f"enabled without {', '.join(missing)}",
                                 remedy=remedy)

        folder = spec.folder
        bot_dir = str(layout.install_root / "container")
        if spec.bundled:
            interpreter = _interpreter(bot_dir, layout.isolation, layout.install_root,
                                       spec.dependency or "telegram")
        else:
            interpreter = (sys.executable,)
        if (spec.dependency and interpreter == (sys.executable,)
                and not _module_available(spec.dependency)):
            return ConnectorPlan(
                self.name, False,
                f"enabled but {spec.dependency_label or spec.dependency} is not installed",
                remedy=("Reinstall woltspace to restore its dependencies: "
                        "`uv tool install --force woltspace` "
                        "(from a checkout: `uv tool install --force .`), then `woltspace start`."
                        if spec.bundled else
                        f"Install {spec.dependency} for the lodge's Python, then `woltspace start`."),
            )
        command = tuple(part for arg in spec.command
                        for part in (interpreter if arg == "{python}" else (arg,)))
        # Its last two argv tokens: the interpreter and the program. An editor
        # or a `tail` naming the program file has no interpreter before it.
        signature = tuple(command[-2:])
        dev = ""
        if _truthy(values.get("DEV_MODE", "")) and _module_available("watchfiles"):
            # The container's dev hot-reload, still as one supervised child:
            # restart the program when a file in its folder changes.
            inner = " ".join(command)
            # A process table loses argv boundaries: the program, then its folder.
            signature = (command[-1], folder)
            command = (*interpreter, "-m", "watchfiles", "--filter", "python", inner, folder)
            dev = " (dev reload)"
        owners = self._owners(values, settings)
        socket_dir = Path(layout.wolts_dir) / ".space" / "run" / "connectors"
        child_env = export_both({
            **secrets,
            "WOLTSPACE_CONNECTOR_NAME": self.name,
            "WOLTSPACE_CONNECTOR_SOCKET": str(socket_dir / f"{self.name}.sock"),
            "WOLTSPACE_CONNECTOR_DATA": str(Path(layout.wolts_dir) / ".space" / self.name),
            "WOLTSPACE_CONNECTOR_OUTBOX": str(Path(layout.wolts_dir) / ".space" / "outbox"),
            "WOLTSPACE_CONNECTOR_OWNERS": ",".join(owners),
            "PYTHONPATH": os.pathsep.join(
                part for part in (str(layout.runtime_lib), values.get("PYTHONPATH", "")) if part
            ),
        })
        if spec.owners_env and spec.bundled:
            # The connector filters on the provider side too, under its old name.
            child_env[spec.owners_env] = ",".join(owners)
        where = "bundled" if spec.bundled else "local"
        return ConnectorPlan(
            self.name, True, f"{spec.title} · {where} connector {folder}{dev}",
            command, folder, child_env, remedy,
            process_signature=signature,
            isolated_env=True, socket_dir=str(socket_dir), owners=owners,
        )


def _registry(layout: RuntimeLayout | None = None):
    """The connector registry, from this install's runtime library."""
    lib_dir = str(Path(__file__).resolve().parent / "_bundle" / "container" / "lib")
    candidates = [str(layout.runtime_lib)] if layout is not None else []
    candidates += [lib_dir, str(Path(__file__).resolve().parents[2] / "container" / "lib")]
    for candidate in candidates:
        if Path(candidate, "connector_registry.py").is_file():
            if candidate not in sys.path:
                sys.path.insert(0, candidate)
            break
    import connector_registry

    return connector_registry


CONNECTORS: tuple[ChannelConnector, ...] = (
    SlackConnector(), WolfConnector(), VultureConnector(),
)


def folder_connectors(layout: RuntimeLayout | None = None) -> list[FolderConnector]:
    """Every connector folder, bundled first (Telegram), then the data root's own."""
    try:
        registry = _registry(layout)
    except ImportError:
        return []
    wolts_dir = layout.wolts_dir if layout is not None else None
    bespoke = {c.name for c in CONNECTORS}
    return [FolderConnector(spec) for spec in registry.declared(wolts_dir)
            if spec.name not in bespoke]


def TelegramConnector() -> FolderConnector:
    """Telegram, the connector the lodge ships in its own folder."""
    spec = _registry().spec("telegram")
    if spec is None:
        raise RuntimeError("the bundled telegram connector folder is missing")
    return FolderConnector(spec)


def all_connectors(layout: RuntimeLayout | None = None) -> tuple[ChannelConnector, ...]:
    """The connector folders, then the built-in classes."""
    return (*folder_connectors(layout), *CONNECTORS)


def plan_connectors(
    layout: RuntimeLayout, env: Mapping[str, str] | None = None
) -> list[ConnectorPlan]:
    return [connector.plan(layout, env) for connector in all_connectors(layout)]


def _declared_secrets(name: str) -> tuple[str, ...]:
    try:
        spec = _registry().spec(name)
    except ImportError:
        return ()
    return spec.manifest.secrets if spec is not None and spec.launchable else ()


def connector_secrets(plans: list[ConnectorPlan]) -> dict[str, str]:
    """Secrets an enabled connector resolved, for in-process reuse (e.g. notify).

    Returned for the control plane's own environment only. Never persisted.
    """
    secrets: dict[str, str] = {}
    for plan in plans:
        if not plan.enabled:
            continue
        for key in (
            "TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS",
            "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_OWNER_USER",
            *_declared_secrets(plan.name),
        ):
            value = plan.env.get(key)
            if value:
                secrets[key] = value
    return secrets
