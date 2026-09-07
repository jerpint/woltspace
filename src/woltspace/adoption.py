"""Registry-led adoption across native control-plane restarts."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .layout import RuntimeLayout


def report_path(layout: RuntimeLayout) -> Path:
    return layout.platform_state / "adoption.json"


def adopt_runtime_sessions(layout: RuntimeLayout) -> dict:
    lib_dir = str(layout.install_root / "container" / "lib")
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    from sessions import SessionRegistry

    report = SessionRegistry(layout.wolts_dir).adopt_runtime_sessions()
    path = report_path(layout)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, indent=2) + "\n")
    tmp.chmod(0o600)
    tmp.replace(path)
    return report


def read_adoption_report(layout: RuntimeLayout) -> dict:
    try:
        report = json.loads(report_path(layout).read_text())
        return report if isinstance(report, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
