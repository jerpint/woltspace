"""The real inbound router: an accepted connector event becomes a session message.

Runs outside the store lock as the store's dispatcher, after core has written
the event as accepted, under a committed attempt claim; the receipt core
writes afterwards is what stops another attempt. The paste into a session is
not idempotent, so a crash between the paste and the receipt repeats the
paste; the pasted header names the event id so a reader can tell.

What it does, in order, every attempt the same way:

1. Decides where each of the event's files goes (an event-owned, stable
   name under `.space/<channel>/uploads/`, where the old adapter saved
   files), writes those destinations into the durable event, then moves the
   files there without ever overwriting: a file already at its destination
   is left alone, a source that is gone is fine if its destination exists.
   Only then are the upload handles released. A crash anywhere in here
   leaves the next attempt the same destinations and the same files.
2. Picks the session: the one the replied-to delivery came from, else the
   binding's remembered active session, else the default wolt's live
   session. Delivers there; a stopped session is resumed only when the
   binding says `may_wake`. No live session and no wake: refused, and the
   human is told through a delivery. Creating a session is a separate grant
   (`may_create`, slice 2b), so this router never spawns.
3. Tells the conversation what happened, as a delivery through the same
   binding, the way the adapter replied "sent to <session>".

Session access goes through three callables (deliver, resume, resolve) that
default to the lodge's own `sessions` module, so tests mock the boundary.
"""

from __future__ import annotations

import errno
import logging
import os
import re
from pathlib import Path
from typing import Callable

from .config import SPACE_DIR, WOLTS_DIR
from .connectors import AcceptedEvent, ConnectorError, ConnectorStore, NoConnectorBinding

logger = logging.getLogger(__name__)

_CREATURE_EMOJIS = {"raccoon": "🦝", "beaver": "🦫", "otter": "🦦", "dog": "🐶", "wolf": "🐺"}
MAX_REPLY_CHARS = 1000
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class AttachmentUnavailable(RuntimeError):
    """A file is neither at its source nor at its destination: nothing to paste."""


def _default_deliver(session: str, text: str) -> dict:
    from sessions import deliver_message

    return deliver_message(session, text)


def _default_resume(session: str, prompt: str) -> dict:
    from sessions import resume_session

    return resume_session(session, prompt)


def _default_resolve(wolt: str) -> str | None:
    from sessions import resolve_active_session

    return resolve_active_session(wolt)


def _reply_instruction(channel: str, conversation: str) -> str:
    try:
        from notify_prompt import notify_reply_instruction

        return notify_reply_instruction(f"--{channel}", conversation)
    except Exception:
        return ""


def destinations_for(event: AcceptedEvent, uploads_dir: Path) -> dict[str, Path]:
    """Event-owned names: the same event always maps to the same paths."""
    stamp = _UNSAFE.sub("_", event.event_id)[:64]
    return {
        a["upload_id"]: uploads_dir / f"{stamp}_{a['upload_id'][:8]}_{a['name']}"
        for a in event.attachments
    }


def place_file(source: Path, dest: Path) -> Path:
    """Move `source` to `dest` without ever overwriting; idempotent across attempts.

    Linking first means a crash between the link and the unlink leaves both
    names on one file, which the next attempt tidies. An existing
    destination is never replaced: if the source is still there it is
    simply removed, since the destination is this event's own file.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        if source.exists():
            source.unlink(missing_ok=True)
        return dest
    if not source.exists():
        raise AttachmentUnavailable(f"{source.name} is gone and {dest.name} was never written")
    try:
        os.link(source, dest)
    except OSError as exc:
        if exc.errno == errno.EEXIST:
            source.unlink(missing_ok=True)
            return dest
        if exc.errno not in (errno.EXDEV, errno.EPERM, errno.EOPNOTSUPP, errno.ENOTSUP):
            raise
        # No hard links across here: copy to a private name, then publish it.
        tmp = dest.with_name(f".{dest.name}.part")
        tmp.write_bytes(source.read_bytes())
        try:
            os.link(tmp, dest)
        except FileExistsError:
            pass
        finally:
            tmp.unlink(missing_ok=True)
    source.unlink(missing_ok=True)
    return dest


def session_text(event: AcceptedEvent, files: list[Path]) -> str:
    """The message a session receives: attributed, with the event id, files, and how to reply."""
    conversation = event.conversation["provider_id"]
    who = event.sender.get("display") or event.sender["provider_id"]
    header = (
        f"[{event.channel} message from {who}, chat_id={conversation}, "
        f"event={event.event_id}]: {event.text}"
    )
    lines = [header]
    for path in files:
        lines.append(f"[file saved: {path}]")
    instruction = _reply_instruction(event.channel, conversation)
    if instruction:
        lines.append(instruction)
    return "\n".join(lines)


class Router:
    def __init__(
        self, store: ConnectorStore, *, uploads_root: Path | None = None,
        deliver: Callable[[str, str], dict] = _default_deliver,
        resume: Callable[[str, str], dict] = _default_resume,
        resolve: Callable[[str], str | None] = _default_resolve,
        wolts_dir: Path | None = None,
    ):
        self.store = store
        self.uploads_root = uploads_root or SPACE_DIR
        self.deliver = deliver
        self.resume = resume
        self.resolve = resolve
        self.wolts_dir = wolts_dir or WOLTS_DIR

    def _tell(self, event: AcceptedEvent, text: str) -> None:
        """Answer the conversation through its own binding; never fatal."""
        try:
            self.store.enqueue(
                channel=event.channel, account=event.account,
                conversation=event.conversation["provider_id"], thread=event.conversation.get("thread"),
                binding_id=event.binding["binding_id"], text=text[:MAX_REPLY_CHARS],
            )
        except (NoConnectorBinding, ConnectorError) as exc:
            logger.warning("could not answer %s: %s", event.conversation["provider_id"], exc)

    def _emoji(self, wolt: str) -> str:
        try:
            import json

            data = json.loads((self.wolts_dir / wolt / "wolt" / "wolt.json").read_text())
            return _CREATURE_EMOJIS.get(data.get("type", ""), "🐾")
        except (OSError, ValueError):
            return "🐾"

    def place_files(self, event: AcceptedEvent) -> list[Path]:
        """Step 1: destinations persisted, files moved idempotently, handles released."""
        if not event.attachments:
            return []
        uploads_dir = self.uploads_root / event.channel / "uploads"
        planned = destinations_for(event, uploads_dir)
        # A destination already written by an earlier attempt wins over the plan.
        destinations = {
            a["upload_id"]: Path(a["dest"]) if a.get("dest") else planned[a["upload_id"]]
            for a in event.attachments
        }
        self.store.record_attachment_destinations(event.instance_id, event.dedup_key, destinations)
        files = [place_file(Path(a["path"]), destinations[a["upload_id"]]) for a in event.attachments]
        self.store.release_uploads(event.instance_id, list(destinations))
        return files

    def __call__(self, event: AcceptedEvent) -> dict:
        files = self.place_files(event)
        text = session_text(event, files)
        binding_id = event.binding["binding_id"]
        wolt = event.binding["default_wolt"]
        remembered = self.store.routing_state(event.instance_id, binding_id).get("active_session")
        session = event.reply_to_session or remembered or self.resolve(wolt)
        if not session:
            self._tell(event, f"🪵 {wolt} has no live session; ask the owner to start one")
            return {"routed": "refused", "reason": "no_session", "wolt": wolt}
        result = self.deliver(session, text)
        status = result.get("status")
        if status == "delivered":
            self.store.remember_route(event.instance_id, binding_id, active_session=session)
            self._tell(event, f"{self._emoji(wolt)} sent to {session}")
            return {"routed": "delivered", "session": session}
        if status in {"session-dead", "agent-gone"}:
            if not event.binding.get("may_wake"):
                self._tell(event, f"🪵 {session} is asleep and this chat may not wake it")
                return {"routed": "refused", "reason": "may_wake_off", "session": session}
            try:
                revived = self.resume(session, text)
            except Exception as exc:
                self._tell(event, f"🪵 could not wake {session}: {exc}")
                return {"routed": "refused", "reason": "resume_failed", "session": session}
            self.store.remember_route(event.instance_id, binding_id, active_session=session)
            self._tell(event, f"🪵 {session} was asleep; woke it and delivered")
            return {"routed": "resumed", "session": session, "detail": revived.get("status", "")}
        # no-session (the remembered or replied-to session is gone): fall back
        # to the wolt's live session once, then give up with a word to the human.
        live = self.resolve(wolt)
        if live and live != session:
            result = self.deliver(live, text)
            if result.get("status") == "delivered":
                self.store.remember_route(event.instance_id, binding_id, active_session=live)
                self._tell(event, f"🪵 {session} is gone; delivered to {wolt}'s live session {live}")
                return {"routed": "delivered", "session": live, "fallback_from": session}
        self._tell(event, f"🪵 {session} is gone and {wolt} has no live session")
        return {"routed": "refused", "reason": "no_session", "session": session}


def make_router(store: ConnectorStore, **overrides) -> Router:
    return Router(store, **overrides)
