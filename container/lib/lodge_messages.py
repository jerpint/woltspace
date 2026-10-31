"""Where an owner's message goes, once for every connector (RFC #617).

A connector turns a provider message (text, a transcribed voice note, a saved
file) into text for a wolt, says whether it replies to one of a session's own
messages, and hands it here. The lodge picks the wolt and the session: the
chat's active one, revived or replaced when it is gone, or a new one. It asks
the dog when no wolt is chosen and a dog is around, and otherwise offers the
wolt picker.

Every outcome the owner should hear about is said through `ports.say`, as a
neutral Reply. Owner checks happen before anything here runs.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import lodge_commands as commands
from connector_contract import Reply

PICK_THEN_RESEND = (
    "your message was not delivered: pick a wolt above (or send /wolt <name>), "
    "then send it again."
)


@dataclass
class MessagePorts:
    """What routing needs beyond the command ports, supplied by the connector."""

    lodge: commands.Ports
    say: Callable[[Reply], Awaitable[None]]
    # message_session(session, text) -> {"ok", "url", "status"}; may block, run in a thread
    message_session: Callable[[str, str], dict]
    # The text a session reads for this message, with the connector's reply instruction.
    frame: Callable[[str], str]
    # The wolt's live session, when the one asked for is gone.
    resolve_live: Callable[[str], str | None]
    # Start a session for a message, the message in its opening prompt.
    spawn_with_message: Callable[[str, str], dict]
    # Whether a wolt still exists (archived or removed ones do not).
    wolt_exists: Callable[[str], bool]
    creature_emoji: Callable[[str], str]
    dog_available: Callable[[], bool]
    # Hand the message to the dog (connector-side: it renders the dog's answer).
    ask_dog: Callable[[str, Any], Awaitable[None]]
    remember_user: Callable[[str], None]
    remember_delivery: Callable[[str], None]
    # deliver(session, wolt, text) -> the session reached, or None. Defaults to
    # `deliver` below; a connector may wrap it.
    deliver: Callable[[str, str, str], Awaitable[str | None]] | None = None
    # say_switch(old_state, wolt, session): tell the owner the chat now talks elsewhere.
    say_switch: Callable[[dict, str, str], Awaitable[None]] | None = None
    # admit(wolt, text, session): queue the message in the wolt's inbox instead of
    # delivering it to a session; session is the one the owner meant, a wake
    # preference only. None (the inbox is off) keeps delivery.
    admit: Callable[[str, str, str | None], Awaitable[None]] | None = None
    log: Callable[[str, dict], None] = lambda event, data: None
    log_error: Callable[[str], None] = lambda message: None


def wolt_from_session_name(session_name: str) -> str | None:
    """'nunu-swift-marsh-abc123' -> 'nunu'; None when the name has no such shape."""
    if not session_name:
        return None
    parts = session_name.rsplit("-", 3)
    return parts[0] if len(parts) == 4 else None


def _link(p: MessagePorts, session_name: str) -> str:
    tunnel_url = p.lodge.tunnel_url()
    return f"{tunnel_url}/tui?session={session_name}" if tunnel_url else session_name


async def deliver(p: MessagePorts, session_name: str, wolt: str, text: str) -> str | None:
    """Deliver to a session; the name it reached, or None when nothing could take it.

    A session that is not routable any more falls back to the wolt's live
    session, and the owner is told where the message went.
    """
    framed = p.frame(text)
    # message_session can block for as long as the agent takes to come up;
    # a connector's event loop must not stall on it.
    result = await asyncio.to_thread(p.message_session, session_name, framed)
    p.log("session_route", {"session": session_name, "wolt": wolt, "text": text[:200], "result": result})
    p.remember_user(text)

    if not result.get("ok"):
        live = p.resolve_live(wolt)
        fallback = (
            await asyncio.to_thread(p.message_session, live, framed)
            if live and live != session_name else None
        )
        p.log("fallback_route", {"from": session_name, "to": live, "wolt": wolt, "result": fallback})
        if fallback and fallback.get("ok"):
            link = fallback.get("url") or live
            await p.say(Reply(f"🪵 {session_name} is gone — delivered to {wolt}'s live session instead\n{link}"))
            p.remember_delivery(live)
            return live
        return None

    link = result.get("url") or session_name
    if result.get("status") == "revived":
        await p.say(Reply(f"🪵 session had exited — revived and delivered\n{link}"))
    else:
        await p.say(Reply(f"🪵 sent to {session_name}\n{link}"))
    p.remember_delivery(session_name)
    return session_name


async def _deliver(p: MessagePorts, session_name: str, wolt: str, text: str) -> str | None:
    if p.deliver:
        return await p.deliver(session_name, wolt, text)
    return await deliver(p, session_name, wolt, text)


async def say_switch(p: MessagePorts, old_state: dict, wolt: str, session_name: str) -> None:
    if wolt != old_state.get("active_wolt") or session_name != old_state.get("active_session"):
        await p.say(Reply(
            f"🪵 now talking to {p.creature_emoji(wolt)} {p.lodge.shown(wolt)} ({session_name})"))


async def _spawn(p: MessagePorts, chat, state: dict, wolt: str, text: str, announce: str) -> None:
    try:
        session = p.spawn_with_message(wolt, text)
        state["active_wolt"] = wolt
        state["active_session"] = session["name"]
        p.lodge.save_chat(chat, state)
        await p.say(Reply(f"{announce}\n{_link(p, session['name'])}"))
    except Exception as e:
        p.log_error(f"Failed to spawn session for {wolt}: {e}")
        await p.say(Reply(f"couldn't start session for {p.lodge.shown(wolt)}: {e}"))


async def _choose_wolt(p: MessagePorts, chat, state: dict) -> bool:
    """With no dog: pick the only wolt, or offer the picker and ask to resend."""
    wolts = commands.chat_wolts(p.lodge)
    if len(wolts) == 1:
        state["active_wolt"] = commands.wolt_name(wolts[0])
        state.pop("active_session", None)
        p.lodge.save_chat(chat, state)
        return True
    if wolts:
        await p.say(commands.wolt_picker(p.lodge, wolts, None))
        await p.say(Reply(PICK_THEN_RESEND))
    else:
        await p.say(Reply(commands.NO_WOLTS_MESSAGE))
    return False


async def route_message(p: MessagePorts, chat, text: str, *, reply_session: str | None = None,
                        reply_text: str | None = None, dog_content: Any = None) -> None:
    """Route one owner message.

    `reply_session` is the session whose message this replies to, if any, and
    `reply_text` the message as that session should read it (with the quote).
    `dog_content` is what the dog sees instead of the text (an image, say).
    """
    if reply_session:
        wolt = wolt_from_session_name(reply_session)
        # A reply to a gone wolt's old message is routed like any message.
        if wolt and p.wolt_exists(wolt):
            framed_text = reply_text if reply_text is not None else text
            if p.admit:
                await p.admit(wolt, framed_text, reply_session)
                return
            previous = p.lodge.load_chat(chat)
            delivered = await _deliver(p, reply_session, wolt, framed_text)
            if delivered:
                state = dict(previous)
                state["active_wolt"] = wolt
                state["active_session"] = delivered
                p.lodge.save_chat(chat, state)
                if p.say_switch:
                    await p.say_switch(previous, wolt, delivered)
                else:
                    await say_switch(p, previous, wolt, delivered)
                return
            await _spawn(p, chat, dict(previous), wolt, framed_text,
                         f"🪵 session expired — new one for {wolt}")
            return

    state = p.lodge.load_chat(chat)
    wolt = state.get("active_wolt")
    session_name = state.get("active_session")

    if not wolt:
        if p.dog_available():
            await p.ask_dog(text, dog_content)
            return
        if not await _choose_wolt(p, chat, state):
            return
        wolt = state["active_wolt"]
        session_name = None

    if p.admit:
        await p.admit(wolt, text, session_name)
        return

    if session_name:
        delivered = await _deliver(p, session_name, wolt, text)
        if delivered:
            if delivered != session_name:
                state["active_session"] = delivered
                p.lodge.save_chat(chat, state)
            return
        await p.say(Reply(f"🪵 session expired — spawning new one for {p.lodge.shown(wolt)}"))

    await _spawn(p, chat, state, wolt, text, f"🪵 new session for {p.lodge.shown(wolt)}")
