"""Installed Woltspace command line."""

from __future__ import annotations

import argparse
import json
import os

from . import __version__, lore
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
            lore.field(key, value, value_style=lore.TEAL)
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
        # Same glyphs, same one-line-per-check shape — the ground read, not
        # rewritten. Moss for a clear path, amber for a warning, terra for a
        # blocked one.
        glyphs = {"pass": "✓", "warn": "!", "fail": "✗"}
        styles = {"pass": lore.MOSS, "warn": lore.AMBER, "fail": lore.TERRA}
        lore.headline(lore.TRACKS, "checking the lodge")
        lore.subtitle("reading the tracks")
        lore.blank()
        for check in checks:
            lore.plain(
                f"{glyphs[check.status]} {check.name}: {check.detail}",
                styles[check.status],
            )
            if check.remedy:
                lore.plain(f"  fix: {check.remedy}", lore.BARK)
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
    # The boot banner. `serve` is the lodge itself — in a native foreground run
    # and in the container's logs alike — so it opens the way the launcher
    # always opened, then hands the terminal over to uvicorn.
    lore.banner()
    lore.transition("waking", subtitle_override="lighting the lodge...")
    lore.blank()
    lore.link(layout.endpoint, note="the lodge is open")
    lore.blank()
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
        lore.failure(f"serve failed: {exc}")
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
        # `status` is the one surface tools read, so it is styled and not
        # restyled: the same lines, in the same order, with no emoji, no
        # indent, and nothing added. Only the colour is new — and `rich` drops
        # even that the moment stdout is not a terminal.
        state = result["state"]
        lore.field("state", state, value_style=_STATE_STYLES.get(state, ""))
        lore.field("endpoint", result["endpoint"], value_style=lore.TEAL)
        lore.field("wolts", result["wolts_dir"], value_style=lore.TEAL)
        owner = result.get("owner") or {}
        if owner:
            lore.field(
                "owner",
                f"pid {owner['pid']} · {owner['instance_id']} · {owner['hostname']}",
            )
        adoption = (result.get("health") or {}).get("adoption") or {}
        if adoption:
            lore.field(
                "adoption",
                f"{len(adoption.get('adopted', []))} live · "
                f"{len(adoption.get('orphaned', []))} orphaned · "
                f"{len(adoption.get('unchanged', []))} unchanged",
            )
        for line in format_connector_lines(result):
            lore.plain(line, _connector_line_style(line))
    return 0 if result["state"] in {"healthy", "stopped"} else 1


#: How each instance state reads at a glance: a lodge that is up is moss, a
#: lodge someone else owns or that broke is terra, everything else is amber.
_STATE_STYLES = {
    "healthy": lore.MOSS,
    "stopped": lore.BARK,
    "starting": lore.AMBER,
    "stale": lore.AMBER,
    "conflict": lore.TERRA,
}


def _connector_line_style(line: str) -> str:
    """Colour a connector line by what it says, without rewriting a word."""
    stripped = line.strip()
    if stripped.startswith(("error:", "fix:")):
        return lore.BARK
    if ": running" in line:
        return lore.MOSS
    if ": failed" in line or ": disabled" in line:
        return lore.TERRA
    return lore.AMBER


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


#: The lodge's own reading of what `lifecycle.start` reported. The detail
#: strings stay exactly as they were — the JSON payload is unchanged — and the
#: launcher's amber/moss pairing is put back on top of them.
_START_TRANSITIONS = {
    "started": "started",
    "already running": "running",
    "already starting; no second instance launched": "waking",
}


def _start(args) -> int:
    from .lifecycle import start, tunnel_report

    layout = RuntimeLayout.from_env(isolation="host")
    layout = RuntimeLayout(
        layout.wolts_dir, layout.install_root,
        args.host or layout.host, args.port or layout.port, "host",
    )
    code, result = start(layout, timeout=args.timeout)
    if code == 0:
        # The public address is configuration, not a log line: a named tunnel's
        # URL is in the data root's `.env` and can be named the moment the lodge
        # is up. Resolved before --json so scripts see it too.
        result["tunnel"] = tunnel_report(layout)
    if args.json:
        print(json.dumps(result, indent=2))
    elif code == 0:
        # Report where it is actually serving. An already-running instance may
        # own a different port than the one just asked for, and naming the
        # requested port sends you to a dead address.
        endpoint = result.get("endpoint") or layout.endpoint
        detail = result.get("detail", "running")
        lore.banner()
        key = _START_TRANSITIONS.get(detail)
        if key == "waking":
            lore.transition(key, subtitle_override="no second instance launched")
        elif key:
            lore.transition(key)
        else:
            lore.headline(lore.TENT, f"the lodge {detail}")
        lore.blank()
        lore.link(endpoint, note="the lodge is open")
        lore.public_tunnel_lines(result["tunnel"])
        lore.blank()
        lore.note(f"wolts: {layout.wolts_dir}")
        if result.get("log"):
            lore.note(f"logs: {result['log']}")
        if result.get("skills_sync_error"):
            lore.note(f"skills: not synced ({result['skills_sync_error']})")
        if result.get("hooks_normalize_error"):
            lore.note(f"hooks: not normalized ({result['hooks_normalize_error']})")
        lore.note("status: woltspace status")
        lore.blank()
    else:
        lore.banner()
        lore.failure(f"start failed: {result.get('error') or result.get('state')}")
        for check in result.get("checks", []):
            if check["status"] == "fail":
                lore.note(f"{check['name']}: {check['detail']}", emoji=lore.TIMBER)
                if check.get("remedy"):
                    lore.note(f"fix: {check['remedy']}", emoji=lore.TRACKS)
        lore.blank()
    return code


#: Same idea for `stop`, with the reassurance the detail carried kept as its
#: own line: "tmux sessions untouched" is the whole reason stop is safe to
#: type, and it must survive the restyling. Anything unmapped keeps its own
#: words as the headline.
_STOP_TRANSITIONS = {
    "control plane stopped; tmux sessions untouched":
        ("stopped", "tmux sessions untouched"),
    "already stopped; tmux sessions untouched":
        ("quiet", "tmux sessions untouched"),
}


def _stop(args) -> int:
    from .lifecycle import stop

    layout = RuntimeLayout.from_env(isolation="host")
    code, result = stop(layout, timeout=args.timeout)
    if args.json:
        print(json.dumps(result, indent=2))
        return code
    detail = result.get("detail")
    if not detail:
        lore.failure(f"stop failed: {result.get('error')}")
        return code
    mapped = _STOP_TRANSITIONS.get(detail)
    if mapped:
        key, reassurance = mapped
        lore.transition(key)
        lore.subtitle(reassurance)
    else:
        # A stale-metadata sweep, or anything else lifecycle grows later: the
        # moon still fits, and the detail is worth reading verbatim.
        lore.headline(lore.MOON, detail)
        lore.subtitle("lodge closed for the night")
    return code


def _backup(args) -> int:
    from .backup import create_backup, summary_lines

    layout = RuntimeLayout.from_env()
    wolts_dir = args.wolts_dir or layout.wolts_dir
    try:
        result = create_backup(wolts_dir, out_dir=args.out or None, tag=args.tag or None)
    except (FileNotFoundError, OSError, ValueError) as exc:
        lore.failure(f"backup failed: {exc}")
        return 1
    if args.json:
        print(json.dumps({
            "archive": str(result.archive),
            "verified": result.verified,
            "manifest": result.manifest,
        }, indent=2))
    else:
        lore.headline(lore.ELEPHANT, f"snapshot tag: {result.manifest['tag']}")
        lore.subtitle("backing up the lodge")
        lore.blank()
        for line in summary_lines(result):
            lore.plain(line, _report_line_style(line))
        lore.blank()
    return 0


def _restore(args) -> int:
    from .backup import restore_backup, restore_lines

    try:
        result = restore_backup(args.archive, to=args.to or None)
    except (FileNotFoundError, FileExistsError, OSError, ValueError) as exc:
        lore.failure(f"restore failed: {exc}")
        return 1
    if args.json:
        print(json.dumps({
            "target": str(result.target),
            "wolts_dir": str(result.wolts_dir),
            "entries": result.entries,
            "manifest": result.manifest,
        }, indent=2))
    else:
        lore.headline(lore.ELEPHANT, f"snapshot tag: {result.manifest['tag']}")
        lore.subtitle("the lodge remembers")
        lore.blank()
        for line in restore_lines(result):
            lore.plain(line, _report_line_style(line))
        lore.blank()
    return 0


def _report_line_style(line: str) -> str:
    """Colour a backup or restore report line by what kind of line it is.

    The lines themselves come from `backup.summary_lines` / `restore_lines`
    unchanged: those strings are the report, and they are asserted on by name
    in the tests. This only decides what colour each one is painted.
    """
    stripped = line.strip()
    if not stripped:
        return ""
    if stripped.startswith(("warnings:", "withheld:")):
        return lore.TERRA
    if stripped.startswith(("verified:", "restored:", "backup:")):
        return lore.MOSS
    if stripped.startswith(("wolt ", "WOLTS_DIR=", "archive:", "data:")):
        return lore.BARK
    if stripped.startswith(("tag:", "source:", "woltspace ", "wolts:")):
        return lore.AMBER
    return lore.BARK


def _tui(args) -> int:
    from .tui import TuiResolutionError, launch_tui, resolve_tui

    try:
        resolution = resolve_tui()
    except TuiResolutionError as exc:
        lore.failure(f"tui failed: {exc}")
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
    parser = argparse.ArgumentParser(
        prog="woltspace",
        description=f"{lore.BEAVER} woltspace - a lodge for your wolts",
        epilog=(
            '"how much wolt could a wolt chuck chuck\n'
            ' if a wolt chuck could chuck wolt?"\n\n'
            "all state lives under ~/.woltspace/wolts (override with WOLTS_DIR)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
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
        # A bare `woltspace` gets the wordmark before the usage, the way the
        # launcher always greeted an empty command line.
        lore.banner()
        parser.print_help()
        return 1
    return args.func(args)
