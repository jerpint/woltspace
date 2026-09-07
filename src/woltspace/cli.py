"""Installed Woltspace command line."""

from __future__ import annotations

import argparse
import json
import os

from . import __version__
from .layout import RuntimeLayout


def _paths(args) -> int:
    layout = RuntimeLayout.from_env()
    payload = {
        "wolts_dir": str(layout.wolts_dir),
        "state_root": str(layout.state_root),
        "install_root": str(layout.install_root),
        "endpoint": layout.endpoint,
        "isolation": layout.isolation,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        for key, value in payload.items():
            print(f"{key}: {value}")
    return 0


def _doctor(args) -> int:
    from .doctor import doctor_ok, run_doctor

    layout = RuntimeLayout.from_env(isolation=args.isolation)
    if args.host:
        layout = RuntimeLayout(
            layout.wolts_dir, layout.install_root, args.host, args.port or layout.port,
            layout.isolation,
        )
    elif args.port:
        layout = RuntimeLayout(
            layout.wolts_dir, layout.install_root, layout.host, args.port,
            layout.isolation,
        )
    checks = run_doctor(layout, check_port=not args.no_port)
    if args.json:
        print(json.dumps({
            "ok": doctor_ok(checks),
            "checks": [check.to_record() for check in checks],
        }, indent=2))
    else:
        glyphs = {"pass": "✓", "warn": "!", "fail": "✗"}
        for check in checks:
            print(f"{glyphs[check.status]} {check.name}: {check.detail}")
            if check.remedy:
                print(f"  fix: {check.remedy}")
    return 0 if doctor_ok(checks) else 1


def serve(
    *, host: str = "", port: int = 0, isolation: str = "", reload: bool = False,
    no_doctor: bool = False, log_level: str = "info", instance_id: str = "",
) -> int:
    """Run the control plane in this process.

    Shared by the `serve` subcommand and `container-entrypoint`: the container
    runs the supervisor in-process rather than exec'ing a second CLI, so both
    reach it through the same call instead of a shell command line.
    """
    isolation = isolation or os.environ.get("WOLTSPACE_ISOLATION", "host")
    layout = RuntimeLayout.from_env(isolation=isolation)
    layout = RuntimeLayout(
        layout.wolts_dir,
        layout.install_root,
        host or layout.host,
        port or layout.port,
        layout.isolation,
    )
    if not no_doctor and _doctor(argparse.Namespace(
        isolation=layout.isolation,
        host=layout.host,
        port=layout.port,
        no_port=False,
        json=False,
    )):
        return 1
    from .supervisor import Supervisor
    from .instance import InstanceConflict
    from .doctor import DataRootConflict, MountError

    supervisor = Supervisor(
        layout,
        reload=reload,
        log_level=log_level,
        **({"instance_id": instance_id} if instance_id else {}),
    )
    try:
        supervisor.run()
    except (InstanceConflict, MountError, DataRootConflict) as exc:
        print(f"serve failed: {exc}")
        return 1
    return 0


def _serve(args) -> int:
    return serve(
        host=args.host, port=args.port, isolation=args.isolation,
        reload=args.reload, no_doctor=args.no_doctor,
        log_level=args.log_level, instance_id=args.instance_id,
    )


def _container_entrypoint(args) -> int:
    from .container_entrypoint import main as boot

    return boot()


def _status(args) -> int:
    from .instance import inspect_instance

    layout = RuntimeLayout.from_env(isolation=args.isolation)
    result = inspect_instance(layout)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"state: {result['state']}")
        print(f"endpoint: {result['endpoint']}")
        print(f"wolts: {result['wolts_dir']}")
        owner = result.get("owner") or {}
        if owner:
            print(f"owner: pid {owner['pid']} · {owner['instance_id']} · {owner['hostname']}")
        adoption = (result.get("health") or {}).get("adoption") or {}
        if adoption:
            print(
                "adoption: "
                f"{len(adoption.get('adopted', []))} live · "
                f"{len(adoption.get('orphaned', []))} orphaned · "
                f"{len(adoption.get('unchanged', []))} unchanged"
            )
        for line in format_connector_lines(result):
            print(line)
    return 0 if result["state"] in {"healthy", "stopped"} else 1


def format_connector_lines(result: dict) -> list[str]:
    """One line per channel connector, with the remedy when it is not running."""
    connectors = (result.get("health") or {}).get("connectors")
    if connectors is None:
        connectors = result.get("connectors") or []
    lines = []
    for connector in connectors:
        state = connector.get("state", "unknown")
        detail = connector.get("detail") or ""
        suffix = f" · {detail}" if detail else ""
        pid = connector.get("pid")
        if pid:
            suffix += f" · pid {pid}"
        restarts = connector.get("restarts") or 0
        if restarts:
            suffix += f" · {restarts} restart(s)"
        lines.append(f"connector {connector.get('name', '?')}: {state}{suffix}")
        error = connector.get("error")
        if error:
            lines.append(f"  error: {error}")
        if state in {"disabled", "failed"} and connector.get("remedy"):
            lines.append(f"  fix: {connector['remedy']}")
    return lines


def _start(args) -> int:
    from .lifecycle import start

    layout = RuntimeLayout.from_env(isolation="host")
    layout = RuntimeLayout(
        layout.wolts_dir, layout.install_root,
        args.host or layout.host, args.port or layout.port, "host",
    )
    code, result = start(layout, timeout=args.timeout)
    if args.json:
        print(json.dumps(result, indent=2))
    elif code == 0:
        # Report where it is actually serving. An already-running instance may
        # own a different port than the one just asked for, and naming the
        # requested port sends you to a dead address.
        endpoint = result.get("endpoint") or layout.endpoint
        print(f"woltspace {result.get('detail', 'running')}: {endpoint}")
        print(f"wolts: {layout.wolts_dir}")
        if result.get("log"):
            print(f"logs: {result['log']}")
        if result.get("skills_sync_error"):
            print(f"skills: not synced ({result['skills_sync_error']})")
        if result.get("hooks_normalize_error"):
            print(f"hooks: not normalized ({result['hooks_normalize_error']})")
        print("status: woltspace status")
    else:
        print(f"start failed: {result.get('error') or result.get('state')}")
        for check in result.get("checks", []):
            if check["status"] == "fail":
                print(f"  {check['name']}: {check['detail']}")
                if check.get("remedy"):
                    print(f"  fix: {check['remedy']}")
    return code


def _stop(args) -> int:
    from .lifecycle import stop

    layout = RuntimeLayout.from_env(isolation="host")
    code, result = stop(layout, timeout=args.timeout)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(result.get("detail") or f"stop failed: {result.get('error')}")
    return code


def _backup(args) -> int:
    from .backup import create_backup, summary_lines

    layout = RuntimeLayout.from_env()
    wolts_dir = args.wolts_dir or layout.wolts_dir
    try:
        result = create_backup(wolts_dir, out_dir=args.out or None, tag=args.tag or None)
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(f"backup failed: {exc}")
        return 1
    if args.json:
        print(json.dumps({
            "archive": str(result.archive),
            "verified": result.verified,
            "manifest": result.manifest,
        }, indent=2))
    else:
        for line in summary_lines(result):
            print(line)
    return 0


def _restore(args) -> int:
    from .backup import restore_backup, restore_lines

    try:
        result = restore_backup(args.archive, to=args.to or None)
    except (FileNotFoundError, FileExistsError, OSError, ValueError) as exc:
        print(f"restore failed: {exc}")
        return 1
    if args.json:
        print(json.dumps({
            "target": str(result.target),
            "wolts_dir": str(result.wolts_dir),
            "entries": result.entries,
            "manifest": result.manifest,
        }, indent=2))
    else:
        for line in restore_lines(result):
            print(line)
    return 0


def _tui(args) -> int:
    from .tui import TuiResolutionError, launch_tui, resolve_tui

    try:
        resolution = resolve_tui()
    except TuiResolutionError as exc:
        print(f"tui failed: {exc}")
        return 1
    forwarded = list(args.tui_args)
    if forwarded[:1] == ["--"]:
        forwarded = forwarded[1:]
    if args.dry_run:
        from .tui import fallback_notices

        record = resolution.to_record()
        record["command"] = [*resolution.command, *forwarded]
        record["notices"] = fallback_notices(resolution)
        if args.json:
            print(json.dumps(record, indent=2))
        else:
            print(f"source: {record['source']}")
            print(f"package: {record['package']}@{record['version']}")
            print(f"command: {' '.join(record['command'])}")
            for notice in record["notices"]:
                print(notice)
        return 0
    launch_tui(resolution, forwarded)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="woltspace")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")
    paths = sub.add_parser("paths", help="show resolved native runtime paths")
    paths.add_argument("--json", action="store_true")
    paths.set_defaults(func=_paths)

    doctor = sub.add_parser("doctor", help="check native runtime prerequisites")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--host", default="")
    doctor.add_argument("--port", type=int, default=0)
    doctor.add_argument("--isolation", choices=("host", "external"), default="host")
    doctor.add_argument("--no-port", action="store_true", help=argparse.SUPPRESS)
    doctor.set_defaults(func=_doctor)

    serve = sub.add_parser("serve", help="run the control plane in the foreground")
    serve.add_argument("--host", default="")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--isolation", choices=("host", "external"), default="")
    serve.add_argument("--reload", action="store_true")
    serve.add_argument("--no-doctor", action="store_true")
    serve.add_argument("--log-level", default="info")
    serve.add_argument("--instance-id", default="", help=argparse.SUPPRESS)
    serve.set_defaults(func=_serve)

    start = sub.add_parser("start", help="start the native control plane")
    start.add_argument("--host", default="")
    start.add_argument("--port", type=int, default=0)
    start.add_argument("--timeout", type=float, default=15.0)
    start.add_argument("--json", action="store_true")
    start.set_defaults(func=_start)

    status = sub.add_parser("status", help="inspect native control-plane ownership")
    status.add_argument("--json", action="store_true")
    status.add_argument("--isolation", choices=("host", "external"), default="host")
    status.set_defaults(func=_status)

    stop = sub.add_parser("stop", help="stop only the native control plane")
    stop.add_argument("--timeout", type=float, default=10.0)
    stop.add_argument("--json", action="store_true")
    stop.set_defaults(func=_stop)

    entrypoint = sub.add_parser(
        "container-entrypoint",
        help="container boot — root phase + setup + serve; not for interactive use",
    )
    entrypoint.set_defaults(func=_container_entrypoint)

    backup = sub.add_parser("backup", help="archive the wolts directory — data only")
    backup.add_argument("--tag", default="", help="archive tag (default: UTC timestamp)")
    backup.add_argument("--out", default="", help="where to write it (default: beside the wolts dir)")
    backup.add_argument("--wolts-dir", default="", help=argparse.SUPPRESS)
    backup.add_argument("--json", action="store_true")
    backup.set_defaults(func=_backup)

    restore = sub.add_parser("restore", help="extract a backup archive into a new directory")
    restore.add_argument("archive")
    restore.add_argument("--to", default="", help="target directory (must be new or empty)")
    restore.add_argument("--json", action="store_true")
    restore.set_defaults(func=_restore)

    tui = sub.add_parser("tui", help="open the exactly compatible terminal UI")
    tui.add_argument("--dry-run", action="store_true", help="show resolution without launching")
    tui.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    tui.add_argument("tui_args", nargs=argparse.REMAINDER)
    tui.set_defaults(func=_tui)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 1
    return args.func(args)
