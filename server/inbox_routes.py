"""The lodge inbox over HTTP (container/lib/inbox.py): send, fetch, ack, release, status.

Off (404 inbox_disabled) unless woltspace.json enables the inbox. The caller is
the reader whose token arrives in the X-Woltspace-Inbox-Token header; nothing
in the body names a sender or a priority, and a body with any other field is
refused. Errors are fixed codes; no body or token is ever logged or echoed.
"""

import dataclasses
import json
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

import inbox
import inbox_wake
import lifecycle

from .config import WOLTS_DIR

router = APIRouter(prefix="/inbox")
TOKEN_HEADER = "X-Woltspace-Inbox-Token"
_FIELDS = {"send": {"to", "key", "text", "session"}, "fetch": set(), "ack": {"seq", "claim"},
           "release": {"seq", "claim"}, "status": {"key"}}
_HTTP = {"inbox_disabled": 404, "unsupported": 404, "invalid": 400, "reader_invalid": 401,
         "store_busy": 503, "store_failed": 503, "lifecycle_busy": 409, "launch_unknown": 409,
         "too_large": 413, "owner_only": 403, "unknown_wolt": 404}
# No message can be larger than a whole mailbox; the rest is JSON's own room.
MAX_BODY = inbox.LIMITS.mailbox_bytes + 64 * 1024


class _TooLarge(Exception):
    pass


def _error(code: str) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=_HTTP.get(code, 409))


async def _body(request: Request):
    """The request's JSON, read only up to MAX_BODY bytes (_TooLarge past it); {} when empty.
    ValueError: not JSON."""
    if int(request.headers.get("content-length") or 0) > MAX_BODY:
        raise _TooLarge
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_BODY:
            raise _TooLarge
    return json.loads(bytes(raw) or b"{}")


@router.get("/summary")
async def inbox_summary():
    """Each agent's mailbox by counts only, for the lodge's Inbox page. Reads
    nothing else: viewing never claims, acks, releases or purges."""
    if not inbox.enabled(WOLTS_DIR):
        return _error("inbox_disabled")
    try:
        wolts = await run_in_threadpool(inbox.Store(WOLTS_DIR, inbox.LIMITS).summary)
    except inbox.InboxError as exc:
        return _error(exc.code)
    return {"ok": True, "now": time.time(), "wolts": wolts}


@router.get("/messages")
async def inbox_messages(request: Request):
    """Retained mail with its text, a page at a time, for the owner's read-only viewer
    (Store.messages): ?wolt= one receiver, ?before= the previous page's next, ?limit=. An owner
    route, so a caller holding a session's inbox token is refused. Never cached, never logged;
    viewing claims, acks, releases or purges nothing."""
    if not inbox.enabled(WOLTS_DIR):
        return _error("inbox_disabled")
    if TOKEN_HEADER in request.headers:
        return _error("owner_only")
    query = request.query_params
    try:
        assert set(query) <= {"wolt", "before", "limit"} and len(query.multi_items()) == len(set(query))
        args = {k: int(query[k]) for k in ("before", "limit") if k in query and query[k].isascii()
                and query[k].isdigit()}
        assert len(args) == len(set(query) - {"wolt"})
        page = await run_in_threadpool(inbox.Store(WOLTS_DIR, inbox.LIMITS).messages, query.get("wolt"), **args)
    except AssertionError:
        return _error("invalid")
    except inbox.InboxError as exc:
        return _error(exc.code)
    return JSONResponse({"ok": True, "now": time.time(), **page}, headers={"Cache-Control": "no-store"})


@router.post("/ready")
async def inbox_ready(request: Request):
    """A session's native backend announces its attempt ready (native_adapters.announce_ready).
    The caller is the session's own reader; inbox_wake.ready checks session and attempt."""
    if not inbox.enabled(WOLTS_DIR):
        return _error("inbox_disabled")
    try:
        body = await _body(request)
        assert isinstance(body, dict) and {"session", "attempt"} <= set(body) <= {"session", "attempt", "kind"}
        assert all(isinstance(body[k], str) and body[k] for k in body)
    except _TooLarge:
        return _error("too_large")
    except (ValueError, AssertionError):
        return _error("invalid")
    try:
        scheduled = await run_in_threadpool(inbox_wake.ready, WOLTS_DIR, request.headers.get(TOKEN_HEADER, ""),
                                            body["session"], body["attempt"], body.get("kind", "ready"))
    except inbox.InboxError as exc:
        return _error(exc.code)
    return {"ok": True, "result": {"scheduled": scheduled}}


@router.post("/rearm")
async def inbox_rearm(request: Request):
    """The owner's explicit rearm of an agent's inhibited wake (inbox_wake.rearm). Like every
    owner route it trusts the localhost guard; a caller holding a session's inbox token is an
    agent, never the owner."""
    if not inbox.enabled(WOLTS_DIR):
        return _error("inbox_disabled")
    if TOKEN_HEADER in request.headers:
        return _error("owner_only")
    try:
        body = await _body(request)
        wolt = body["wolt"]
        assert set(body) == {"wolt"} and isinstance(wolt, str)
    except _TooLarge:
        return _error("too_large")
    except (ValueError, KeyError, TypeError, AssertionError):
        return _error("invalid")
    try:
        return {"ok": True, "result": await run_in_threadpool(inbox_wake.rearm, WOLTS_DIR, wolt)}
    except (inbox.InboxError, lifecycle.LifecycleBusy, lifecycle.LifecycleUnknown) as exc:
        return _error(exc.code)


@router.post("/{verb}")
async def inbox_call(verb: str, request: Request):
    if verb not in _FIELDS:
        return _error("unsupported")
    if not inbox.enabled(WOLTS_DIR):
        return _error("inbox_disabled")
    try:
        body = await _body(request)
    except _TooLarge:
        return _error("too_large")
    except ValueError:
        return _error("invalid")
    if not isinstance(body, dict) or not set(body) <= _FIELDS[verb]:
        return _error("invalid")
    token, store = request.headers.get(TOKEN_HEADER, ""), inbox.Store(WOLTS_DIR, inbox.LIMITS)
    call = {
        "send": lambda: store.send(token, body.get("to"), body.get("key"), body.get("text"), body.get("session")),
        "fetch": lambda: store.fetch(token),
        "ack": lambda: store.ack(token, body.get("seq"), body.get("claim")),
        "release": lambda: store.release(token, body.get("seq"), body.get("claim")),
        "status": lambda: store.status(token, body.get("key")),
    }[verb]
    try:
        result = await run_in_threadpool(call)
    except inbox.InboxError as exc:
        return _error(exc.code)
    if verb == "send":  # committed: wake the recipient (never blocks or raises)
        await run_in_threadpool(inbox_wake.notify_admitted, WOLTS_DIR, result)
    return {"ok": True, "result": dataclasses.asdict(result) if dataclasses.is_dataclass(result) else result}
