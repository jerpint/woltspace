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

    Supervisor(layout, reload=args.reload, log_level=args.log_level).run()
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
    serve.set_defaults(func=_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 1
    return args.func(args)
