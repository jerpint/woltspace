"""One foreground control-plane entrypoint for native and container runs."""

from __future__ import annotations

import os
import signal
import uuid
from dataclasses import dataclass, field
from typing import Callable

from .layout import RuntimeLayout
from .instance import DataRootLock
from .adoption import adopt_runtime_sessions
from .doctor import ensure_container_mounts, ensure_data_root_available
from .channels import (
    TOKEN_BUSY_DETAIL,
    TOKEN_BUSY_REMEDY,
    ConnectorPlan,
    connector_secrets,
    plan_connectors,
    telegram_token_is_busy,
)
from .channel_supervisor import ChannelSupervisor

# How long uvicorn may wait for in-flight handlers (open websockets included)
# once told to stop. Well inside `woltspace stop`'s own 10s patience.
GRACEFUL_SHUTDOWN_SECONDS = 3.0


def _note_signal(signum, frame) -> None:
    """Swallow the SIGTERM uvicorn re-raises after shutdown; see Supervisor.run."""


@dataclass
class Supervisor:
    layout: RuntimeLayout
    reload: bool = False
    log_level: str = "info"
    instance_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    probe_token: Callable[[str], bool] = staticmethod(telegram_token_is_busy)

    def prepare(self) -> None:
        ensure_container_mounts(self.layout)
        # Before creating anything: is this data root already someone's? Runs
        # here rather than in doctor because `serve` is normally --no-doctor.
        ensure_data_root_available(self.layout)
        self.layout.apply_environment()
        self.layout.platform_state.mkdir(parents=True, exist_ok=True)
        self.layout.logs_dir.mkdir(parents=True, exist_ok=True)
        os.environ["WOLTSPACE_INSTANCE_ID"] = self.instance_id
        # Native runs with the tunnel off unless someone says otherwise. This
        # has to happen before the entrypoint early-return below: the server's
        # own default is "true", so a `woltspace start` on a Mac that skipped
        # this line published a public quick tunnel for the lodge.
        if self.layout.isolation == "host":
            os.environ.setdefault("WOLTSPACE_PUBLIC_TUNNEL", "false")
        if self.layout.is_entrypoint:
            return
        # A guest in the container never publishes. The container exports
        # WOLTSPACE_PUBLIC_TUNNEL=true to everything it hosts, and the tunnel
        # lifecycle is process-wide: starting one here would race the real
        # instance's cloudflared, and stopping would delete its state file.
        # (A native guest already got the off-default above; an explicit
        # opt-in there is someone's deliberate choice and stays.)
        if self.layout.isolation != "host":
            os.environ["WOLTSPACE_PUBLIC_TUNNEL"] = "false"

    def channel_supervisor(self) -> ChannelSupervisor:
        """Plan connectors and share their resolved secrets with this process.

        `notify` and the settings page read the token from the environment; the
        connector config is the single place it is resolved, and it stays in
        memory — nothing here writes a credential to disk.
        """
        # Order matters. A connector orphaned by a previous control plane still
        # holds the token, so asking Telegram first would always hear 409 and
        # disable the connector — and only *then* reap the orphan, leaving the
        # channel down with nothing running. Clear our own debris, then ask.
        planned = plan_connectors(self.layout)
        reaper = ChannelSupervisor(self.layout, planned)
        reaped = reaper.reap_orphans()
        if reaped:
            print(f"[connectors] reaped {len(reaped)} orphaned connector(s): {reaped}")

        plans = [self._refuse_busy_token(plan) for plan in planned]
        for key, value in connector_secrets(plans).items():
            os.environ.setdefault(key, value)
        for plan in plans:
            # The server proxies `/tui` to whatever port the bridge binds, and
            # `server.config` reads TUI_PORT at import — so it must be in the
            # environment before `server.app` is imported in run().
            if plan.name == "tui" and plan.enabled:
                os.environ["TUI_PORT"] = plan.env["TUI_PORT"]
        return ChannelSupervisor(self.layout, plans, already_reaped=True)

    def _refuse_busy_token(self, plan: ConnectorPlan) -> ConnectorPlan:
        """Never spawn a poller onto a token someone else already holds.

        The 409 detection in the supervisor protects *this* connector after the
        fact. This protects the one that was already there, which is the one
        that has no idea we exist.
        """
        if not plan.enabled or plan.name != "telegram":
            return plan
        token = plan.env.get("TELEGRAM_BOT_TOKEN", "")
        if not self.probe_token(token):
            return plan
        return ConnectorPlan(
            plan.name, False, TOKEN_BUSY_DETAIL, remedy=TOKEN_BUSY_REMEDY
        )

    def run(self) -> None:
        self.prepare()
        import uvicorn
        # uvicorn captures SIGTERM for the length of serve() and, on the way
        # out, restores the previous disposition and *re-raises* the signal.
        # The previous disposition was the default — death — so the process
        # ended between "Finished server process" and the `finally` below, and
        # every connector was left running for the next start's reaper to
        # find. A handler that merely notes the signal lets the re-raise land
        # here instead, so the children are stopped by the parent that owns them.
        previous = signal.signal(signal.SIGTERM, _note_signal)
        try:
            self._serve(uvicorn)
        finally:
            signal.signal(signal.SIGTERM, previous)

    def _serve(self, uvicorn) -> None:
        with DataRootLock(self.layout, self.instance_id):
            adopt_runtime_sessions(self.layout)
            channels = self.channel_supervisor()
            channels.start()
            try:
                # Without a graceful-shutdown deadline uvicorn waits forever
                # for open handlers — and a browser tab holding a `/tui`
                # websocket is one. `woltspace stop` then times out with the
                # process still alive and the port already released.
                serve_options = dict(
                    host=self.layout.host,
                    port=self.layout.port,
                    log_level=self.log_level,
                    timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
                )
                if self.reload:
                    uvicorn.run(
                        "server.app:app",
                        reload=True,
                        reload_dirs=[str(self.layout.install_root / "server")],
                        **serve_options,
                    )
                    return

                from server.app import app
                uvicorn.run(app, **serve_options)
            finally:
                channels.stop()
