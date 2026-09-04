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


def _serve(args) -> int:
    isolation = args.isolation or os.environ.get("WOLTSPACE_ISOLATION", "host")
    layout = RuntimeLayout.from_env(isolation=isolation)
    layout = RuntimeLayout(
        layout.wolts_dir,
        layout.install_root,
        args.host or layout.host,
        args.port or layout.port,
        layout.isolation,
    )
    if not args.no_doctor and _doctor(argparse.Namespace(
        isolation=layout.isolation,
        host=layout.host,
        port=layout.port,
        no_port=False,
        json=False,
    )):
        return 1
    from .supervisor import Supervisor
    from .instance import InstanceConflict

    supervisor = Supervisor(
        layout,
        reload=args.reload,
        log_level=args.log_level,
        **({"instance_id": args.instance_id} if args.instance_id else {}),
    )
    try:
        supervisor.run()
    except InstanceConflict as exc:
        print(f"serve failed: {exc}")
        return 1
    return 0


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
    return 0 if result["state"] in {"healthy", "stopped"} else 1


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
        print(f"woltspace {result.get('detail', 'running')}: {layout.endpoint}")
        print(f"wolts: {layout.wolts_dir}")
        if result.get("log"):
            print(f"logs: {result['log']}")
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
        record = resolution.to_record()
        if args.json:
            print(json.dumps(record, indent=2))
        else:
            print(f"source: {record['source']}")
            print(f"package: {record['package']}@{record['version']}")
            print(f"command: {' '.join(record['command'])}")
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
