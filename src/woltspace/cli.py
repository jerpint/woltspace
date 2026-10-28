"""Installed Woltspace command line."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

from . import __version__, lore
from .envvars import get_env, warn_legacy_once
from .layout import RuntimeLayout


def _lodge_auth_module(layout: RuntimeLayout):
    lib = str(layout.runtime_lib)
    if lib not in sys.path:
        sys.path.insert(0, lib)
    import lodge_auth
    return lodge_auth


def _pair(args) -> int:
    """Print a one-time link that pairs a browser with this lodge."""
    layout = RuntimeLayout.from_env()
    lodge_auth = _lodge_auth_module(layout)
    if get_env("WOLTSPACE_WOLT_SESSION"):
        lore.failure("pairing is for the owner: run `woltspace pair` in your own terminal, not in a wolt session",
                     subtitle_text="a wolt can't let a browser in")
        return 1
    lodge_auth.ensure_keys(layout.wolts_dir)
    if args.forget:
        lodge_auth.forget_browsers(layout.wolts_dir)
        lore.note("every paired browser was unpaired")
    code = lodge_auth.new_pairing_code(layout.wolts_dir)
    links = [f"{layout.endpoint.rstrip('/')}/pair?code={code}"]
    from .lifecycle import read_tunnel_url
    tunnel = (read_tunnel_url(layout) or "").rstrip("/")
    if tunnel:
        links.append(f"{tunnel}/pair?code={code}")
    if args.json:
        print(json.dumps({"links": links, "expires_in": lodge_auth.PAIRING_TTL,
                          "enabled": lodge_auth.enabled(os.environ)}))
        return 0
    for url in links:
        lore.link(url)
    lore.note("works once, for ten minutes; open it in the browser to pair")
    if not lodge_auth.enabled(os.environ):
        lore.warn("lodge auth is off: set WOLTSPACE_LODGE_AUTH=1 in the lodge's .env and restart to require it")
    return 0


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


def _restart(args) -> int:
    from .update import restart

    return restart(args)


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


def _delegate(args) -> int:
    """Delegate a whole noun (session, wolt) to the bundled control client."""
    layout = RuntimeLayout.from_env()
    client = layout.install_root / "container" / "bin" / "woltspace"
    if not client.is_file():
        print(f"bundled {args.command} client not found: {client}", file=sys.stderr)
        return 2
    os.environ.setdefault("WOLTSPACE_API", layout.endpoint)
    # The bundled client uses an env-based woltspace-python shebang, but a
    # native terminal need not have the bundle bin directory on PATH. Reuse
    # the interpreter running this CLI instead of rediscovering one.
    os.execv(sys.executable, [sys.executable, str(client), args.command, *args.noun_args])
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


_RECORD_KEY = re.compile(r"record((?:\.[A-Za-z_][A-Za-z0-9_]*)+)")


def _db_show(args) -> int:
    """Print one lodge.db table, read-only, with the lodge up or down.

    No SQL from the user: the table and every --where column are checked
    against the file's own schema, and values are bound, never pasted in.
    """
    import sqlite3

    layout = RuntimeLayout.from_env()
    lib_dir = str(layout.runtime_lib)
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    import lodge_db

    try:
        conn = lodge_db.connect(layout.wolts_dir, readonly=True)
    except sqlite3.Error as exc:
        db = lodge_db.path(layout.wolts_dir)
        print(f"cannot read {db}: {exc}" if db.exists() else f"no lodge.db at {db}", file=sys.stderr)
        return 1
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if args.table not in tables:
            print(f"no table {args.table}; tables: {', '.join(sorted(tables))}", file=sys.stderr)
            return 2
        columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{args.table}")')]
        clauses, values = [], []
        for where in args.where:
            column, sep, value = where.partition("=")
            if not sep:
                print(f"--where takes col=value, not {where!r}", file=sys.stderr)
                return 2
            key = _RECORD_KEY.fullmatch(column)
            if column in columns:
                clauses.append(f'"{column}" = ?')  # the column's affinity converts it
                values.append(value)
            elif column.startswith("record.") and "record" in columns:
                if key is None:
                    print(f"{column!r} is not a record key", file=sys.stderr)
                    return 2
                # JSON has no affinity: "7" and 7 are both asked for.
                clauses.append("json_extract(record, ?) IN (?, ?)")
                number = int(value) if value.lstrip("-").isdigit() else value
                values.extend(["$" + key.group(1), value, number])
            else:
                print(f"no column {column} in {args.table}; columns: {', '.join(columns)}"
                      + (", or record.<key>" if "record" in columns else ""), file=sys.stderr)
                return 2
        sql = f'SELECT * FROM "{args.table}"'
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        rows = conn.execute(sql + " ORDER BY rowid", values).fetchall()
    finally:
        conn.close()
    if args.json:
        for row in rows:
            data = dict(row)
            if isinstance(data.get("record"), str):
                data["record"] = json.loads(data["record"])
            print(json.dumps(data))
        return 0
    # The record column is the whole JSON: --json prints it.
    shown = [c for c in columns if c != "record"]
    print("| " + " | ".join(shown) + " |")
    print("|" + "|".join("---" for _ in shown) + "|")
    for row in rows:
        cells = ("" if row[c] is None else str(row[c]).replace("|", "\\|") for c in shown)
        print("| " + " | ".join(cells) + " |")
    return 0


def _backup(args) -> int:
    from .backup import create_backup, summary_lines

    layout = RuntimeLayout.from_env()
    wolts_dir = args.wolts_dir or layout.wolts_dir
    try:
        result = create_backup(
            wolts_dir, out_dir=args.out or None, tag=args.tag or None, full=args.full,
        )
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
    import tarfile

    from .backup import restore_backup, restore_lines

    try:
        identity = None
        if args.identity == "-":
            identity = sys.stdin.buffer.read()
        elif args.identity:
            with open(os.path.expanduser(args.identity), "rb") as key:
                identity = key.read()
        result = restore_backup(args.archive, to=args.to or None, identity=identity)
    except (FileNotFoundError, FileExistsError, OSError, ValueError, tarfile.TarError) as exc:
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
    if stripped.startswith(("warnings:", "withheld:", "left out:")):
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


def _graft_runtime_lib() -> RuntimeLayout:
    layout = RuntimeLayout.from_env()
    lib_dir = str(layout.runtime_lib)
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    return layout


def _transcribe_module():
    """The runtime's own transcription settings: one validator, one writer."""
    layout = _graft_runtime_lib()
    import transcription_settings

    return layout, transcription_settings


def _transcription(args) -> int:
    """A bare `woltspace transcription` names its own verbs."""
    args.transcription_parser.print_help()
    return 1


def _transcription_show(args) -> int:
    layout, module = _transcribe_module()
    try:
        settings = module.load_settings(module.config_path(layout.wolts_dir))
    except module.SettingsInvalid as exc:
        print(f"woltspace transcription: {exc}", file=sys.stderr)
        return 1
    payload = settings.as_config()
    if args.json:
        print(json.dumps({"transcription": payload}, indent=2))
        return 0
    for key, value in payload.items():
        lore.field(key, value or "(none: the server needs no key)", value_style=lore.TEAL)
    return 0


def _transcription_set(args) -> int:
    """Choose where voice notes are transcribed: any OpenAI-style endpoint.

    Read on every voice note, so a change applies without a restart. Only the
    name of the key's environment variable is saved, never the key.
    """
    layout, module = _transcribe_module()
    path = module.config_path(layout.wolts_dir)
    try:
        current = module.load_settings(path).as_config()
        if args.base_url is not None:
            from urllib.parse import urlsplit

            module.check_base_url(args.base_url.strip().rstrip("/"))  # a bad URL is a plain error

            def authority(url):
                parts = urlsplit(url.strip())
                return (parts.scheme, (parts.hostname or "").lower(), parts.port)

            if (authority(args.base_url) != authority(current["base_url"])
                    and args.key_env is None and not args.no_key):
                print(
                    "woltspace transcription: a new server needs its key named: pass "
                    "--key-env NAME (the variable holding that server's key) or --no-key, "
                    f"so {current['key_env'] or 'no key'} is not sent to it by accident.",
                    file=sys.stderr,
                )
                return 1
            current["base_url"] = args.base_url
        if args.model is not None:
            current["model"] = args.model
        if args.no_key:
            current["key_env"] = ""
        elif args.key_env is not None:
            current["key_env"] = args.key_env
        settings = module.TranscriptionSettings.parse(current)
        module.save_settings(path, settings)
    except (module.SettingsInvalid, OSError) as exc:
        print(f"woltspace transcription: {exc}", file=sys.stderr)
        return 1
    print(f"Saved. Voice notes go to {settings.endpoint} ({settings.model}).")
    return 0


def _transcription_clear(args) -> int:
    layout, module = _transcribe_module()
    try:
        module.save_settings(module.config_path(layout.wolts_dir), None)
    except (module.SettingsInvalid, OSError) as exc:
        print(f"woltspace transcription: {exc}", file=sys.stderr)
        return 1
    default = module.TranscriptionSettings()
    print(f"Cleared. Voice notes go to the default, {default.endpoint} ({default.model}).")
    return 0


def _speech_module():
    """The runtime's own speech settings: one validator, one writer."""
    layout = _graft_runtime_lib()
    import speech_settings

    return layout, speech_settings


def _speech(args) -> int:
    """A bare `woltspace speech` names its own verbs."""
    args.speech_parser.print_help()
    return 1


def _speech_show(args) -> int:
    layout, module = _speech_module()
    try:
        saved = module.read_section(module.config_path(layout.wolts_dir))
        settings = module.SpeechSettings.parse(saved)
    except module.SettingsInvalid as exc:
        print(f"woltspace speech: {exc}", file=sys.stderr)
        return 1
    payload = settings.as_config()
    if args.json:
        print(json.dumps({"speech": payload, "saved": saved}, indent=2))
        return 0
    for key, value in payload.items():
        if key in ("model", "voice") and key not in saved:
            value = f"{value} (unless OPENAI_TTS_{key.upper()} is set in the lodge's .env)"
        lore.field(key, value or "(none: the server needs no key)", value_style=lore.TEAL)
    return 0


def _speech_set(args) -> int:
    """Choose where spoken messages are made: any OpenAI-style /audio/speech endpoint.

    Read on every spoken message, so a change applies without a restart. Only
    the name of the key's environment variable is saved, never the key.
    """
    layout, module = _speech_module()
    path = module.config_path(layout.wolts_dir)
    try:
        # Only what the owner names is saved: a model or voice left out keeps
        # following OPENAI_TTS_MODEL / OPENAI_TTS_VOICE in the lodge's .env.
        saved = module.read_section(path)
        current = module.SpeechSettings.parse(saved).as_config()
        if args.base_url is not None:
            from urllib.parse import urlsplit

            module.check_base_url(args.base_url.strip().rstrip("/"))  # a bad URL is a plain error

            def authority(url):
                parts = urlsplit(url.strip())
                return (parts.scheme, (parts.hostname or "").lower(), parts.port)

            if (authority(args.base_url) != authority(current["base_url"])
                    and args.key_env is None and not args.no_key):
                print(
                    "woltspace speech: a new server needs its key named: pass "
                    "--key-env NAME (the variable holding that server's key) or --no-key, "
                    f"so {current['key_env'] or 'no key'} is not sent to it by accident.",
                    file=sys.stderr,
                )
                return 1
            saved["base_url"] = args.base_url.strip().rstrip("/")
        if args.model is not None:
            saved["model"] = args.model
        if args.voice is not None:
            saved["voice"] = args.voice
        if args.no_key:
            saved["key_env"] = ""
        elif args.key_env is not None:
            saved["key_env"] = args.key_env
        settings = module.SpeechSettings.parse(saved)
        module.save_section(path, saved)
    except (module.SettingsInvalid, OSError) as exc:
        print(f"woltspace speech: {exc}", file=sys.stderr)
        return 1
    print(f"Saved. Spoken messages come from {settings.endpoint} ({settings.model}, voice {settings.voice}).")
    return 0


def _speech_clear(args) -> int:
    layout, module = _speech_module()
    try:
        module.save_settings(module.config_path(layout.wolts_dir), None)
    except (module.SettingsInvalid, OSError) as exc:
        print(f"woltspace speech: {exc}", file=sys.stderr)
        return 1
    default = module.SpeechSettings()
    print(f"Cleared. Spoken messages come from the default, {default.endpoint} ({default.model}).")
    return 0


def _auto_runtime():
    """Borrow the runtime's own grant store and target resolver.

    The grant file's shape, its lock, and the canonicalization rule that makes
    a grant match a spawn all live in `container/lib`, and a second
    implementation here would be a second answer to "is this directory
    approved?". Same sys.path graft `adoption.py` uses to reach the registry.
    """
    layout = _graft_runtime_lib()
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
    """The dig grants, kept in the lodge DB that `container/lib` opens."""
    layout = _graft_runtime_lib()
    from .dig import DigStore

    return DigStore(layout.state_root)


def _dig_grant(args) -> int:
    _graft_runtime_lib()
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
    _graft_runtime_lib()
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
    _graft_runtime_lib()
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
    layout = RuntimeLayout.from_env()
    headers = _lodge_auth_module(layout).client_headers(wolts_dir=layout.wolts_dir)
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
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


# --- woltspace creature: the lodge's custom creatures ---------------------
# A thin client of /creatures, like `woltspace wolf`: the lodge validates and
# writes, so this command and the creatures skill share one path.

def _creature(args) -> int:
    args.creature_parser.print_help()
    return 1


def _creature_list(args) -> int:
    status, body = _wolf_request("GET", "/creatures")
    if _wolf_done(status, body, args.json):
        return 1
    if args.json:
        print(json.dumps(body, indent=2, ensure_ascii=False))
        return 0
    sizes = "  ".join(f"{s['emoji']} {s['id']}" for s in body.get("sizes", []))
    lore.headline(lore.BEAVER, "creatures")
    lore.labelled("sizes", sizes)
    custom = body.get("custom", [])
    if not custom:
        lore.subtitle("no custom creatures yet: woltspace creature add")
        return 0
    for c in custom:
        models = ", ".join(f"{h}: {m}" for h, m in sorted((c.get("models") or {}).items()))
        detail = f"{c['size']}-sized" + (f"  ·  {models}" if models else "")
        lore.labelled(f"{c['emoji']} {c['id']}", detail)
        if c.get("role"):
            lore.plain(f"{lore.SUBINDENT}{c['role']}", lore.BARK)
        if c.get("wolts"):
            lore.plain(f"{lore.SUBINDENT}wolts: {', '.join(c['wolts'])}", lore.BARK)
    return 0


def _creature_add(args) -> int:
    body = {"id": args.name, "emoji": args.emoji, "size": args.size}
    models = {}
    for pair in args.model or []:
        harness, sep, model = pair.partition("=")
        if not sep or not harness or not model:
            lore.failure(f"--model is HARNESS=MODEL, got {pair!r}", subtitle_text="")
            return 2
        models[harness] = model
    if models:
        body["models"] = models
    if args.role:
        body["role"] = args.role
    status, result = _wolf_request("POST", "/creatures", body)
    if _wolf_done(status, result, args.json):
        return 1
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        lore.headline(result["emoji"], f"{args.name} joins the lodge, {result['size']}-sized")
        lore.subtitle(f"give it to a wolt: woltspace creature assign <wolt> {args.name}")
    return 0


def _creature_rm(args) -> int:
    status, result = _wolf_request("DELETE", f"/creatures/{args.name}")
    if _wolf_done(status, result, args.json):
        return 1
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        lore.headline(lore.MOON, f"removed {args.name}")
    return 0


def _creature_assign(args) -> int:
    status, result = _wolf_request("POST", f"/wolts/{args.wolt}/creature", {"type": args.type})
    if _wolf_done(status, result, args.json):
        return 1
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    lore.headline(result["emoji"], f"{args.wolt} is now a {result['type']} (was {result['previous_type']})")
    lore.subtitle("applies from its next session; a running session keeps what it has")
    lore.labelled("next session", f"{result['harness']} · {result.get('model') or 'default'}")
    for key, value in (result.get("kept") or {}).items():
        lore.labelled(f"kept {key}", str(value))
    if result.get("site_emoji_updated"):
        lore.labelled("site", f"emoji now {result['emoji']}")
    return 0


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
    for noun, what in (("session", "sessions"), ("wolt", "configure, archive or delete a wolt")):
        delegated = sub.add_parser(noun, help=f"{what} (delegated to the bundled control client)",
                                   add_help=False)
        delegated.add_argument("noun_args", nargs=argparse.REMAINDER)
        delegated.set_defaults(func=_delegate)

    pair = sub.add_parser("pair", help="print a one-time link that pairs a browser with the lodge")
    pair.add_argument("--forget", action="store_true", help="unpair every browser first")
    pair.add_argument("--json", action="store_true")
    pair.set_defaults(func=_pair)

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

    restart = sub.add_parser("restart", help="stop and start the lodge on the same address, then check health")
    restart.add_argument("--json", action="store_true")
    restart.set_defaults(func=_restart)

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

    backup = sub.add_parser(
        "backup", help="archive the wolts directory — all context and code (--full adds media)",
    )
    backup.add_argument("--tag", default="", help="archive tag (default: UTC timestamp)")
    backup.add_argument(
        "--full", action="store_true",
        help="also keep media, binaries and other large files (default: slim, and they are listed)",
    )
    backup.add_argument("--out", default="", help="where to write it (default: beside the wolts dir)")
    backup.add_argument("--wolts-dir", default="", help=argparse.SUPPRESS)
    backup.add_argument("--json", action="store_true")
    backup.set_defaults(func=_backup)

    restore = sub.add_parser("restore", help="extract a backup archive into a new directory")
    restore.add_argument("archive")
    restore.add_argument("--to", default="", help="target directory (must be new or empty)")
    restore.add_argument("--identity", default="", help="age key file for an encrypted archive (- for stdin)")
    restore.add_argument("--json", action="store_true")
    restore.set_defaults(func=_restore)

    db = sub.add_parser("db", help="read the lodge's state database")
    db_sub = db.add_subparsers(dest="db_command", required=True)
    db_show = db_sub.add_parser("show", help="print one table, read-only")
    db_show.add_argument("table")
    db_show.add_argument("--where", action="append", default=[], metavar="COL=VALUE",
                         help="a column, or record.<key>; repeat to narrow")
    db_show.add_argument("--json", action="store_true", help="one JSON object per row")
    db_show.set_defaults(func=_db_show)

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

    transcription = sub.add_parser(
        "transcription", help="show or choose the service that transcribes voice notes"
    )
    transcription.set_defaults(func=_transcription, transcription_parser=transcription)
    transcription_sub = transcription.add_subparsers(dest="verb")
    transcription_show = transcription_sub.add_parser("show", help="show the transcription settings")
    transcription_show.add_argument("--json", action="store_true")
    transcription_show.set_defaults(func=_transcription_show)
    transcription_set = transcription_sub.add_parser(
        "set", help="use any OpenAI-style /audio/transcriptions endpoint (a local server too)"
    )
    transcription_set.add_argument("--base-url", default=None,
                                   help="e.g. https://api.openai.com/v1, or http://127.0.0.1:8080/v1 "
                                        "for a server on this machine")
    transcription_set.add_argument("--model", default=None,
                                   help="the model name the server expects, e.g. gpt-4o-transcribe")
    key = transcription_set.add_mutually_exclusive_group()
    key.add_argument("--key-env", default=None,
                     help="environment variable holding the API key (default OPENAI_API_KEY)")
    key.add_argument("--no-key", action="store_true", help="the server needs no API key")
    transcription_set.set_defaults(func=_transcription_set)
    transcription_clear = transcription_sub.add_parser(
        "clear", help="go back to the default (OpenAI, gpt-4o-transcribe)"
    )
    transcription_clear.set_defaults(func=_transcription_clear)
    speech = sub.add_parser(
        "speech", help="show or choose the service that speaks a wolt's voice notes (notify --speak)"
    )
    speech.set_defaults(func=_speech, speech_parser=speech)
    speech_sub = speech.add_subparsers(dest="verb")
    speech_show = speech_sub.add_parser("show", help="show the speech settings")
    speech_show.add_argument("--json", action="store_true")
    speech_show.set_defaults(func=_speech_show)
    speech_set = speech_sub.add_parser(
        "set", help="use any OpenAI-style /audio/speech endpoint (a local server too)"
    )
    speech_set.add_argument("--base-url", default=None,
                            help="e.g. https://api.openai.com/v1, or http://127.0.0.1:8000/v1 "
                                 "for a server on this machine")
    speech_set.add_argument("--model", default=None,
                            help="the model name the server expects, e.g. gpt-4o-mini-tts")
    speech_set.add_argument("--voice", default=None,
                            help="the voice name the server expects, e.g. alloy")
    speech_key = speech_set.add_mutually_exclusive_group()
    speech_key.add_argument("--key-env", default=None,
                            help="environment variable holding the API key (default OPENAI_API_KEY)")
    speech_key.add_argument("--no-key", action="store_true", help="the server needs no API key")
    speech_set.set_defaults(func=_speech_set)
    speech_clear = speech_sub.add_parser(
        "clear", help="go back to the default (OpenAI, gpt-4o-mini-tts, alloy)"
    )
    speech_clear.set_defaults(func=_speech_clear)

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

    creature = sub.add_parser("creature", help="custom creatures: list, add, remove, assign to a wolt")
    creature.set_defaults(func=_creature, creature_parser=creature)
    creature_sub = creature.add_subparsers(dest="creature_command")

    creature_list = creature_sub.add_parser("list", help="the sizes and the lodge's custom creatures")
    creature_list.add_argument("--json", action="store_true")
    creature_list.set_defaults(func=_creature_list)

    creature_add = creature_sub.add_parser(
        "add", help="register a custom creature: its own emoji on one of the three sizes")
    creature_add.add_argument("name", help="lowercase id, e.g. star")
    creature_add.add_argument("--emoji", required=True)
    creature_add.add_argument("--size", required=True, choices=["otter", "beaver", "raccoon"],
                              help="what it behaves like")
    creature_add.add_argument("--model", action="append", metavar="HARNESS=MODEL",
                              help="its own model on a harness (repeatable)")
    creature_add.add_argument("--role", default="", help="default role for new wolts of this creature")
    creature_add.add_argument("--json", action="store_true")
    creature_add.set_defaults(func=_creature_add)

    creature_rm = creature_sub.add_parser("rm", help="remove a custom creature no wolt uses")
    creature_rm.add_argument("name")
    creature_rm.add_argument("--json", action="store_true")
    creature_rm.set_defaults(func=_creature_rm)

    creature_assign = creature_sub.add_parser(
        "assign", help="change a wolt's creature; applies from its next session")
    creature_assign.add_argument("wolt")
    creature_assign.add_argument("type", help="otter, beaver, raccoon, or a custom creature")
    creature_assign.add_argument("--json", action="store_true")
    creature_assign.set_defaults(func=_creature_assign)

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
