"""Installed Woltspace command line."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__, lore
from .envvars import get_env, warn_legacy_once
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


def _update(args) -> int:
    from .update import update

    return update(args)


def _status(args) -> int:
    from .instance import inspect_instance
    from .lifecycle import tunnel_report

    layout = RuntimeLayout.from_env(isolation=args.isolation)
    result = inspect_instance(layout)
    # `status` answers instantly. It reads the same configuration and the same
    # state file `start` reads — one resolution, one shape in `--json` — but
    # with `wait=False`: a question about right now must never sit for eight
    # seconds hoping cloudflared turns up.
    result["tunnel"] = tunnel_report(layout, wait=False)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        # The human read: a creature and a lore line for the state, then every
        # fact the plain CLI printed, each keeping its own `key: value` shape
        # so it stays greppable. `--json` is the machine contract.
        state = result["state"]
        emoji, said = lore.STATUS_LORE.get(state, lore.DEFAULT_STATUS_LORE)
        lore.headline(emoji, said)
        # The state word keeps its `state: …` shape at column 0 — coloured by
        # what it means rather than always moss, because a conflict painted
        # green would be a lie, but not moved: `grep '^state:'` and
        # `awk -F': ' '/^state/{print $2}'` are how this line is read.
        lore.field("state", state, value_style=_STATE_STYLES.get(state, lore.MOSS))
        lore.blank()
        # Same for the address. It was briefly a bare URL beside a beaver,
        # which reads beautifully and cannot be grepped for by name — and the
        # label is the only thing that says what the URL *is*.
        lore.field("endpoint", result["endpoint"], value_style=lore.TEAL)
        lore.status_tunnel_line(result["tunnel"])
        lore.note(f"wolts: {result['wolts_dir']}")
        owner = result.get("owner") or {}
        if owner:
            # Hostname only. A pid and a 32-character instance id are
            # diagnostics — an agent reads them out of `--json`; a human
            # reading a terminal wants to know *which machine* holds the lodge,
            # which is the part that matters in a colony spanning more than one.
            lore.note(f"owner: {owner['hostname']}")
        adoption = (result.get("health") or {}).get("adoption") or {}
        if adoption:
            lore.note(
                "adoption: "
                f"{len(adoption.get('adopted', []))} live · "
                f"{len(adoption.get('orphaned', []))} orphaned · "
                f"{len(adoption.get('unchanged', []))} unchanged",
                emoji=lore.TRACKS,
            )
        connector_lines = format_connector_lines(result, show_pid=False)
        if connector_lines:
            lore.blank()
        for line in connector_lines:
            _print_connector_line(line)
    return 0 if result["state"] in {"healthy", "stopped"} else 1


def _print_connector_line(line: str) -> None:
    """A connector's own line, with the creature that does its job in front.

    Continuation lines (`error:`, `fix:`) stay indented under their connector
    rather than collecting a creature of their own.
    """
    stripped = line.strip()
    if line.startswith("  "):
        lore.plain(f"{lore.SUBINDENT}{stripped}", lore.BARK)
        return
    name = stripped.split(":", 1)[0].removeprefix("connector ").strip()
    lore.note(
        stripped,
        emoji=lore.CONNECTOR_CREATURES.get(name, lore.TIMBER),
        style=_connector_line_style(line),
    )


#: How each instance state reads at a glance: a lodge that is up is moss, a
#: lodge someone else owns or that broke is terra, everything else is amber.
_STATE_STYLES = {
    "healthy": lore.MOSS,
    "stopped": lore.BARK,
    "starting": lore.AMBER,
    "stale": lore.AMBER,
    "conflict": lore.TERRA,
}


def _session(args) -> int:
    """Delegate the whole session noun to the bundled control client."""
    layout = RuntimeLayout.from_env()
    client = layout.install_root / "container" / "bin" / "woltspace"
    if not client.is_file():
        print(f"bundled session client not found: {client}", file=sys.stderr)
        return 2
    os.environ.setdefault("WOLTSPACE_API", layout.endpoint)
    os.execv(str(client), [str(client), "session", *args.session_args])
    return 0


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


def format_connector_lines(result: dict, *, show_pid: bool = True) -> list[str]:
    """One line per channel connector, with the remedy when it is not running.

    `show_pid=False` is the human render: a pid is a diagnostic, not something
    anyone reads off a terminal and uses. It stays in `--json`, where the
    agents and scripts that actually need it look.
    """
    connectors = (result.get("health") or {}).get("connectors")
    if connectors is None:
        connectors = result.get("connectors") or []
    lines = []
    for connector in connectors:
        state = connector.get("state", "unknown")
        detail = connector.get("detail") or ""
        suffix = f" · {detail}" if detail else ""
        pid = connector.get("pid")
        if pid and show_pid:
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
    recovery = (result.get("health") or {}).get("recovery") or result.get("recovery")
    if isinstance(recovery, dict) and recovery.get("ran") and not recovery.get("ok"):
        owed = recovery.get("owed") or []
        lines.append(
            "connector recovery: " + (f"{len(owed)} revocation(s) still owed" if owed else "failing")
        )
        for error in recovery.get("errors") or []:
            lines.append(f"  error: {error}")
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
        if result.get("platform_docs_sync_error"):
            lore.note(f"instructions: not synced ({result['platform_docs_sync_error']})")
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
    if stripped.startswith(("wolt ", "WOLTSPACE_WOLTS_DIR=", "WOLTS_DIR=",
                            "archive:", "data:")):
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
            label = (
                f"{record['package']}@{record['version']}"
                if record.get("version") else record["spec"]
            )
            print(f"package: {label}")
            print(f"command: {' '.join(record['command'])}")
            for notice in record["notices"]:
                print(notice)
        return 0
    launch_tui(resolution, forwarded)
    return 0


def _auto(args) -> int:
    """A bare `woltspace auto` names its own verbs, not the whole CLI."""
    args.auto_parser.print_help()
    return 1


def _access_module(layout: RuntimeLayout):
    """The lodge's own Access settings code: one validator, one atomic writer."""
    root = str(layout.install_root)
    if root not in sys.path:
        sys.path.insert(0, root)
    from server import access

    return access


def _access(args) -> int:
    """A bare `woltspace access` names its own verbs."""
    args.access_parser.print_help()
    return 1


def _access_show(args) -> int:
    layout = RuntimeLayout.from_env()
    try:
        settings = _access_module(layout).load_access_settings(layout.wolts_dir)
    except RuntimeError as exc:
        print(f"woltspace access: {exc}", file=sys.stderr)
        return 1
    payload = _access_module(layout).access_settings_dict(settings)
    if args.json:
        print(json.dumps({"access": payload}, indent=2))
    elif payload is None:
        print("No lodge-side Access settings: remote callers are trusted to whatever "
              "guards the way in (e.g. Cloudflare Access at the edge).")
    else:
        for key, value in payload.items():
            lore.field(key, value or "(none)", value_style=lore.TEAL)
    return 0


def _access_set(args) -> int:
    """Write the lodge's Access settings from this machine.

    Works with the lodge stopped, over SSH, and without a browser, so it is
    also the way back when the saved settings lock the owner out. The values are not secrets; they
    come from the Cloudflare Zero Trust dashboard (see the cloudflare skill).
    """
    layout = RuntimeLayout.from_env()
    access = _access_module(layout)
    apps_aud = args.apps_aud
    if apps_aud is None:
        # Omitted keeps the app domain's audience; `--apps-aud ""` clears it.
        try:
            current = access.load_access_settings(layout.wolts_dir)
        except RuntimeError:
            current = None
        apps_aud = current.apps_aud if current else ""
        team = args.team_domain.strip().lower().rstrip(".")
        if current and current.apps_aud and team != current.team_domain:
            print(
                "woltspace access: the team domain changes, so the saved apps audience "
                "may no longer apply. Pass --apps-aud <tag> to keep apps reachable, "
                'or --apps-aud "" to turn remote apps off.',
                file=sys.stderr,
            )
            return 1
    raw = {
        "team_domain": args.team_domain,
        "lodge_aud": args.lodge_aud,
        "apps_aud": apps_aud,
        "owner_email": args.owner_email,
    }
    try:
        settings = access.save_access_settings(layout.wolts_dir, raw)
    except (ValueError, RuntimeError) as exc:
        print(f"woltspace access: {exc}", file=sys.stderr)
        return 1
    print(f"Saved. Remote requests now need an Access token for {settings.owner_email}.")
    return 0


def _access_clear(args) -> int:
    layout = RuntimeLayout.from_env()
    try:
        _access_module(layout).save_access_settings(layout.wolts_dir, None)
    except RuntimeError as exc:
        print(f"woltspace access: {exc}", file=sys.stderr)
        return 1
    print("Cleared. The lodge no longer verifies remote requests itself.")
    return 0


def _auto_runtime():
    """Borrow the runtime's own grant store and target resolver.

    The grant file's shape, its lock, and the canonicalization rule that makes
    a grant match a spawn all live in `container/lib`, and a second
    implementation here would be a second answer to "is this directory
    approved?". Same sys.path graft `adoption.py` uses to reach the registry.
    """
    layout = RuntimeLayout.from_env()
    lib_dir = str(layout.runtime_lib)
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    from execution_policy import AutoGrantStore
    from session_targets import SessionTarget

    return layout, AutoGrantStore(layout.wolts_dir), SessionTarget


def _auto_target(args):
    """Resolve the exact wolt and directory a grant is keyed by.

    `--workdir` is optional and defaults to the wolt's own home — the same
    default `SessionTarget.resolve` applies to a spawn that names no
    directory, so `woltspace auto grant <wolt>` approves precisely the
    sessions the lodge starts for that wolt with no workdir of their own.
    """
    layout, store, target_cls = _auto_runtime()
    target = target_cls.resolve(
        args.wolt, getattr(args, "workdir", "") or None, wolts_dir=layout.wolts_dir
    )
    return store, target


def _auto_grant(args) -> int:
    try:
        store, target = _auto_target(args)
    except ValueError as exc:
        lore.failure(f"auto grant failed: {exc}")
        return 1
    grant = store.grant(target)
    if args.json:
        print(json.dumps({"ok": True, "grant": grant.to_record()}, indent=2))
        return 0
    lore.headline(lore.SUN, f"auto approved: {target.wolt_id}")
    lore.subtitle("it works here without asking")
    lore.labelled("workdir", str(target.canonical_workdir))
    return 0


def _auto_revoke(args) -> int:
    try:
        store, target = _auto_target(args)
    except ValueError as exc:
        lore.failure(f"auto revoke failed: {exc}")
        return 1
    revoked = store.revoke(target)
    if args.json:
        print(json.dumps(
            {"ok": True, "revoked": revoked, "target": target.to_record()}, indent=2
        ))
        return 0
    lore.headline(
        lore.MOON,
        f"auto {'revoked' if revoked else 'was not granted'}: {target.wolt_id}",
    )
    lore.subtitle("it asks again from here on")
    lore.labelled("workdir", str(target.canonical_workdir))
    return 0


def _auto_list(args) -> int:
    _, store, _ = _auto_runtime()
    grants = sorted(
        store.list(), key=lambda g: (g.wolt_id, str(g.canonical_workdir))
    )
    if args.json:
        print(json.dumps({"grants": [g.to_record() for g in grants]}, indent=2))
        return 0
    if not grants:
        lore.headline(lore.TRACKS, "no auto grants")
        lore.subtitle("every native session asks before it acts")
        return 0
    lore.headline(lore.TRACKS, f"auto grants: {len(grants)}")
    lore.subtitle("these wolts work unattended in these directories")
    for grant in grants:
        lore.labelled(grant.wolt_id, str(grant.canonical_workdir))
    return 0


def _seed(args) -> int:
    args.seed_parser.print_help()
    return 1


def _dig(args) -> int:
    args.dig_parser.print_help()
    return 1


def _dig_store():
    from .dig import DigStore

    return DigStore(RuntimeLayout.from_env().state_root)


def _dig_grant(args) -> int:
    from .dig import DigError, resolve_ssh

    layout = RuntimeLayout.from_env()
    if not (layout.wolts_dir / args.wolt).is_dir():
        lore.failure(f"dig grant failed: unknown wolt: {args.wolt}")
        return 1
    try:
        resolved = resolve_ssh(args.destination)
        grant = _dig_store().grant(
            name=args.name,
            wolt=args.wolt,
            destination=args.destination,
            resolved=resolved,
            bootstrap_dir=args.bootstrap_dir,
        )
    except DigError as exc:
        lore.failure(f"dig grant failed: {exc}")
        return 1
    if args.json:
        print(json.dumps({"ok": True, "grant": grant}, indent=2))
    else:
        lore.headline(lore.TRACKS, f"dig approved: {grant['name']}")
        lore.labelled("wolt", grant["wolt"])
        lore.labelled("ssh", grant["destination"])
        lore.labelled("resolved", f"{resolved.user}@{resolved.hostname}:{resolved.port}")
        lore.labelled("handoff", grant["bootstrap_dir"])
        lore.subtitle("this records consent; the SSH user's real permissions still apply")
    return 0


def _dig_list(args) -> int:
    grants = sorted(_dig_store().list(), key=lambda item: item["name"])
    if args.json:
        print(json.dumps({"grants": grants}, indent=2))
    elif not grants:
        lore.headline(lore.TRACKS, "no approved digs")
    else:
        lore.headline(lore.TRACKS, f"approved digs: {len(grants)}")
        for grant in grants:
            lore.labelled(grant["name"], f"{grant['wolt']} -> {grant['destination']}")
    return 0


def _dig_revoke(args) -> int:
    from .dig import DigError

    try:
        revoked = _dig_store().revoke(args.name)
    except DigError as exc:
        lore.failure(f"dig revoke failed: {exc}")
        return 1
    if args.json:
        print(json.dumps({"ok": True, "revoked": revoked, "name": args.name}, indent=2))
    else:
        lore.headline(lore.MOON, f"dig {'revoked' if revoked else 'was not approved'}: {args.name}")
        lore.subtitle("remove the SSH key/config separately if access itself must end")
    return 0


def _dig_connect(args) -> int:
    from .dig import DigError, connect

    try:
        grant = _dig_store().get(args.name)
        if not args.json:
            lore.headline(lore.TRACKS, f"digging: {args.name}")
            lore.labelled("wolt", grant["wolt"])
            lore.labelled("handoff", grant["bootstrap_dir"])
        remote_command = list(args.remote_command)
        if remote_command[:1] == ["--"]:
            remote_command = remote_command[1:]
        return connect(
            _dig_store(),
            args.name,
            command=remote_command,
            actor_wolt=get_env("WOLTSPACE_WOLT_NAME", ""),
        )
    except DigError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        else:
            lore.failure(f"dig failed: {exc}")
        return 1


def _seed_create(args) -> int:
    from .seed import SeedError, create_seed

    layout = RuntimeLayout.from_env()
    try:
        summary = create_seed(
            wolts_dir=layout.wolts_dir,
            output=args.output,
            name=args.name,
            wolt_names=args.wolt,
            app_names=args.app,
            skills=args.skill,
        )
    except SeedError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        else:
            lore.failure(f"colony seed creation failed: {exc}")
        return 1
    payload = {"ok": True, **summary.to_record()}
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        lore.headline(lore.TRACKS, f"colony seed ready: {summary.name}")
        lore.labelled("path", str(summary.root))
        lore.labelled("wolts", ", ".join(summary.wolts))
        lore.labelled("apps", ", ".join(summary.apps) or "none")
        lore.labelled("size", f"{summary.bytes:,} bytes in {summary.files} files")
    return 0


def _seed_inspect(args) -> int:
    from .seed import SeedError, inspect_seed

    try:
        summary = inspect_seed(args.source)
    except SeedError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        else:
            lore.failure(f"colony seed is invalid: {exc}")
        return 1
    payload = {"ok": True, **summary.to_record()}
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        lore.headline(lore.TRACKS, f"colony seed: {summary.name}")
        lore.labelled("wolts", ", ".join(summary.wolts))
        lore.labelled("apps", ", ".join(summary.apps) or "none")
        lore.labelled("size", f"{summary.bytes:,} bytes in {summary.files} files")
        lore.labelled("sha256", summary.digest)
    return 0


def _seed_install(args) -> int:
    from .seed import SeedError, install_seed

    layout = RuntimeLayout.from_env()
    try:
        result = install_seed(
            source=args.source,
            wolts_dir=layout.wolts_dir,
            install_root=layout.install_root,
        )
    except SeedError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        else:
            lore.failure(f"colony seed install failed: {exc}")
        return 1
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        lore.headline(lore.SUN, f"colony seed installed: {result['seed']}")
        lore.labelled("wolts", ", ".join(result["wolts"]))
        lore.labelled("apps", ", ".join(result["apps"]) or "none")
        lore.subtitle("fresh independent copies; lived memory starts here")
    return 0


# --- wolf --------------------------------------------------------------------
# A thin client of the lodge's /wolf/crons API: every rule (validation, naming,
# locking) lives server-side, so a hand edit, the CLI and the lodge page all
# land on the same wolf.json the same way. Times are shown as the lodge gives
# them — its own local time, which is when the wolf fires.

WOLF = "🐺"


def _wolf_api() -> str:
    api = (os.environ.get("WOLTSPACE_API") or "").strip().rstrip("/")
    return api or RuntimeLayout.from_env().endpoint.rstrip("/")


def _wolf_request(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    """(status, json) from the lodge. An unreachable lodge exits 2."""
    import urllib.error
    import urllib.request

    url = f"{_wolf_api()}{path}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode() or "{}")
        except ValueError:
            return exc.code, {"error": str(exc)}
    except urllib.error.URLError as exc:
        lore.failure(f"cannot reach the lodge at {_wolf_api()}: {exc.reason}",
                     subtitle_text="is it running? woltspace status")
        raise SystemExit(2)


def _wolf_time(iso: str | None) -> str:
    """`2026-03-22T06:00:00-04:00` → `2026-03-22 06:00`, the lodge's wall clock."""
    if not iso:
        return "-"
    return iso[:16].replace("T", " ")


def _wolf_done(status: int, body: dict, as_json: bool) -> bool:
    """Print a failed call and say so; a success is left to the caller."""
    if status < 400:
        return False
    if as_json:
        print(json.dumps(body, indent=2))
    else:
        field = f" ({body['field']})" if body.get("field") else ""
        lore.failure(f"{body.get('error', f'HTTP {status}')}{field}", subtitle_text="")
    return True


def _wolf_message(args, *, required: bool) -> str | None:
    """The prompt: `--message TEXT`, `--message -` or piped stdin (a heredoc)."""
    if args.message not in (None, "-"):
        return args.message
    if args.message == "-" or (required and not sys.stdin.isatty()):
        return sys.stdin.read().strip()
    return None


def _wolf_line(cron: dict) -> None:
    when = cron.get("schedule") or f"at {cron.get('at', '')}"
    lore.labelled(
        f"{cron['wolt']}/{cron['name']}",
        f"{when}  ·  next {_wolf_time(cron.get('next_run'))}"
        f"  ·  last {_wolf_time(cron.get('last_run'))}",
    )
    if cron.get("error"):
        lore.plain(f"{lore.SUBINDENT}{cron['error']}", lore.TERRA)
    prompt = " ".join((cron.get("prompt") or "").split())
    lore.plain(f"{lore.SUBINDENT}{prompt[:72]}{'…' if len(prompt) > 72 else ''}", lore.BARK)


def _wolf(args) -> int:
    args.wolf_parser.print_help()
    return 1


def _wolf_list(args) -> int:
    status, body = _wolf_request("GET", "/wolf/crons")
    if _wolf_done(status, body, args.json):
        return 1
    crons = [c for c in body.get("crons", []) if not args.wolt or c["wolt"] == args.wolt]
    if args.json:
        print(json.dumps({"tz": body.get("tz"), "crons": crons}, indent=2))
        return 0
    if not crons:
        lore.headline(WOLF, "no crons")
        lore.subtitle("the wolf is idle")
        return 0
    lore.headline(WOLF, f"crons: {len(crons)}")
    lore.subtitle(f"lodge time · {body.get('tz', 'local')}")
    for cron in crons:
        _wolf_line(cron)
    return 0


def _wolf_add(args) -> int:
    wolt = args.wolt or get_env("WOLTSPACE_WOLT_NAME", "")
    if not wolt:
        lore.failure("which wolt? pass --wolt", subtitle_text="")
        return 2
    prompt = _wolf_message(args, required=True)
    if not prompt:
        lore.failure("the message is empty: pipe it on stdin (heredoc) or pass --message",
                     subtitle_text="")
        return 2
    body = {"wolt": wolt, "prompt": prompt}
    for key, value in (("schedule", args.cron), ("at", args.at),
                       ("name", args.name), ("notify", args.notify)):
        if value:
            body[key] = value
    path = "/wolf/crons?dry_run=1" if args.dry_run else "/wolf/crons"
    status, result = _wolf_request("POST", path, body)
    if _wolf_done(status, result, args.json):
        return 1
    if args.json:
        print(json.dumps(result, indent=2))
    elif args.dry_run:
        lore.headline(WOLF, f"dry run: would add {wolt}/{result['entry']['name']}")
        lore.subtitle("nothing written")
        lore.plain(json.dumps(result["entry"], indent=2))
    else:
        lore.headline(WOLF, f"added {wolt}/{result['name']}")
        lore.labelled("next", _wolf_time(result.get("next_run")))
    return 0


def _wolf_set(args) -> int:
    body = {}
    for key, value in (("schedule", args.cron), ("at", args.at),
                       ("notify", args.notify), ("wolt", args.move_to)):
        if value is not None:
            body[key] = value
    prompt = _wolf_message(args, required=False)
    if prompt is not None:
        body["prompt"] = prompt
    if not body:
        lore.failure("nothing to change: --cron, --at, --message, --notify or --move-to",
                     subtitle_text="")
        return 2
    status, result = _wolf_request("PUT", f"/wolf/crons/{args.wolt}/{args.name}", body)
    if _wolf_done(status, result, args.json):
        return 1
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        lore.headline(WOLF, f"updated {result['wolt']}/{result['name']}")
        _wolf_line(result)
    return 0


def _wolf_rm(args) -> int:
    status, result = _wolf_request("DELETE", f"/wolf/crons/{args.wolt}/{args.name}")
    if _wolf_done(status, result, args.json):
        return 1
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        lore.headline(lore.MOON, f"removed {args.wolt}/{args.name}")
    return 0


def _wolf_run(args) -> int:
    status, result = _wolf_request("POST", f"/wolf/crons/{args.wolt}/{args.name}/fire")
    if _wolf_done(status, result, args.json):
        return 1
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        lore.headline(WOLF, f"running {args.wolt}/{args.name} now")
        lore.labelled("session", result.get("session") or "-")
        if result.get("url"):
            lore.link(result["url"])
    return 0


def _wolf_runs(args) -> int:
    from urllib.parse import urlencode

    query = {"limit": args.limit}
    if args.wolt:
        query["wolt"] = args.wolt
    status, result = _wolf_request("GET", f"/wolf/fires?{urlencode(query)}")
    if _wolf_done(status, result, args.json):
        return 1
    fires = result.get("fires", [])
    if args.json:
        print(json.dumps(fires, indent=2))
        return 0
    if not fires:
        lore.headline(WOLF, "no runs yet")
        return 0
    lore.headline(WOLF, f"last {len(fires)} wolf events")
    for fire in fires:
        extra = fire.get("session") or fire.get("missed") or fire.get("error") or ""
        lore.plain(
            f"{lore.INDENT}{_wolf_time(fire.get('ts'))}  "
            f"{fire.get('owner', '?')}/{fire.get('cron', '?'):<24} "
            f"{fire.get('event', '?'):<11} {extra}".rstrip()
        )
    return 0


def _connector_store(layout: RuntimeLayout):
    """The lodge's own connector state code: one store, one lock, one writer."""
    for entry in (str(layout.install_root), str(layout.runtime_lib)):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    from server import connectors

    return connectors, connectors.ConnectorStore(layout.wolts_dir / ".space" / "connectors")


def _connector(args) -> int:
    """A bare `woltspace connector` names its own verbs."""
    args.connector_parser.print_help()
    return 1


def _connector_run(args, action) -> int:
    """Run one owner action; a refusal or unreadable state is a message and exit 1."""
    connectors, store = _connector_store(RuntimeLayout.from_env())
    try:
        store.recover()
        payload = action(connectors, store)
    except (connectors.ConnectorError, connectors.ConnectorStateUnreadable) as exc:
        print(f"woltspace connector: {exc}", file=sys.stderr)
        return 1
    lines = payload.pop("lines", ())
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    for line in lines:
        print(line)
    return 0


def _connector_list(args) -> int:
    def action(_connectors, store):
        instances = store.list_instances()
        bindings = store.list_bindings()
        lines = []
        for instance in instances:
            state = "enabled" if instance["enabled"] else "disabled"
            lines.append(
                f"{instance['instance_id']}  {instance['channel']}  account={instance['account']}"
                f"  {state}  generation={instance['generation']}"
            )
            for binding in bindings:
                if binding["instance_id"] != instance["instance_id"]:
                    continue
                thread = f" thread={binding['thread']}" if binding.get("thread") else ""
                wake = " may-wake" if binding["may_wake"] else ""
                lines.append(
                    f"    sender={binding['sender']} conversation={binding['conversation']}"
                    f"{thread} -> {binding['default_wolt']}{wake}"
                )
        if not lines:
            lines.append("no connector instances")
        from .handoff import DebtLedger

        ledger = DebtLedger(str(RuntimeLayout.from_env().platform_state)).status()
        if not ledger["readable"]:
            lines.append(f"revocation ledger unreadable: {ledger['error']} (kept as it is; recovery is failing)")
        elif ledger["debts"]:
            lines.append("revocations owed: " + ", ".join(f"{i}/{g}" for i, g in ledger["debts"]))
        return {"instances": instances, "bindings": bindings, "ledger": ledger, "lines": lines}
    return _connector_run(args, action)


def _connector_add(args) -> int:
    def action(_connectors, store):
        instance = store.add_instance(args.instance_id, args.channel, args.account)
        return {"instance": instance, "lines": [
            f"registered {args.instance_id} (disabled, no code installed); bind a sender, then enable it",
        ]}
    return _connector_run(args, action)


def _connector_enable(args) -> int:
    def action(_connectors, store):
        store.enable(args.instance_id)
        return {"ok": True, "lines": [f"{args.instance_id} enabled"]}
    return _connector_run(args, action)


def _connector_disable(args) -> int:
    def action(_connectors, store):
        store.disable(args.instance_id)
        return {"ok": True, "lines": [f"{args.instance_id} disabled; its queue waits"]}
    return _connector_run(args, action)


def _connector_remove(args) -> int:
    def action(_connectors, store):
        outcome = store.remove_instance(args.instance_id)
        return {"ok": True, **outcome, "lines": [
            f"removed {args.instance_id}: {outcome['cancelled']} queued deliveries cancelled",
        ]}
    return _connector_run(args, action)


FORGED_SENDER_WARNING = (
    "a malicious connector can forge an allowed sender and use any grant you turn on here"
)


def _connector_bind(args) -> int:
    """Grant one provider sender in one conversation; the chat-command grants are the owner's.

    Residual trust, said every time a grant is turned on: core bounds what
    a connector submits, it cannot prove the provider sender really sent it,
    so a malicious connector can forge an allowed sender and use any grant
    the owner turned on for that binding.
    """
    def action(_connectors, store):
        selectable = [name.strip() for name in (args.selectable_wolts or "").split(",") if name.strip()]
        binding = store.bind(
            args.instance_id, sender=args.sender, conversation=args.conversation,
            thread=args.thread, default_wolt=args.wolt, may_wake=args.may_wake,
            selectable_wolts=selectable, may_create=args.may_create,
            may_stop_own_sessions=args.may_stop_own_sessions,
            create_per_hour=args.create_per_hour, create_concurrent=args.create_concurrent,
            may_transcribe=args.transcribe, may_manage_apps=args.may_manage_apps,
        )
        lines = [f"{args.sender} in {args.conversation} -> {args.wolt}"]
        grants = []
        if binding["may_wake"]:
            grants.append("may wake a stopped session")
        if binding["selectable_wolts"]:
            grants.append("may pick: " + ", ".join(binding["selectable_wolts"]))
        if binding["may_create"]:
            grants.append(
                f"may start sessions ({binding['create_per_hour']}/hour, {binding['create_concurrent']} live)"
            )
        if binding["may_stop_own_sessions"]:
            grants.append("may stop the sessions it started or talked to")
        if binding.get("may_manage_apps"):
            grants.append("may list, start and stop the lodge's apps (/apps)")
        if binding.get("may_transcribe"):
            grants.append(
                "voice notes are transcribed: the audio is sent to OpenAI (whisper-1) with this lodge's "
                "OPENAI_API_KEY, within a daily byte and request budget"
            )
        if grants:
            lines.extend(f"  grant: {grant}" for grant in grants)
            lines.append(f"  warning: {FORGED_SENDER_WARNING}")
        return {"binding": binding, "lines": lines}
    return _connector_run(args, action)


def _connector_unbind(args) -> int:
    def action(_connectors, store):
        removed = store.unbind(
            args.instance_id, sender=args.sender, conversation=args.conversation,
            thread=args.thread,
        )
        return {"removed": removed, "lines": [
            "binding removed" if removed else "no such binding",
        ]}
    return _connector_run(args, action)


def _connector_token(args) -> int:
    """Mint the instance's next credential, or revoke the current one.

    The token is printed once, on stdout and nowhere else, so it can be piped
    straight into a handoff file. It never appears in an argv.
    """
    def action(_connectors, store):
        if args.revoke:
            count = store.revoke_tokens(args.instance_id)
            return {"revoked": count, "lines": [f"revoked {count} token(s)"]}
        token = store.issue_token(args.instance_id)
        print("the previous generation is revoked; its queue is untouched", file=sys.stderr)
        return {"token": token, "lines": [token]}
    return _connector_run(args, action)


def _connector_runtime(args) -> int:
    """Show or set which runtime owns a channel: its adapter or its connector.

    A switch is refused while either runtime runs, or might: it is made
    under two locks taken without waiting, the lodge's own control-plane
    lock (held by `woltspace start` for the lodge's whole life, so no
    adapter child or connector API is live) and the channel's run lock
    (held by a running connector process until it exits, so no provider
    call of its is in flight). Held or unknown refuses. Nothing is polled
    by either runtime until the next start, which reads the new setting.
    """
    def action(connectors, store):
        layout = RuntimeLayout.from_env()
        path = layout.wolts_dir / "woltspace.json"
        if args.runtime:
            from .instance import DataRootLock, InstanceConflict

            lock = DataRootLock(layout, f"switch-{os.getpid()}")
            try:
                lock.acquire()
            except InstanceConflict as exc:
                raise connectors.ConnectorError(
                    409, "lodge_running",
                    f"the lodge is running or starting ({exc}); stop it, then switch",
                ) from None
            except OSError as exc:
                raise connectors.ConnectorError(
                    409, "lodge_state_unknown", f"cannot tell whether the lodge runs: {exc}",
                ) from None
            try:
                try:
                    with connectors.held_run_lock(connectors.run_lock_path(store.root, args.channel)):
                        connectors.set_channel_runtime(path, args.channel, args.runtime)
                except BlockingIOError:
                    raise connectors.ConnectorError(
                        409, "connector_running",
                        f"a {args.channel} connector process is running; stop it, then switch",
                    ) from None
                except OSError as exc:
                    raise connectors.ConnectorError(
                        409, "connector_state_unknown",
                        f"cannot tell whether a {args.channel} connector runs: {exc}",
                    ) from None
            finally:
                lock.release()
        runtime = connectors.channel_runtime(path, args.channel)
        lines = [f"{args.channel}.runtime = {runtime}"]
        if args.runtime:
            lines.append("takes effect at the next `woltspace start`")
        return {"channel": args.channel, "runtime": runtime, "lines": lines}
    return _connector_run(args, action)


def _connector_handoffs(args) -> int:
    """Accepted events still waiting for a session, parked ones included; retry or cancel one."""
    def action(_connectors, store):
        if args.retry:
            done = store.retry_handoff(args.instance_id, args.retry)
            return {"retried": done, "lines": ["queued for another attempt" if done else "no such pending event"]}
        if args.cancel:
            done = store.cancel_handoff(args.instance_id, args.cancel)
            return {"cancelled": done, "lines": ["cancelled; its files are released" if done else "no such pending event"]}
        records = store.handoffs(args.instance_id)
        lines = [
            f"{r['event_id']}  {r['state']}  attempts={r.get('attempts', 0)}"
            + (f"  {r['last_error']}" if r.get("last_error") else "")
            for r in records
        ] or ["nothing waiting"]
        return {"handoffs": records, "lines": lines}
    return _connector_run(args, action)


def _connector_cursor(args) -> int:
    """Owner-side: seed or clear an instance's provider cursor (a handover)."""
    def action(_connectors, store):
        cursor = None if args.clear else args.cursor
        store.seed_cursor(args.instance_id, cursor)
        return {"instance_id": args.instance_id, "cursor": cursor, "lines": [
            f"{args.instance_id} cursor = {cursor if cursor is not None else '(none: provider backlog)'}",
        ]}
    return _connector_run(args, action)


def _connector_operations(args) -> int:
    """Parked chat commands (effect uncertain after a crash): say what happened, nothing is rerun."""
    def action(_connectors, store):
        if args.done and args.abandon:
            raise _connectors.ConnectorError(400, "--done and --abandon are exclusive")
        if args.abandon and args.session:
            raise _connectors.ConnectorError(400, "--session goes with --done, not --abandon")
        if args.done:
            ok = store.resolve_operation(args.instance_id, args.done, session=args.session)
            return {"resolved": ok, "lines": ["resolved" if ok else "no such parked operation"]}
        if args.abandon:
            ok = store.resolve_operation(args.instance_id, args.abandon, abandon=True)
            return {"abandoned": ok, "lines": ["abandoned; its reservation is released" if ok else "no such parked operation"]}
        records = store.uncertain_operations(args.instance_id)
        lines = [
            f"{r['event_id']}  /{r['command']} {r.get('wolt') or r.get('session') or ''}  {r.get('note', '')}".rstrip()
            for r in records
        ] or ["nothing parked"]
        return {"operations": records, "lines": lines}
    return _connector_run(args, action)


def _connector_queue(args) -> int:
    def action(_connectors, store):
        deliveries = store.list_deliveries(args.instance_id)
        lines = [
            f"{d['delivery_id']}  {d['status']}  attempts={d.get('attempts', 0)}"
            f"  conversation={d['conversation']}" + (f"  {d['error']}" if d.get("error") else "")
            for d in deliveries
        ] or ["queue is empty"]
        return {"deliveries": deliveries, "lines": lines}
    return _connector_run(args, action)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="woltspace",
        description=f"{lore.BEAVER} woltspace - a lodge for your wolts",
        epilog=(
            '"how much wolt could a wolt chuck chuck\n'
            ' if a wolt chuck could chuck wolt?"\n\n'
            "all state lives under ~/.woltspace/wolts "
            "(override with WOLTSPACE_WOLTS_DIR)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")
    session = sub.add_parser(
        "session",
        help="sessions (delegated to the bundled control client)",
        add_help=False,
    )
    session.add_argument("session_args", nargs=argparse.REMAINDER)
    session.set_defaults(func=_session)

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

    update = sub.add_parser("update", help="check or update a uv-managed native install")
    update.add_argument("--check", action="store_true", help="check only; change nothing")
    update.add_argument("--version", default="", metavar="X", help="install this exact release")
    update.add_argument("--pre", action="store_true", help="allow prerelease targets")
    update.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    update.add_argument("--json", action="store_true")
    update.set_defaults(func=_update)

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

    auto = sub.add_parser(
        "auto", help="manage repository-scoped Auto consent for native sessions"
    )
    auto.set_defaults(func=_auto, auto_parser=auto)
    auto_sub = auto.add_subparsers(dest="verb")

    auto_grant = auto_sub.add_parser(
        "grant", help="let a wolt work unattended in one exact directory"
    )
    auto_grant.add_argument("wolt")
    auto_grant.add_argument(
        "--workdir", default="", help="directory to approve (default: the wolt's home)"
    )
    auto_grant.add_argument("--json", action="store_true")
    auto_grant.set_defaults(func=_auto_grant)

    auto_revoke = auto_sub.add_parser(
        "revoke", help="withdraw one wolt's Auto consent for a directory"
    )
    auto_revoke.add_argument("wolt")
    auto_revoke.add_argument(
        "--workdir", default="", help="directory to revoke (default: the wolt's home)"
    )
    auto_revoke.add_argument("--json", action="store_true")
    auto_revoke.set_defaults(func=_auto_revoke)

    auto_list = auto_sub.add_parser("list", help="show every standing Auto grant")
    auto_list.add_argument("--json", action="store_true")
    auto_list.set_defaults(func=_auto_list)

    access = sub.add_parser(
        "access", help="show or set the Cloudflare Access settings remote callers need"
    )
    access.set_defaults(func=_access, access_parser=access)
    access_sub = access.add_subparsers(dest="verb")
    access_show = access_sub.add_parser("show", help="show the saved Access settings")
    access_show.add_argument("--json", action="store_true")
    access_show.set_defaults(func=_access_show)
    access_set = access_sub.add_parser(
        "set", help="save Access settings so remote requests can be verified"
    )
    access_set.add_argument("--team-domain", required=True,
                            help="e.g. yourteam.cloudflareaccess.com")
    access_set.add_argument("--lodge-aud", required=True,
                            help="Application Audience (AUD) tag of the lodge's Access app")
    access_set.add_argument("--apps-aud", default=None,
                            help="AUD tag of the apps' Access app; omit to keep the saved one, "
                                 '"" to turn remote apps off')
    access_set.add_argument("--owner-email", required=True)
    access_set.set_defaults(func=_access_set)
    access_clear = access_sub.add_parser(
        "clear", help="remove Access settings; the lodge stops verifying remote requests"
    )
    access_clear.set_defaults(func=_access_clear)

    seed = sub.add_parser(
        "seed", help="create, inspect, and install shareable colony seeds"
    )
    seed.set_defaults(func=_seed, seed_parser=seed)
    seed_sub = seed.add_subparsers(dest="verb")

    seed_create = seed_sub.add_parser(
        "create", help="create a shareable colony seed (not a stateful backup)"
    )
    seed_create.add_argument("output", help="new directory to create")
    seed_create.add_argument("--name", required=True, help="portable seed name")
    seed_create.add_argument(
        "--wolt", action="append", required=True, help="wolt to include (repeatable)"
    )
    seed_create.add_argument(
        "--app", action="append", default=[], help="tracked app source to include (repeatable)"
    )
    seed_create.add_argument(
        "--skill", action="append", default=[], metavar="WOLT:SKILL",
        help="explicit user-owned skill to include (repeatable)",
    )
    seed_create.add_argument("--json", action="store_true")
    seed_create.set_defaults(func=_seed_create)

    seed_inspect = seed_sub.add_parser(
        "inspect", help="validate and summarize a colony seed"
    )
    seed_inspect.add_argument("source")
    seed_inspect.add_argument("--json", action="store_true")
    seed_inspect.set_defaults(func=_seed_inspect)

    seed_install = seed_sub.add_parser(
        "install", help="install independent starter copies from a directory or Git URL"
    )
    seed_install.add_argument("source")
    seed_install.add_argument("--json", action="store_true")
    seed_install.set_defaults(func=_seed_install)

    dig = sub.add_parser("dig", help="let a wolt visit an owner-approved SSH host")
    dig.set_defaults(func=_dig, dig_parser=dig)
    dig_sub = dig.add_subparsers(dest="dig_command")

    dig_grant = dig_sub.add_parser("grant", help="approve an exact SSH destination")
    dig_grant.add_argument("name")
    dig_grant.add_argument("destination", help="SSH host or alias from ~/.ssh/config")
    dig_grant.add_argument("--wolt", required=True)
    dig_grant.add_argument("--bootstrap-dir", default=".woltspace/bootstrap")
    dig_grant.add_argument("--json", action="store_true")
    dig_grant.set_defaults(func=_dig_grant)

    dig_list = dig_sub.add_parser("list", help="show approved SSH destinations")
    dig_list.add_argument("--json", action="store_true")
    dig_list.set_defaults(func=_dig_list)

    dig_revoke = dig_sub.add_parser("revoke", help="remove a Woltspace dig approval")
    dig_revoke.add_argument("name")
    dig_revoke.add_argument("--json", action="store_true")
    dig_revoke.set_defaults(func=_dig_revoke)

    dig_connect = dig_sub.add_parser("connect", help="open the approved SSH destination")
    dig_connect.add_argument("name")
    dig_connect.add_argument("remote_command", nargs=argparse.REMAINDER)
    dig_connect.add_argument("--json", action="store_true")
    dig_connect.set_defaults(func=_dig_connect)

    wolf = sub.add_parser("wolf", help="scheduled runs: list, add, change, remove, run now")
    wolf.set_defaults(func=_wolf, wolf_parser=wolf)
    wolf_sub = wolf.add_subparsers(dest="wolf_command")

    wolf_list = wolf_sub.add_parser("list", help="every cron, soonest first")
    wolf_list.add_argument("--wolt", default="")
    wolf_list.add_argument("--json", action="store_true")
    wolf_list.set_defaults(func=_wolf_list)

    wolf_add = wolf_sub.add_parser(
        "add", help="add a cron; the message is read from stdin (heredoc)"
    )
    wolf_add.add_argument("--wolt", default="", help="owning wolt (default: $WOLTSPACE_WOLT_NAME)")
    when = wolf_add.add_mutually_exclusive_group(required=True)
    when.add_argument("--cron", metavar="EXPR", help="recurring: 'min hour day month weekday'")
    when.add_argument("--at", metavar="YYYY-MM-DDTHH:MM", help="once, in lodge local time")
    wolf_add.add_argument("--name", default="", help="default: from the message's first words")
    wolf_add.add_argument("--notify", default="", choices=["telegram", "slack"],
                          help="also ping you on this channel when it runs (default: no ping)")
    wolf_add.add_argument("--message", default=None, help="the prompt, instead of stdin")
    wolf_add.add_argument("--dry-run", action="store_true", help="show it, write nothing")
    wolf_add.add_argument("--json", action="store_true")
    wolf_add.set_defaults(func=_wolf_add)

    wolf_set = wolf_sub.add_parser("set", help="change a cron in place, or move it")
    wolf_set.add_argument("wolt")
    wolf_set.add_argument("name")
    set_when = wolf_set.add_mutually_exclusive_group()
    set_when.add_argument("--cron", metavar="EXPR", default=None)
    set_when.add_argument("--at", metavar="YYYY-MM-DDTHH:MM", default=None)
    wolf_set.add_argument("--message", default=None, help="new prompt; '-' reads stdin")
    wolf_set.add_argument("--notify", default=None, choices=["telegram", "slack", ""],
                          help="ping channel when it runs; '' turns the ping off")
    wolf_set.add_argument("--move-to", metavar="WOLT", default=None)
    wolf_set.add_argument("--json", action="store_true")
    wolf_set.set_defaults(func=_wolf_set)

    wolf_rm = wolf_sub.add_parser("rm", help="remove a cron")
    wolf_rm.add_argument("wolt")
    wolf_rm.add_argument("name")
    wolf_rm.add_argument("--json", action="store_true")
    wolf_rm.set_defaults(func=_wolf_rm)

    wolf_run = wolf_sub.add_parser("run", help="run a cron now; its schedule is untouched")
    wolf_run.add_argument("wolt")
    wolf_run.add_argument("name")
    wolf_run.add_argument("--json", action="store_true")
    wolf_run.set_defaults(func=_wolf_run)

    wolf_runs = wolf_sub.add_parser("runs", help="recent fires, newest first")
    wolf_runs.add_argument("--wolt", default="")
    wolf_runs.add_argument("--limit", type=int, default=20)
    wolf_runs.add_argument("--json", action="store_true")
    wolf_runs.set_defaults(func=_wolf_runs)

    connector = sub.add_parser(
        "connector", help="connector instances, their bindings and credentials"
    )
    connector.set_defaults(func=_connector, connector_parser=connector)
    connector_sub = connector.add_subparsers(dest="verb")
    connector_list = connector_sub.add_parser("list", help="every instance and its bindings")
    connector_list.add_argument("--json", action="store_true")
    connector_list.set_defaults(func=_connector_list)
    connector_add = connector_sub.add_parser(
        "add", help="register an instance, disabled (registration only: installs no code)"
    )
    connector_add.add_argument("instance_id")
    connector_add.add_argument("--channel", required=True, help="the chat kind it serves, e.g. telegram")
    connector_add.add_argument("--account", required=True, help="the provider account it is, e.g. a bot id")
    connector_add.add_argument("--json", action="store_true")
    connector_add.set_defaults(func=_connector_add)
    for verb, func, help_text in (
        ("enable", _connector_enable, "route to the instance; one per channel"),
        ("disable", _connector_disable, "pause the instance; its queue and files wait"),
        ("queue", _connector_queue, "show the instance's deliveries"),
    ):
        verb_parser = connector_sub.add_parser(verb, help=help_text)
        verb_parser.add_argument("instance_id")
        verb_parser.add_argument("--json", action="store_true")
        verb_parser.set_defaults(func=func)
    connector_remove = connector_sub.add_parser(
        "remove", help="revoke its tokens, drop its bindings and cancel its queue"
    )
    connector_remove.add_argument("instance_id")
    connector_remove.add_argument("--json", action="store_true")
    connector_remove.set_defaults(func=_connector_remove)
    connector_bind = connector_sub.add_parser(
        "bind", help="allow one provider sender in one conversation"
    )
    connector_bind.add_argument("instance_id")
    connector_bind.add_argument("--sender", required=True)
    connector_bind.add_argument("--conversation", required=True)
    connector_bind.add_argument("--thread", default=None)
    connector_bind.add_argument("--wolt", required=True, help="where unrouted text goes")
    connector_bind.add_argument("--may-wake", action="store_true", help="a message may resume a stopped session")
    connector_bind.add_argument(
        "--selectable-wolts", default="", metavar="A,B",
        help="wolts this chat may pick with /wolt and start with /new",
    )
    connector_bind.add_argument("--may-create", action="store_true", help="/new, or a message for a wolt with no live session, may start a session (not the same as --may-wake)")
    connector_bind.add_argument("--may-stop-own-sessions", action="store_true", help="/kill may stop a session this chat started or talked to")
    connector_bind.add_argument("--may-manage-apps", action="store_true", help="/apps may list, start and stop the lodge's apps")
    connector_bind.add_argument("--transcribe", action="store_true", help="transcribe voice notes (the audio is sent to OpenAI)")
    connector_bind.add_argument("--create-per-hour", type=int, default=None, help="/new limit per hour (default 6)")
    connector_bind.add_argument("--create-concurrent", type=int, default=None, help="live sessions /new may have started at once (default 2)")
    connector_bind.add_argument("--json", action="store_true")
    connector_bind.set_defaults(func=_connector_bind)
    connector_unbind = connector_sub.add_parser("unbind", help="remove a binding")
    connector_unbind.add_argument("instance_id")
    connector_unbind.add_argument("--sender", required=True)
    connector_unbind.add_argument("--conversation", required=True)
    connector_unbind.add_argument("--thread", default=None)
    connector_unbind.add_argument("--json", action="store_true")
    connector_unbind.set_defaults(func=_connector_unbind)
    connector_runtime = connector_sub.add_parser(
        "runtime", help="show or set which runtime owns a channel: adapter or connector"
    )
    connector_runtime.add_argument("channel")
    connector_runtime.add_argument("runtime", nargs="?", choices=("adapter", "connector"), default=None)
    connector_runtime.add_argument("--json", action="store_true")
    connector_runtime.set_defaults(func=_connector_runtime)
    connector_handoffs = connector_sub.add_parser(
        "handoffs", help="accepted events not yet handed to a session; --retry or --cancel one by event id"
    )
    connector_handoffs.add_argument("instance_id")
    connector_handoffs.add_argument("--retry", metavar="EVENT_ID", default=None, help="the event id shown by the listing")
    connector_handoffs.add_argument("--cancel", metavar="EVENT_ID", default=None)
    connector_handoffs.add_argument("--json", action="store_true")
    connector_handoffs.set_defaults(func=_connector_handoffs)
    connector_operations = connector_sub.add_parser(
        "operations", help="chat commands parked after a crash; --done EVENT_ID --session NAME, or --abandon EVENT_ID"
    )
    connector_operations.add_argument("instance_id")
    connector_operations.add_argument("--done", metavar="EVENT_ID", default=None)
    connector_operations.add_argument("--session", default=None, help="the session the parked /new did start")
    connector_operations.add_argument("--abandon", metavar="EVENT_ID", default=None)
    connector_operations.add_argument("--json", action="store_true")
    connector_operations.set_defaults(func=_connector_operations)
    connector_cursor = connector_sub.add_parser(
        "cursor", help="seed an instance's provider cursor (a handover), or clear it"
    )
    connector_cursor.add_argument("instance_id")
    connector_cursor.add_argument("cursor", nargs="?", type=int, default=None)
    connector_cursor.add_argument("--clear", action="store_true")
    connector_cursor.add_argument("--json", action="store_true")
    connector_cursor.set_defaults(func=_connector_cursor)
    connector_token = connector_sub.add_parser(
        "token", help="mint the instance's next token (printed once), or revoke it"
    )
    connector_token.add_argument("instance_id")
    connector_token.add_argument("--revoke", action="store_true")
    connector_token.add_argument("--json", action="store_true")
    connector_token.set_defaults(func=_connector_token)

    tui = sub.add_parser("tui", help="open the terminal UI")
    tui.add_argument("--dry-run", action="store_true", help="show resolution without launching")
    tui.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    tui.add_argument("tui_args", nargs=argparse.REMAINDER)
    tui.set_defaults(func=_tui)
    return parser


def main(argv: list[str] | None = None) -> int:
    # One deprecation line per process, before anything reads a path.
    warn_legacy_once()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        # A bare `woltspace` gets the wordmark before the usage, the way the
        # launcher always greeted an empty command line.
        lore.banner()
        parser.print_help()
        return 1
    return args.func(args)
