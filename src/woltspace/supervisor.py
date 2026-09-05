"""One foreground control-plane entrypoint for native and container runs."""

from __future__ import annotations

import os
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
        if self.layout.is_entrypoint:
            return
        # A guest never publishes. The container exports
        # WOLTSPACE_PUBLIC_TUNNEL=true to everything it hosts, and the tunnel
        # lifecycle is process-wide: starting one here would race the real
        # instance's cloudflared, and stopping would delete its state file.
        if self.layout.isolation == "host":
            os.environ.setdefault("WOLTSPACE_PUBLIC_TUNNEL", "false")
        else:
            os.environ["WOLTSPACE_PUBLIC_TUNNEL"] = "false"

    def channel_supervisor(self) -> ChannelSupervisor:
        """Plan connectors and share their resolved secrets with this process.

        `notify` and the settings page read the token from the environment; the
        connector config is the single place it is resolved, and it stays in
        memory — nothing here writes a credential to disk.
        """
        plans = [self._refuse_busy_token(plan) for plan in plan_connectors(self.layout)]
        for key, value in connector_secrets(plans).items():
            os.environ.setdefault(key, value)
        return ChannelSupervisor(self.layout, plans)

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
        with DataRootLock(self.layout, self.instance_id):
            adopt_runtime_sessions(self.layout)
            channels = self.channel_supervisor()
            channels.start()
            try:
                if self.reload:
                    uvicorn.run(
                        "server.app:app",
                        host=self.layout.host,
                        port=self.layout.port,
                        reload=True,
                        reload_dirs=[str(self.layout.install_root / "server")],
                        log_level=self.log_level,
                    )
                    return

                from server.app import app
                uvicorn.run(
                    app,
                    host=self.layout.host,
                    port=self.layout.port,
                    log_level=self.log_level,
                )
            finally:
                channels.stop()
