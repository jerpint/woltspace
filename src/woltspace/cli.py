"""Installed Woltspace command line."""

from __future__ import annotations

import argparse
import json

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="woltspace")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")
    paths = sub.add_parser("paths", help="show resolved native runtime paths")
    paths.add_argument("--json", action="store_true")
    paths.set_defaults(func=_paths)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 1
    return args.func(args)
