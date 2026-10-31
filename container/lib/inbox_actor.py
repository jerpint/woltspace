"""
The inbox's delivery actors (RFC #615): one task per conversation with mail,
under one Coordinator that owns the store and the engine adapter.

Inactive: nothing in the lodge starts a Coordinator. The tests own it, its
store and its fake adapter, and stop a Coordinator before the next one takes
over. Its generation is a label on each attempt, not a fence: nothing here
stops a second process from dispatching.

An actor claims its conversation's oldest unresolved message (a committed
attempt), awaits the adapter outside any transaction and records the result,
so a conversation has at most one call open and later mail waits behind a
message that is attempting, blocked or uncertain. The backlog stays in SQL;
the Coordinator only remembers which conversations to wake.

The adapter is `async (inbox.Attempt) -> inbox.Outcome`; anything else it
returns or raises is UNKNOWN, since the call may have reached the agent.

A storage failure halts every actor: no new claim until a new Coordinator,
started once the store is back and this one is stopped, has reconciled what
was left attempting (to uncertain, never sent again).
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import inbox
from inbox import Outcome

log = logging.getLogger(__name__)

_SETTLED = frozenset({"native_accepted", "blocked", "uncertain"})
_OWNED: set[str] = set()  # stores with a started Coordinator in this process


class Halted(inbox.InboxError):
    """Dispatch stopped; `code` says why (a fixed code, never raw text)."""

    def __init__(self, code: str):
        super().__init__(f"inbox dispatch halted ({code})")
        self.code = code


class Coordinator:
    def __init__(self, store: inbox.Store, adapter, *, generation: str, overdue_after: float,
                 clock=time.monotonic, observer=None):
        self.store, self.adapter = store, adapter
        self.generation = inbox._text(generation, store.limits.name_chars, "generation")
        self.overdue_after, self.clock, self.observer = overdue_after, clock, observer
        self.halted: inbox.InboxError | None = None
        self._running = self._owner = False
        self._root = str(Path(store.root).resolve())
        self._actors: dict[tuple[str, str], asyncio.Task] = {}
        self._later: dict[tuple[str, str], None] = {}  # woken while `work` actors ran
        self._open: dict[tuple[str, str], float] = {}  # conversation -> its call's start
        self._waiters: dict[str, list[asyncio.Future]] = {}
        self._n_waiters = 0

    async def start(self) -> None:
        """Own the store: settle what the last owner left, then dispatch. Once:
        a stopped Coordinator stays stopped."""
        if self._owner or self.halted is not None or self._root in _OWNED:
            raise inbox.InboxError("this store already has a Coordinator")
        _OWNED.add(self._root)
        self._owner = True
        conversations = self._guard(self.store.reconcile)
        self._running = True
        for conversation in conversations:
            self._wake(conversation)

    async def stop(self) -> None:
        """Cancel every actor and wait for it. A call cut short stays
        attempting, for the next Coordinator to reconcile."""
        self._running = False  # from here nothing new starts
        self._later.clear()
        tasks = list(self._actors.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._fail_waiters(Halted("stopped"))
        if self._owner:
            _OWNED.discard(self._root)
            self._owner = False
        if self.halted is None:
            self.halted = Halted("stopped")

    # -- the public library -------------------------------------------------

    def admit(self, key: str, recipient: str, conversation: str, body: str) -> inbox.Status:
        status = self._guard(self.store.admit, key, recipient, conversation, body)
        self._wake((recipient, conversation))
        return status

    def status(self, key: str) -> inbox.Status | None:
        return self._guard(self.store.status, key)

    def events(self, after: int = 0, limit: int | None = None) -> inbox.Page:
        return self._guard(self.store.events, after, limit)

    async def wait(self, key: str, timeout: float) -> inbox.Status | None:
        """The message once accepted, blocked or uncertain; TimeoutError after
        timeout seconds. A caller that stops waiting changes nothing else."""
        status = self.status(key)
        if status is None or status.state in _SETTLED:
            return status
        if self.halted is not None:
            raise Halted(self.halted.code)
        if self._n_waiters >= self.store.limits.waiters:
            raise inbox.Refused("waiters")
        future = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(key, []).append(future)
        self._n_waiters += 1
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self._n_waiters -= 1
            futures = self._waiters[key]
            futures.remove(future)
            if not futures:
                del self._waiters[key]

    def overdue(self, recipient: str, conversation: str) -> bool:
        """The conversation's open call has run past overdue_after. Only a flag:
        the call goes on."""
        started = self._open.get((recipient, conversation))
        return started is not None and self.clock() - started > self.overdue_after

    # -- the actors -----------------------------------------------------------

    def _guard(self, operation, *args):
        """Any store failure, a caller's read included, halts dispatch."""
        try:
            return operation(*args)
        except inbox.Unavailable as exc:
            self._halt(exc)
            raise

    def _wake(self, conversation) -> None:
        if not self._running or conversation in self._actors:  # a halted actor ends at once
            return
        if len(self._actors) >= self.store.limits.work:
            self._later[conversation] = None
            return
        self._actors[conversation] = asyncio.get_running_loop().create_task(self._run(conversation))

    async def _run(self, conversation) -> None:
        try:
            while self._running and self.halted is None:  # an adapter may swallow a cancel
                attempt = self.store.claim(*conversation, self.generation)
                if attempt is None:
                    return
                self._open[conversation] = self.clock()
                try:
                    outcome = Outcome(await self.adapter(attempt))
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise  # this task was cancelled: see below
                    # The adapter's reply was cancelled: it may have acted.
                    log.error("inbox: adapter call cancelled seq=%d attempt=%s", attempt.seq, attempt.id)
                    outcome = Outcome.UNKNOWN
                except Exception:  # it may have reached the agent anyway
                    log.error("inbox: adapter call failed seq=%d attempt=%s", attempt.seq, attempt.id)
                    outcome = Outcome.UNKNOWN
                finally:
                    del self._open[conversation]
                self._publish(self.store.record(attempt, outcome))
        except inbox.InboxError as exc:  # Unavailable, or Stale: the store is not as we left it
            self._halt(exc)
        except asyncio.CancelledError:
            if self._running:  # not by stop(): an attempt may be left open
                self._halt(Halted("actor_cancelled"))
            raise
        finally:
            del self._actors[conversation]
            if self._later and self.halted is None:  # halting dropped the rest
                later = next(iter(self._later))
                del self._later[later]
                self._wake(later)

    def _publish(self, status: inbox.Status) -> None:
        """Only ever a committed status."""
        if status.state in _SETTLED:
            for future in self._waiters.get(status.key, ()):
                if not future.done():
                    future.set_result(status)
        if self.observer is not None:
            try:
                self.observer(status)
            except Exception:  # an observer neither undoes a commit nor drives a retry
                log.error("inbox: observer failed seq=%d state=%s", status.seq, status.state)

    def _halt(self, exc: inbox.InboxError) -> None:
        """Diagnostics carry fixed codes and ids only, never exception text."""
        log.error("inbox: dispatch halted code=%s", exc.code)
        self.halted = exc
        self._later.clear()
        self._fail_waiters(Halted(exc.code))

    def _fail_waiters(self, exc: Exception) -> None:
        for futures in self._waiters.values():
            for future in futures:
                if not future.done():
                    future.set_exception(exc)
