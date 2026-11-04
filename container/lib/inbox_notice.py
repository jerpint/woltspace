"""Tell whoever sent mail to an agent that cannot wake.

notice(wolts_dir, wolt, reason, owner=...) is the waker's port, called when a wake
ends with neither a hint nor a start. The owner hears through the lodge's own
outbound path (POST /notify with no session: their default channel) while human
mail is queued, when the waker's reservation says so. Each agent that sent queued
mail gets one notice per message and reason in its own inbox, from the lodge at
agent priority, and its normal hint: the notice's key is the dedup, so a repeat
wake adds nothing. Fixed lines, never a message body. No state of its own, no
retry, never raises.
"""

from __future__ import annotations

import json
import logging
import urllib.request

import inbox
from env_compat import get_env

log = logging.getLogger("inbox_notice")
NOTIFY = "/notify"  # the lodge's own outbound path, on its API

# Why a wake is inhibited, in the owner's words.
REASONS = {
    "not_startable": "it can't be started automatically",
    "stopped": "one of its sessions was stopped, so it stays asleep until rearmed",
    "no_progress": "its last wake didn't pick the mail up",
    "launch_unknown": "an earlier start of it never confirmed and may still come up",
    "resume_failed": "its session could not be resumed",
    "busy": "it was busy starting or stopping",
    "error": "the lodge hit an error while waking it",
}
REACHED = ("hinted", "resumed", "started", "busy")


def plain(reason: str) -> str:
    if reason.startswith("hint_"):
        return f"its session could not be told ({reason})"
    return REASONS.get(reason, f"its wake is held ({reason})")


def outcome(wolts_dir, wolt: str, *, seqs, how: str, session: str | None = None,
            reason: str | None = None, chat: dict | None = None) -> None:
    """The waker's port for what one wake did with the owner's messages: exactly one line for each
    message it answers (seqs), as before the inbox. A session hinted, resumed or started is named
    with its link, and the chat the message came from follows it; anything else is the reason the
    wolt cannot wake. "Notified" never claims the body was read. Never raises."""
    try:
        if how in REACHED:
            line = (f"🪵 {session} is busy; it gets your message when its current turn ends\n{_link(session)}"
                    if how == "busy" else f"🪵 {session} notified\n{_link(session)}")
            if chat:
                _follow(wolts_dir, wolt, chat, session)
        else:
            line = f"🪵 your mail for {wolt} is waiting, but {wolt} can't wake now: {plain(reason or how)}"
    except Exception as exc:
        log.warning("owner outcome for %s not built: %s", wolt, type(exc).__name__)
        return
    for _ in seqs:  # each message its own line: one lost send never silences the others
        try:
            _post(line, chat)
        except Exception as exc:
            log.warning("owner outcome for %s not sent: %s", wolt, type(exc).__name__)


def _post(message: str, chat: dict | None) -> None:
    """One line through the lodge's own /notify: to the chat when known, else the owner's default."""
    body = {"session": "", "message": message}
    if chat:
        body["adapter"] = chat["adapter"]
        body["chat_id" if chat["adapter"] == "telegram" else "route"] = chat["chat"]
    _call("POST", NOTIFY, body)


def _link(session: str) -> str:
    try:
        url = (_call("GET", "/tunnel") or {}).get("url") or ""
    except Exception:
        url = ""
    return f"{url}/tui?session={session}" if url else session


def _follow(wolts_dir, wolt: str, chat: dict, session: str) -> None:
    """The chat's conversation is the session reached, while the chat is still on that wolt."""
    import chat_state
    state = chat_state.get(wolts_dir, chat["adapter"], chat["chat"])
    if state.get("active_wolt") == wolt:
        chat_state.put(wolts_dir, chat["adapter"], chat["chat"], {**state, "active_session": session})


def _call(method: str, path: str, body: dict | None = None):
    api = get_env("WOLTSPACE_API", "http://localhost:7777")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{api}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    # The lodge's own API is local: never through a proxy from the environment.
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=10) as answer:
        raw = answer.read()
    return json.loads(raw) if raw else None


def owner_notice(wolts_dir, wolt: str, reason: str) -> None:
    try:
        rows = inbox.Store(wolts_dir, inbox.LIMITS).summary()
        if not any(r["wolt"] == wolt and r["queued_human"] for r in rows):
            return
        _post(f"🪵 your mail for {wolt} is waiting, but {wolt} can't wake now: {plain(reason)}", None)
    except Exception as exc:
        log.warning("owner notice for %s not sent: %s", wolt, type(exc).__name__)


def sender_notices(wolts_dir, wolt: str, reason: str) -> None:
    try:
        store = inbox.Store(wolts_dir, inbox.LIMITS)
        for mail in store.unnoticed_agent_mail(wolt, reason):
            try:
                status = store.admit("lodge", f"stuck:{mail['seq']}:{reason}", mail["sender"],
                                     f"[lodge] your message {mail['key']} to {wolt} is queued, but {wolt} "
                                     f"can't wake now: {plain(reason)}", mailbox=mail["mailbox"])
            except inbox.InboxError as exc:  # the sender's mailbox is gone or full
                log.warning("sender notice for %s not queued: %s", wolt, exc.code)
                continue
            import inbox_wake
            inbox_wake.notify_admitted(wolts_dir, status)
    except Exception as exc:
        log.warning("sender notices for %s not sent: %s", wolt, type(exc).__name__)


def notice(wolts_dir, wolt: str, reason: str, *, owner: bool) -> None:
    if owner:
        owner_notice(wolts_dir, wolt, reason)
    sender_notices(wolts_dir, wolt, reason)
