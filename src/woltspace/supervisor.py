"""One foreground control-plane entrypoint for native and container runs."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field

from .layout import RuntimeLayout
from .instance import DataRootLock
from .adoption import adopt_runtime_sessions
from .doctor import ensure_container_mounts
from .channels import connector_secrets, plan_connectors
from .channel_supervisor import ChannelSupervisor


@dataclass
class Supervisor:
    layout: RuntimeLayout
    reload: bool = False
    log_level: str = "info"
    instance_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def prepare(self) -> None:
        ensure_container_mounts(self.layout)
        self.layout.apply_environment()
        self.layout.platform_state.mkdir(parents=True, exist_ok=True)
        self.layout.logs_dir.mkdir(parents=True, exist_ok=True)
        os.environ["WOLTSPACE_INSTANCE_ID"] = self.instance_id
        if self.layout.isolation == "host":
            os.environ.setdefault("WOLTSPACE_PUBLIC_TUNNEL", "false")

    def channel_supervisor(self) -> ChannelSupervisor:
        """Plan connectors and share their resolved secrets with this process.

        `notify` and the settings page read the token from the environment; the
        connector config is the single place it is resolved, and it stays in
        memory — nothing here writes a credential to disk.
        """
        plans = plan_connectors(self.layout)
        for key, value in connector_secrets(plans).items():
            os.environ.setdefault(key, value)
        return ChannelSupervisor(self.layout, plans)

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
