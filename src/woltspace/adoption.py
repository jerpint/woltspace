"""Registry-led adoption across native control-plane restarts."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .layout import RuntimeLayout


def report_path(layout: RuntimeLayout) -> Path:
    return layout.platform_state / "adoption.json"


def import_lodge_state(layout: RuntimeLayout) -> dict:
    """Bring the old state files into lodge.db: {store: error} for the ones that failed.

    A failed import must not stop the lodge from booting: its sources are
    untouched, so the next boot tries again, and /health shows the error.
    """
    import lodge_db
    from sessions import import_sessions

    importers = {"sessions": import_sessions}
    errors = {}
    for store, importer in importers.items():
        try:
            for name, counts in lodge_db.run_imports(layout.wolts_dir, {store: importer}).items():
                print(f"[lodge-db] imported {name}: {counts}", file=sys.stderr)
        except Exception as exc:
            errors[store] = f"{type(exc).__name__}: {exc}"
            print(f"[lodge-db] IMPORT OF {store} FAILED, retried at next boot: "
                  f"{errors[store]}", file=sys.stderr)
    return errors


def adopt_runtime_sessions(layout: RuntimeLayout) -> dict:
    lib_dir = str(layout.install_root / "container" / "lib")
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    from sessions import SessionRegistry

    import_errors = import_lodge_state(layout)
    report = SessionRegistry(layout.wolts_dir).adopt_runtime_sessions()
    if import_errors:
        report["import_errors"] = import_errors
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
