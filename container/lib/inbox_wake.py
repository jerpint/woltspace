"""
The inbox's waker: once mail has committed, see that the agent will look at it.

notify_admitted(wolts_dir, status) is the one post-commit port, for Telegram and
for the inbox's own send. Admission itself records, in the same write, the session
the sender is talking to (tried first) and the chat that hears how a human message
was answered (Store.admit's route). It never blocks, raises, claims or acknowledges: it
schedules one coalesced wake per agent on a background thread, so a burst is
one wake. A wake re-reads the agent's real mailbox and sessions under its
lifecycle lock, never the caller's seq or state:

- nothing queued, the mailbox not open, or the wake inhibited: nothing.
- a live session: a body-free hint naming the senders, sent after the lock is
  released, even while the wake is inhibited: an inhibition only ever stops a start.
  The hint goes the way the session's launch attempt recorded (inbox_adapter, chosen
  by inbox.native_hints at each real launch; a record without one but with a native
  locator is native, one with neither is unavailable), never another way:
  - paste (the default): once the agent takes input (_await_input_ready) and nobody
    has typed in the session for QUIET seconds (at most QUIET_CAP per pass; unknown
    activity waits the whole cap), the senders are read again and the hint is pasted
    with Enter by sessions.hint_live_agent, under the agent's lock, only into the same
    attempt's own pane. It can submit a human's unfinished draft; a running turn
    queues it. Not ready in time: unanswered, until a later wake.
  - native: native_adapters.deliver. A busy Claude or Codex engine gets the hint inside
    its running turn (a tool step's hook, a steer); any other busy engine (deliver says
    pending/busy) gets none: the owner hears it is busy. Either way the end of its turn
    (ready kind=turn_end) is a wake that hints once if mail is still queued.
  submitted, pending, unknown, unavailable and failed are transport facts; none changes
  custody, and none is retried here. Mail waits for the agent's own fetch and ack.
- no live session: all mail brings the wolt up (the owner's rule). The wolt's
  newest resting session is resumed without a prompt (the post-publication pickup
  and its backend's ready call hint it); with none, one fresh session, reserved
  by name first and started with the fixed fetch instruction. Either runs in the
  wolt's own recorded mode. A start's fetch instruction is that attempt's hint. The owner's explicit stop or archive of a session
  marks that record (wake_inhibit): that conversation is never auto-resumed, and
  nothing else. While any launch of the agent is unresolved, whoever made it,
  nothing starts and rearm refuses.

The owner gets exactly one line per human message, once a wake decides
(inbox_notice.outcome): hinted, resumed or started, or blocked or error, a crashed
wake included. Every message, an agent's too, gets that outcome recorded in the wake
state ("outcomes", the last 50); "answered" is the last message answered.

Inbox off: notify_admitted does nothing.
"""

from __future__ import annotations

import itertools
import logging
import re
import threading
import time
import uuid
from pathlib import Path

import inbox
import lifecycle
import sessions

log = logging.getLogger("inbox_wake")
START = ("You have lodge inbox mail{sender}. Fetch your inbox (`woltspace inbox fetch`). ACK each message once "
         "received into your context (`woltspace inbox ack <seq> <claim>`); ACK confirms receipt, not task "
         "completion. Release a claim if you cannot accept it (`woltspace inbox release <seq> <claim>`). "
         "Fetch again for the next message, until fetch returns nothing.")
HINT = ("New inbox mail{sender}. Fetch your inbox (`woltspace inbox fetch`). ACK each message once received into "
        "your context (`woltspace inbox ack <seq> <claim>`); ACK confirms receipt, not task completion. "
        "Release a claim if you cannot accept it (`woltspace inbox release <seq> <claim>`). Fetch again "
        "for the next message (nonce {nonce}).")
BUSY_RETRIES, BUSY_PAUSE = 5, 2.0
QUIET, QUIET_CAP = 2.0, 5.0  # paste mode: seconds with no keystroke; the most one pass defers for typing
SENDERS = 3  # names in a hint; more are counted
OUTCOMES = 50  # per-message outcomes kept in the wake state
_GUARD = threading.Lock()
_PENDING: dict[tuple[str, str], bool] = {}  # (root, wolt) -> another wake was asked for meanwhile


def notify_admitted(wolts_dir, status) -> None:
    """Called after Store admission committed. Never raises."""
    try:
        if status.state != "acknowledged" and inbox.enabled(wolts_dir):
            _schedule(str(wolts_dir), status.to)
    except Exception:
        log.exception("inbox wake not scheduled")


def ready(wolts_dir, token: str, session: str, attempt: str, kind: str = "ready") -> bool:
    """A native backend announced its attempt ready, or the end of a turn
    (native_adapters.announce_ready). Only the session's own reader, for its current,
    published, running attempt, schedules: on ready a pickup (one hint if this attempt never
    had one); on turn_end a plain wake, which hints once if mail is still queued (mail a busy
    session was not hinted for, and none for mail it took during the turn). False for
    anything stale or not its own."""
    if not inbox.enabled(wolts_dir) or kind not in ("ready", "turn_end"):
        return False
    owner = inbox.Store(wolts_dir, inbox.LIMITS).reader_session(token)
    record = sessions.SessionRegistry(wolts_dir).get(session, check_alive=False) or {}
    if (owner is None or owner[0] != session or record.get("wolt") != owner[1]
            or record.get("status") != "running" or record.get("launching") or record.get("attempt") != attempt):
        return False
    _schedule(str(wolts_dir), owner[1], once=kind == "ready")
    return True


def schedule_pickup(wolts_dir, wolt: str) -> None:
    """A resume that found the agent already running: its backend was ready before, so no
    ready call will come. One pickup hint for the attempt if it never had one."""
    if wolt and inbox.enabled(wolts_dir):
        _schedule(str(wolts_dir), wolt, once=True)


def reconcile(wolts_dir) -> None:
    """At boot: mail admitted just before a crash never got its wake."""
    if inbox.enabled(wolts_dir):
        for wolt in inbox.Store(wolts_dir, inbox.LIMITS).queued_wolts():
            _schedule(str(wolts_dir), wolt)


def rearm(wolts_dir, wolt: str) -> dict:
    """The operator's explicit clear of the wake state, then a wake. Refused while any
    of the agent's launches is unresolved. An owner's stop mark stays: that
    conversation is never auto-resumed. A name that is not a wolt is refused before any lock."""
    import wolt_archive
    try:
        wolt_archive.check_wolt_name(wolt)
    except wolt_archive.ArchiveError:
        raise inbox.Refused("unknown_wolt") from None
    if not (Path(wolts_dir) / wolt / "wolt" / "wolt.json").is_file():
        raise inbox.Refused("unknown_wolt")
    with lifecycle.agent_lock(wolts_dir, wolt):
        store = inbox.Store(wolts_dir, inbox.LIMITS)
        facts = store.wake_facts(wolt)
        if facts is None:
            raise inbox.Refused("no_mailbox")
        registry = sessions.SessionRegistry(wolts_dir)
        for record in registry.records(wolt=wolt):  # any unresolved launch of the agent refuses it
            sessions.refuse_unresolved(record)
        store.set_wake(wolt, _kept(facts["wake"]))
    _schedule(str(wolts_dir), wolt)
    return {"wolt": wolt, "rearmed": True}


def _schedule(root: str, wolt: str, once: bool = False) -> None:
    """One worker per agent; what arrives meanwhile is one more pass ("all" outranks "once")."""
    key = (root, wolt)
    with _GUARD:
        if key in _PENDING:
            _PENDING[key] = "all" if not once or _PENDING[key] == "all" else "once"
            return
        _PENDING[key] = None
    threading.Thread(target=_worker, args=(root, wolt, once), daemon=True, name=f"inbox-wake-{wolt}").start()


def _worker(root: str, wolt: str, once: bool = False) -> None:
    key, busy = (root, wolt), 0
    while True:
        try:
            _wake(root, wolt, once=once)
        except lifecycle.LifecycleBusy:
            busy += 1
            if busy < BUSY_RETRIES:
                time.sleep(BUSY_PAUSE)
                continue
            log.warning("inbox wake for %s skipped: lifecycle busy", wolt)
            _failed(root, wolt, "busy")
        except Exception:
            log.exception("inbox wake for %s failed; the mail stays queued", wolt)
            _failed(root, wolt, "error")
        with _GUARD:
            again = _PENDING[key]
            if again is None:
                del _PENDING[key]
                return
            _PENDING[key], busy, once = None, 0, again == "once"


def _wake(root: str, wolt: str, *, once: bool = False) -> None:
    """once: hint a live session only if its current attempt has had none yet."""
    store, registry = inbox.Store(root, inbox.LIMITS), sessions.SessionRegistry(root)
    hinted = None
    with lifecycle.agent_lock(root, wolt):
        facts = store.wake_facts(wolt)
        if not facts or not facts["open"] or not facts["queued"]:
            taken = bool(facts and facts["unanswered"])  # a reader took them before any wake did
        else:
            # current while its own message waits unfetched: until then every wake nudges only that
            # session, if it is live or resumable; another may still fetch it, unnudged
            taken = False
            prefer = (facts["wake"].get("prefer") or {}).get("session") if facts["prefer_queued"] else None
            # the session the sender is talking to first, live or resumable; else the newest live one
            live = _live(registry, wolt, prefer)
            if not live and not _resumable(registry.get(prefer, wolt=wolt, check_alive=False) if prefer else None):
                live = sessions.resolve_active_session(wolt, registry)
            # All mail brings the wolt up (the owner's rule): with no live session it resumes or
            # starts one; only an unresolved launch holds it (in _start). A live session is hinted.
            how, what = (None, None) if live else _start(store, registry, wolt, facts, prefer)
            if how == "started":  # its START prompt is the published attempt's hint: its pickup adds none
                started = registry.get(what, wolt=wolt, check_alive=False) or {}
                if started.get("attempt") and not started.get("launching"):
                    hinted = {"session": what, "attempt": started["attempt"]}
            if live:
                record = registry.get(live, wolt=wolt, check_alive=False) or {}
                hinted = {"session": live, "attempt": record.get("attempt")}
                if once and facts["wake"].get("hinted") == hinted:
                    return
    if taken or not facts or not facts["open"] or not facts["queued"]:
        if taken:
            _answer(root, wolt)
        return
    if how == "resume":  # outside the lock: resume relaunches under its own lock and waits after it
        try:
            # no prompt: nothing is pasted; the pickup hints. only_resting: an owner's stop or
            # archive that landed after the choice wins at the relaunch itself
            sessions.resume_session(what, "", only_resting=True)
            how = "resumed"
        except Exception as exc:  # the reservation lets the next wake judge it
            log.warning("inbox wake could not resume %s: %s", what, exc)
            _judge_failed_resume(root, registry, wolt, what)
            how, what = "error", "resume_failed"
    if not live:
        if how in ("blocked", "error"):  # neither a hint nor a start: outside the lock and any Store write
            _notice(root, wolt, what)
            _answer(root, wolt, how, reason=what)
        elif how:
            _answer(root, wolt, how, session=what, hinted=hinted)
        return
    hint = HINT.format(sender=_sender(facts), nonce=uuid.uuid4().hex[:8])
    fresh = registry.get(live, wolt=wolt, check_alive=False)  # the fence: re-read just before placing
    if not fresh or any(fresh.get(k) != record.get(k) for k in ("attempt", "native", "launching", "inbox_adapter")):
        log.info("inbox hint for %s via %s: not placed, the session changed since the wake", wolt, live)
        return  # a wait, unanswered: its new attempt's ready pickup hints
    status, code = _paste(store, registry, wolt, fresh) if _mode(fresh) == "paste" else _deliver(fresh, hint)
    if status is None or code == "taken":  # changed under the lock; or a reader took the mail meanwhile
        log.info("inbox hint for %s via %s: not placed (%s)", wolt, live, code)
        if code == "taken":
            _answer(root, wolt)
        return
    log.info("inbox hint for %s via %s: %s (%s)", wolt, live, status, code)
    if status == "submitted" or (status, code) == ("pending", "placed"):  # only a hint that reached the engine
        _answer(root, wolt, "hinted", session=live, hinted=hinted)
    elif (status, code) == ("pending", "busy"):  # its turn end's wake hints, if mail is still queued
        _answer(root, wolt, "busy", session=live)
    elif status != "pending":
        _answer(root, wolt, "blocked", reason=f"hint_{status}")
    # pending, not placed: a wait, unanswered; the hint of its ready pickup answers it


def _mode(record: dict) -> str | None:
    """The transport this attempt recorded; a legacy record with a locator is native."""
    mode = record.get("inbox_adapter")
    return mode if mode in ("paste", "native") else "native" if record.get("native") else None


def _paste(store, registry, wolt: str, record: dict) -> tuple[str | None, str]:
    """Paste mode: wait for input and for quiet, outside every lock; then re-read who the
    mail is from and hand the hint to sessions.hint_live_agent, which checks the session
    again under the locks."""
    if sessions._await_input_ready(record, sessions.resolve_harness(record.get("harness"))) is None:
        return "pending", "not_ready"
    _quiet(sessions.RuntimeHandle.from_record(record).tmux_session_name)
    facts = store.wake_facts(wolt)  # the last chance to read it, before the paste
    if not facts or not facts["open"] or not facts["queued"]:
        return "pending", "taken"
    return sessions.hint_live_agent(registry, record, HINT.format(sender=_sender(facts), nonce=uuid.uuid4().hex[:8]))


def _quiet(tmux_name: str, clock=time.monotonic, now=time.time, sleep=time.sleep) -> None:
    """Return once tmux says nobody typed in the session for QUIET seconds, or at QUIET_CAP from
    now, one deadline: keys meanwhile never extend it. Unknown activity (no answer, no such
    session) waits; each probe gets only the time left. session_activity is coarse: it counts
    a client attaching, and an idle composer may still hold a draft."""
    deadline = clock() + QUIET_CAP
    while (left := deadline - clock()) > 0:
        try:
            stamp = sessions._runtime().last_keys(timeout=max(0.1, left)).get(tmux_name)
        except Exception:
            stamp = None
        if stamp is not None and now() - stamp >= QUIET:
            return
        sleep(max(0.0, min(0.5, deadline - clock())))


def _sender(facts: dict) -> str:
    """" from your human, pal-a": who the queued mail is from, never what it says. The owner
    always first and never counted in "and N more"; other names only in the wolt-name charset."""
    names = [re.sub(r"[^A-Za-z0-9_-]", "-", name)[:64] for name in facts.get("senders") or []]
    more = f" and {len(names) - SENDERS} more" if len(names) > SENDERS else ""
    names = (["your human"] if facts.get("human") else []) + names[:SENDERS]
    return f" from {', '.join(names)}{more}" if names else ""


def _resumable(record: dict | None) -> bool:
    return bool(record and record.get("status") == "resting" and sessions.stored_resume_id(record)
                and not record.get("wake_inhibit")  # a conversation the owner stopped is never auto-resumed
                and not record.get("resume_failed"))  # nor one whose resume failed for certain


def _live(registry, wolt: str, name: str | None) -> str | None:
    record = registry.get(name, wolt=wolt) if name else None
    return name if record and record.get("status") == "running" and record.get("alive") else None


def _start(store, registry, wolt: str, facts: dict, prefer: str | None) -> tuple[str | None, str | None]:
    """No live session: wait for an unresolved launch or a reserved session still
    settling, then reserve an unmarked resting session to resume (the preferred one,
    else the newest), or start one, in the wolt's own mode. Returns (how, what):
    ("blocked", launch_unknown or not_startable), ("resume", the session to resume
    after the lock), ("started", its name), or (None, None) while it waits."""
    wake, reserved = facts["wake"], facts["wake"].get("reserved")
    records = registry.records(wolt=wolt)
    if any(r.get("launching") for r in records):
        return "blocked", "launch_unknown"  # an unresolved launch of this agent, whoever made it, may still start
    resting = [r for r in records if _resumable(r)]
    if reserved and prefer not in {r["name"] for r in resting}:  # the preferred one never waits
        record = registry.get(reserved["session"], wolt=wolt, check_alive=False) or {}
        if record.get("status") in ("running", sessions.STOPPING):
            return None, None  # still settling: kept as it is
    keep = _kept(wake)
    if resting:  # its own conversation, in its own mode: resumed after the lock, without a prompt
        name = (prefer if prefer in {r["name"] for r in resting}
                else max(resting, key=lambda r: r.get("last_activity") or 0)["name"])
        store.set_wake(wolt, {**keep, "reserved": {"session": name, "at": int(time.time())}})
        return "resume", name
    name = sessions.session_name(wolt)
    store.set_wake(wolt, {**keep, "reserved": {"session": name, "at": int(time.time())}})
    chats = wake.get("chats", {})  # owner mail from a chat: the session answers there, as before the inbox
    owner = [chats[str(m["seq"])] for m in facts["unanswered"] if m["human"] and chats.get(str(m["seq"]))]
    routing = _routing(store.root, owner[-1]) if owner else None
    try:  # any mail starts the wolt in its own recorded mode, as Telegram did before the inbox
        sessions.start_session(wolt=wolt, prompt=START.format(sender=_sender(facts)), name=name, **({"routing": routing} if routing else {}))
    except (PermissionError, ValueError, sessions.HarnessNotInstalled) as exc:
        return "blocked", _inhibit(store, wolt, {**keep, "detail": str(exc)[:200]}, "not_startable")
    return "started", name


def _routing(root, chat: dict) -> dict | None:
    """The routing chat_lodge gave a session it started for a chat: the connector and the
    chat's own route, so notify's exact binding keeps its reply footer."""
    try:
        import connector_registry
        return {"adapter": chat["adapter"],
                **connector_registry.spec(chat["adapter"], root).codec.canonical(str(chat["chat"]))}
    except Exception:
        log.exception("inbox wake: no routing for a %s chat", chat.get("adapter"))
        return None


def _judge_failed_resume(root, registry, wolt: str, name: str) -> None:
    """A resume that failed DEFINITELY (nothing launched: the session is still resting,
    or its new tmux was refused) marks that conversation not auto-resumable, so the next
    wake resumes another or starts one. An UNKNOWN outcome (a launch still marked, or one
    that started) is left alone: it holds, with no second worker."""
    with lifecycle.agent_lock(root, wolt):
        if any(r.get("launching") for r in registry.records(wolt=wolt)):
            return
        with registry.edit(name, wolt=wolt) as current:
            if current and current.get("status") in ("resting", "stopped"):
                current["resume_failed"] = True


def _kept(wake: dict) -> dict:
    """What a reset of the wake state keeps: the answered mail and its outcomes (Store.set_wake
    keeps what admission recorded)."""
    return {k: wake[k] for k in ("answered", "outcomes") if k in wake}


def _answer(root, wolt: str, how: str | None = None, *, session=None, reason=None, hinted=None) -> None:
    """Every message no wake answered yet is answered now (one a reader already took: "hinted"
    to that reader's session) and recorded in the wake state's outcomes, under the lock; then
    the owner gets exactly one line per human message (inbox_notice.outcome), never retried,
    each on the chat it came from. how None: only those a reader
    already took. A human message waits until its admission's route is recorded. hinted: also
    record the hint this attempt had. Never raises."""
    try:
        store = inbox.Store(root, inbox.LIMITS)
        with lifecycle.agent_lock(root, wolt):
            facts = store.wake_facts(wolt)
            if not facts:
                return
            wake = {**facts["wake"], **({"hinted": hinted} if hinted else {})}
            chats = dict(wake.get("chats", {}))
            mail = list(itertools.takewhile(lambda m: (how or m["session"]) and (
                not m["human"] or str(m["seq"]) in chats), facts["unanswered"]))
            done = [("hinted", m["session"], None) if m["session"] else (how, session, reason) for m in mail]
            if mail:
                wake["answered"] = mail[-1]["seq"]
                wake["outcomes"] = (wake.get("outcomes", []) + [  # the last OUTCOMES, for a sender who waits
                    {"seq": m["seq"], "how": h, **({"session": s} if s else {"reason": r})}
                    for m, (h, s, r) in zip(mail, done)])[-OUTCOMES:]
            if mail or hinted:
                store.set_wake(wolt, wake)
        human = [(m["seq"], (*d, chats.get(str(m["seq"])))) for m, d in zip(mail, done) if m["human"]]
        if human:
            import inbox_notice
            for (h, sess, r, chat), group in itertools.groupby(human, lambda pair: pair[1]):
                inbox_notice.outcome(root, wolt, seqs=[seq for seq, _ in group], how=h, session=sess,
                                     reason=r, chat=chat)
    except Exception:
        log.exception("inbox answer for %s failed", wolt)


def _failed(root, wolt: str, reason: str) -> None:
    """A wake that ended in an exception still answers: the senders and the owner are told."""
    _notice(root, wolt, reason)
    _answer(root, wolt, "error", reason=reason)


def _inhibit(store, wolt: str, wake: dict, reason: str) -> str:
    store.set_wake(wolt, {**wake, "inhibited": reason})
    return reason


def _notice(root, wolt: str, reason: str) -> None:
    """Mail cannot wake the wolt: inbox_notice tells the agents whose mail waits (the
    owner's line is _answer's). Never raises."""
    try:
        import inbox_notice
        inbox_notice.notice(root, wolt, reason, owner=False)
    except Exception:
        log.exception("inbox sender notice for %s failed", wolt)


def _deliver(record: dict | None, hint: str) -> tuple[str, str]:
    try:
        import native_adapters
    except ImportError:
        return "unavailable", "no_adapters"
    return native_adapters.deliver(record or {}, hint)
