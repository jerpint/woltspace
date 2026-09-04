"""One foreground control-plane entrypoint for native and container runs."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field

from .layout import RuntimeLayout
from .instance import DataRootLock


@dataclass
class Supervisor:
    layout: RuntimeLayout
    reload: bool = False
    log_level: str = "info"
    instance_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def prepare(self) -> None:
        self.layout.apply_environment()
        self.layout.platform_state.mkdir(parents=True, exist_ok=True)
        self.layout.logs_dir.mkdir(parents=True, exist_ok=True)
        os.environ["WOLTSPACE_INSTANCE_ID"] = self.instance_id
        if self.layout.isolation == "host":
            os.environ.setdefault("WOLTSPACE_PUBLIC_TUNNEL", "false")

    def run(self) -> None:
        self.prepare()
        import uvicorn
        with DataRootLock(self.layout, self.instance_id):
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
