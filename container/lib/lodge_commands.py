"""The owner's chat commands, once for every connector (RFC #617).

Each command is implemented here and answers with a neutral Reply. A
connector decides how commands reach the lodge (a native menu, typed text,
buttons) and how replies look (buttons, a numbered list). Selections are the
actions behind a reply's choices: "wolt:pal", "sstop:<session>", ...

The lodge's state and services reach this module through `ports`, an object
the connector builds (see `Ports`). Owner checks happen before anything here
runs: every function assumes the caller is the owner.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from connector_contract import Ack, Choice, Outcome, Reply
from creature_types import base_type, emoji_from

WOLT_TYPE_EMOJI = {
    "raccoon": "🦝",
    "beaver": "🦫",
    "otter": "🦦",
    "rodent": "🦫",
    "wolf": "🐺",
    "dog": "🐶",
}
# Wolves and dogs are singletons, not chat targets: pickers leave them out.
RODENT_WOLT_TYPES = {"raccoon", "beaver", "otter", "rodent"}
NO_WOLTS_MESSAGE = "no wolts found. create a wolt in the lodge first."

# The lodge's idle window: the same four choices as the Settings page.
IDLE_CHOICES = ((None, "Never"), (3600, "1 hour"), (14400, "4 hours"), (86400, "1 day"))
IDLE_LABELS = dict(IDLE_CHOICES)


@dataclass
class Ports:
    """What the commands need from the lodge, supplied by the connector.

    Plain callables, so a connector can hand in its own module's functions and
    tests can fake any one of them.
    """

    list_wolts: Callable[[], list]
    list_sessions: Callable[[], list]
    tunnel_url: Callable[[], str | None]
    wolt_exists: Callable[[str], bool]
    shown: Callable[[str], str]
    session_emoji: Callable[[str], str]
    load_chat: Callable[[Any], dict]
    save_chat: Callable[[Any, dict], None]
    forget_session: Callable[[str], None]
    kill_session: Callable[[str], bool]
    spawn_session: Callable[[str, Any], dict]
    dog_available: Callable[[], bool]
    dog_name: Callable[[], str]
    fetch_apps: Callable[[], Awaitable[list]]
    app_action: Callable[[str, str], Awaitable[bool]]
    release_hold: Callable[[str], Awaitable[bool]]
    idle_setting: Callable[..., Awaitable[tuple[int, dict]]]
    log_error: Callable[[str], None] = lambda message: None


@dataclass(frozen=True)
class CommandSpec:
    name: str
    description: str  # the one line a native menu shows
    help: tuple[str, ...] = ()  # lines for /help, in HELP_ORDER


# The registry, in the order a native menu lists it.
COMMANDS = (
    CommandSpec("sessions", "list active sessions", ("/sessions — list active sessions with links",)),
    CommandSpec("holds", "list sessions kept up by a hold",
                ("/holds — sessions kept up by a hold, with release buttons",)),
    CommandSpec("idle", "how long idle sessions stay up",
                ("/idle — how long idle sessions stay up (lodge-wide)",)),
    CommandSpec("wolt", "switch or show the active wolt",
                ("/wolt — show active wolt and available wolts", "/wolt <name> — switch active wolt")),
    CommandSpec("new", "start a fresh session",
                ("/new — start a fresh session with the active wolt",
                 "/new <name> — start a fresh session with a specific wolt")),
    CommandSpec("apps", "list apps with public links",
                ("/apps — list all apps with status and public links",)),
    CommandSpec("kill", "stop a session by slug", ("/kill <session> — kill a session",)),
    CommandSpec("help", "show commands and messaging cheatsheet", ("/help — this message",)),
    CommandSpec("start", "onboard and show current state"),
)
COMMAND_NAMES = tuple(c.name for c in COMMANDS)
HELP_ORDER = ("new", "wolt", "sessions", "holds", "idle", "kill", "apps", "help")


# --- shared helpers ----------------------------------------------------------

def wolt_name(w: dict) -> str:
    return w.get("name") or Path(w.get("dir", "")).name


def is_chat_wolt(w: dict) -> bool:
    return base_type(w.get("type", "")) in RODENT_WOLT_TYPES


def wolt_of(session_name: str) -> str:
    """The wolt name from a session slug: {wolt}-{word}-{word}-{hex}."""
    parts = session_name.rsplit("-", 3)
    return parts[0] if len(parts) == 4 else session_name


def clock(epoch: int) -> str:
    """17:20 today, or "Oct 18 17:20" on another day (the lodge's local time)."""
    when = datetime.fromtimestamp(epoch)
    if when.date() == datetime.now().date():
        return when.strftime("%H:%M")
    return f"{when.strftime('%b')} {when.day} {when.strftime('%H:%M')}"


def _rows(buttons: list[Choice], per_row: int) -> tuple[tuple[Choice, ...], ...]:
    rows = [tuple(buttons[i:i + per_row]) for i in range(0, len(buttons), per_row)]
    return tuple(rows)


def _session_link(tunnel_url: str | None, name: str) -> str | None:
    return f"{tunnel_url}/tui?session={name}" if tunnel_url else None


# --- reply builders ----------------------------------------------------------

def wolt_picker(ports: Ports, wolts: list, active: str | None) -> Reply:
    """Header naming the active wolt, and a two-per-row grid of the others."""
    if active:
        active_type = next((w.get("type", "") for w in wolts if wolt_name(w) == active), "")
        line = f"active: {emoji_from(WOLT_TYPE_EMOJI, active_type, '🦫')} {ports.shown(active)}"
    else:
        line = "active: none"
    others = [w for w in wolts if wolt_name(w) != active]
    text = f"{line}\n\ntap to switch" if others else f"{line}\n\nno other wolts yet."
    return Reply(text, wolt_picker_choices(ports, wolts, active))


def wolt_picker_choices(ports: Ports, wolts: list, active: str | None):
    buttons = []
    for w in wolts:
        name = wolt_name(w)
        if not name or name == active:
            continue
        emoji = emoji_from(WOLT_TYPE_EMOJI, w.get("type", ""), "🦫")
        buttons.append(Choice(f"{emoji} {ports.shown(name)}", action=f"wolt:{name}"))
    return _rows(buttons, 2)


def sessions_picker(ports: Ports, sessions: list, tunnel_url: str | None) -> Reply:
    """The /sessions list: most recently active first, each with open and stop."""
    alive = [s for s in sessions if s.get("alive")]
    if not alive:
        return Reply("no active sessions.", (), markdown=True)
    alive.sort(key=lambda s: s.get("last_activity") or s.get("started") or 0, reverse=True)
    types_by_wolt = {wolt_name(w): w.get("type", "") for w in ports.list_wolts()}
    counts: dict[str, int] = {}
    rows = []
    for s in alive:
        name = s["name"]
        wolt = wolt_of(name)
        emoji = emoji_from(WOLT_TYPE_EMOJI, types_by_wolt.get(wolt, ""), "🦫")
        counts[wolt] = counts.get(wolt, 0) + 1
        url = _session_link(tunnel_url, name)
        label = f"{emoji} 📌 {name}" if s.get("hold") else f"{emoji} {name}"
        opener = Choice(label, url=url) if url else Choice(label, action=f"noop:{name}")
        rows.append((opener, Choice("⏹", action=f"sstop:{name}")))
    summary = " · ".join(
        f"{emoji_from(WOLT_TYPE_EMOJI, types_by_wolt.get(w, ''), '🦫')} {w} ({c})"
        for w, c in sorted(counts.items(), key=lambda kv: -kv[1])
    )
    total = len(alive)
    return Reply(f"**{total} active session{'s' if total != 1 else ''}**\n{summary}",
                 tuple(rows), markdown=True)


def holds_list(ports: Ports, sessions: list, tunnel_url: str | None) -> Reply:
    """Every running session a hold keeps up, oldest hold first.

    Plain text, no markdown: a reason is the wolt's own words.
    """
    held = [s for s in sessions if s.get("hold") and s.get("alive")]
    if not held:
        return Reply("no held sessions.", ())
    held.sort(key=lambda s: s["hold"].get("since") or 0)
    lines = [f"📌 {len(held)} held session{'s' if len(held) != 1 else ''}"]
    rows = []
    for s in held:
        name, hold = s["name"], s["hold"]
        emoji = ports.session_emoji(name)
        span = "forever" if hold.get("until") is None else f"until {clock(hold['until'])}"
        since = f" · since {clock(hold['since'])}" if hold.get("since") else ""
        lines.append(f"{emoji} {name} · {span} · {hold.get('reason', '')}{since}")
        url = _session_link(tunnel_url, name)
        label = f"{emoji} {name}"
        opener = Choice(label, url=url) if url else Choice(label, action=f"noop:{name}")
        rows.append((opener, Choice("🔓", action=f"hrel:{name}")))
    return Reply("\n".join(lines), tuple(rows))


def apps_list(apps: list) -> Reply:
    """/apps: running first, alphabetical within each group."""
    if not apps:
        return Reply("no apps yet.", (), markdown=True)
    ordered = sorted(apps, key=lambda a: (not a.get("running"), a.get("name", "")))
    running = sum(1 for a in apps if a.get("running"))
    rows = []
    for a in ordered:
        name = a.get("name", "?")
        emoji = a.get("emoji") or "📦"
        on = bool(a.get("running"))
        tunnel = a.get("tunnel_url")
        label = f"{emoji} {name}" if on else f"{emoji} {name} (off)"
        primary = Choice(label, url=tunnel) if on and tunnel else Choice(label, action=f"noop:{name}")
        toggle = Choice("⏹", action=f"astop:{name}") if on else Choice("▶", action=f"astart:{name}")
        rows.append((primary, toggle))
    total = len(apps)
    return Reply(f"**{total} app{'s' if total != 1 else ''}** · {running} running",
                 tuple(rows), markdown=True)


def idle_text(value, mode: str = "is") -> str:
    """mode "is" answers /idle, "now" a change, "still" a refused change."""
    if value is None:
        return {"is": "Idle sessions stay open (whole lodge).",
                "now": "Idle sessions now stay open.",
                "still": "Idle sessions still stay open."}[mode]
    label = IDLE_LABELS.get(value, f"{value} seconds")
    return {"is": f"Idle sessions rest after {label} (whole lodge).",
            "now": f"Idle sessions now rest after {label}.",
            "still": f"Idle sessions still rest after {label}."}[mode]


def idle_choices(current):
    return ((tuple(
        Choice(f"✓ {label}" if value == current else label,
               action=f"idle:{'never' if value is None else value}")
        for value, label in IDLE_CHOICES
    )),)


def help_text(state: dict, tips: tuple[str, ...] = ()) -> str:
    by_name = {c.name: c for c in COMMANDS}
    lines = ["📋 Commands:", ""]
    for name in HELP_ORDER:
        lines.extend(by_name[name].help)
    if tips:
        lines += ["", "💬 Messaging:", *tips]
    lines += [
        "",
        f"Active wolt: {state.get('active_wolt', 'none')}",
        f"Active session: {state.get('active_session', 'none')}",
    ]
    return "\n".join(lines)


def set_active_wolt(ports: Ports, chat, name: str) -> None:
    state = ports.load_chat(chat)
    state["active_wolt"] = name
    state["active_session"] = None  # the next message starts a session
    ports.save_chat(chat, state)


def chat_wolts(ports: Ports) -> list:
    return [w for w in ports.list_wolts() if is_chat_wolt(w)]


# --- commands ----------------------------------------------------------------

async def run_command(ports: Ports, chat, name: str, args=(), tips: tuple[str, ...] = ()) -> Reply:
    """Run one owner command for `chat` and return what to say."""
    args = list(args)
    if name == "start":
        if ports.dog_available():
            return Reply(f"Hey. I'm {ports.dog_name()}. Talk to me and I'll connect you to a wolt.")
        return Reply("Hey. Send a message and it goes to your wolt. /wolt picks which one.")

    if name == "help":
        return Reply(help_text(ports.load_chat(chat), tips))

    if name == "wolt":
        if args:
            wolt = args[0]
            if not ports.wolt_exists(wolt):
                return Reply(f"no wolt named '{wolt}' found.")
            set_active_wolt(ports, chat, wolt)
            return Reply(f"active wolt set to {ports.shown(wolt)}. next message starts a session.")
        active = ports.load_chat(chat).get("active_wolt")
        wolts = chat_wolts(ports)
        if not wolts:
            return Reply(NO_WOLTS_MESSAGE)
        return wolt_picker(ports, wolts, active)

    if name == "new":
        state = ports.load_chat(chat)
        if args:
            wolt = args[0]
            if not ports.wolt_exists(wolt):
                return Reply(f"No wolt named '{wolt}' found.")
            state["active_wolt"] = wolt
        if not state.get("active_wolt"):
            return Reply("No active wolt. Use /wolt <name> first.")
        wolt = state["active_wolt"]
        try:
            session = ports.spawn_session(wolt, chat)
            state["active_session"] = session["name"]
            ports.save_chat(chat, state)
            tunnel_url = ports.tunnel_url()
            link = f"{tunnel_url}/tui?session={session['name']}" if tunnel_url else session["name"]
            return Reply(f"🪵 new session for {ports.shown(wolt)}\n{link}")
        except Exception as e:
            ports.log_error(f"Failed to spawn session for {wolt}: {e}")
            return Reply(f"couldn't start session for {ports.shown(wolt)}: {e}")

    if name == "sessions":
        return sessions_picker(ports, ports.list_sessions(), ports.tunnel_url())

    if name == "holds":
        return holds_list(ports, ports.list_sessions(), ports.tunnel_url())

    if name == "kill":
        if not args:
            return Reply("Usage: /kill <session-name>")
        target = args[0]
        if ports.kill_session(target):
            ports.forget_session(target)
            return Reply(f"Killed {target}.")
        return Reply(f"Couldn't kill {target}.")

    if name == "apps":
        try:
            apps = await ports.fetch_apps()
        except Exception as e:
            return Reply(f"couldn't fetch apps: {e}")
        return apps_list(apps)

    if name == "idle":
        try:
            status, data = await ports.idle_setting()
            if status >= 400:
                raise RuntimeError(data.get("error") or f"HTTP {status}")
        except Exception as e:
            return Reply(f"couldn't read the idle setting: {e}")
        value = data.get("idle_timeout_seconds")
        return Reply(idle_text(value), idle_choices(value))

    raise KeyError(name)


# --- selections --------------------------------------------------------------

async def _apps_body(ports: Ports) -> Reply:
    try:
        apps = await ports.fetch_apps()
    except Exception as e:
        return Reply(f"couldn't refresh apps: {e}")
    return apps_list(apps)


def _sessions_body(ports: Ports) -> Reply:
    return sessions_picker(ports, ports.list_sessions(), ports.tunnel_url())


def _holds_body(ports: Ports) -> Reply:
    return holds_list(ports, ports.list_sessions(), ports.tunnel_url())


async def _pick_idle(ports: Ports, choice: str) -> Outcome:
    value = None if choice == "never" else int(choice) if choice.isdigit() else -1
    if value not in IDLE_LABELS:
        return Outcome(Ack("unknown choice", alert=True))
    try:
        status, data = await ports.idle_setting({"idle_timeout_seconds": value})
        error = None if status < 400 else (data.get("error") or f"HTTP {status}")
    except Exception as e:
        error = str(e) or type(e).__name__
    if error is None:
        saved = data.get("idle_timeout_seconds", value)
        return Outcome(Ack(), Reply(idle_text(saved, "now"), idle_choices(saved)))
    # The setting is unchanged: say what it still is, if the lodge can tell.
    body = Reply(f"couldn't change it: {error}", ())
    try:
        status, data = await ports.idle_setting()
        if status < 400:
            still = data.get("idle_timeout_seconds")
            body = Reply(f"{body.text}\n{idle_text(still, 'still')}", idle_choices(still))
    except Exception:
        pass
    return Outcome(Ack("not changed", alert=True), body)


async def run_selection(ports: Ports, chat, action: str) -> Outcome:
    """Run the action behind a choice the owner picked."""
    kind, _, name = action.partition(":")

    if kind == "astart":
        ok = await ports.app_action(name, "start")
        return Outcome(Ack(f"{'started' if ok else 'failed to start'} {name}", alert=not ok),
                       await _apps_body(ports))
    if kind == "astop":
        return Outcome(Ack(), Reply(f"stop app `{name}`?", ((
            Choice("✓ stop", action=f"astop-confirm:{name}"),
            Choice("cancel", action="astop-cancel"),
        ),), markdown=True))
    if kind == "astop-confirm":
        ok = await ports.app_action(name, "stop")
        return Outcome(Ack(f"{'stopped' if ok else 'failed to stop'} {name}", alert=not ok),
                       await _apps_body(ports))
    if action == "astop-cancel":
        return Outcome(Ack(), await _apps_body(ports))

    if kind == "idle":
        return await _pick_idle(ports, name)

    if kind == "hrel":
        return Outcome(Ack(), Reply(
            f"release the hold on {ports.session_emoji(name)} {name}? "
            f"It rests at the next sweep if nobody is using it.",
            ((Choice("✓ release", action=f"hrel-confirm:{name}"),
              Choice("cancel", action="hrel-cancel")),),
        ))
    if kind == "hrel-confirm":
        if await ports.release_hold(name):
            ack = Ack(f"released {name}")
        else:
            ack = Ack("failed — the session may be gone", alert=True)
        return Outcome(ack, _holds_body(ports))
    if action == "hrel-cancel":
        return Outcome(Ack(), _holds_body(ports))

    if kind == "sstop":
        return Outcome(Ack(), Reply(f"stop {ports.session_emoji(name)} `{name}`?", ((
            Choice("✓ stop", action=f"sstop-confirm:{name}"),
            Choice("cancel", action="sstop-cancel"),
        ),), markdown=True))
    if kind == "sstop-confirm":
        if ports.kill_session(name):
            ack = Ack(f"stopped {name}")
            ports.forget_session(name)
        else:
            ack = Ack("failed — session may already be gone", alert=True)
        return Outcome(ack, _sessions_body(ports))
    if action == "sstop-cancel":
        return Outcome(Ack(), _sessions_body(ports))

    if kind == "noop":
        return Outcome(Ack())

    if kind == "wolt":
        if not ports.wolt_exists(name):
            return Outcome(Ack("that wolt no longer exists", alert=True))
        set_active_wolt(ports, chat, name)
        wolts = chat_wolts(ports)
        return Outcome(Ack(f"switched to {name}"), wolt_picker(ports, wolts, name))

    return Outcome(Ack())
