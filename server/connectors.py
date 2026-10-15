"""Core-owned connector state: instances, bindings, credentials, queues.

A connector carries messages between a provider (Telegram, Slack, ...) and the
lodge, and it decides nothing: which senders are allowed, where an event goes,
what it may fetch, and when a delivery has failed are all answered here,
against state the owner controls and only the lodge writes.

Everything lives under `.space/connectors/`:

    registry.json        instances, bindings and credential digests
    queues/<instance>.json   one instance's deliveries, dedup records,
                         upload handles and rate counters
    uploads/<instance>/<upload_id>   bytes a connector sent in
    files/<file_id>      bytes the lodge wants sent out
    inbound.jsonl        accepted events, until the real router takes them
    failures.jsonl       deliveries that ended without a provider message

Every write is temp + fsync + rename under one file lock, so a crash leaves
the previous state rather than half of the next one. A file that does not
parse is `ConnectorStateUnreadable`: nothing is guessed from it and no
credential it would have vouched for is accepted.

Transitions never need two files to agree. Enable, disable, rotation, bind
and unbind are single registry writes; what they mean for a queue (paused
clocks, released leases, cancelled deliveries) is derived from the registry
every time the queue is read. Removal writes the registry first and then
cancels the queue; a queue file whose instance is gone is an orphan that
`recover()` cancels the same way, so a crash in between is finished on the
next load. Transfer to a replacement instance is not in this slice: removal
cancels, always.

Listeners and the router run after the lock is released and the state is on
disk, so they may call back into the store. A call made while the lock is
held by the same thread is refused, not blocked. Failure notifications go to
committed transitions only, at most once: a crash between the write and the
dispatch loses one.

Inbound events have their own durable state. An accepted event is written
as `accepted` together with its dedup record, and only then is it handed to
the dispatcher (the lodge's router), outside the lock; the receipt turns the
record into `handed_off`. A store with no dispatcher configured (the CLI, a
diagnostic read) never hands anything off and never writes a receipt. Each
handoff is a committed claim with its own random identity, made by a
connector-driven operation (lease, ack, upload, event, file): an owner read
claims nothing, an uncommitted transaction dispatches nothing, and a receipt
whose claim is not the current one is ignored. The claim identity is never
reused: an owner retry resets the attempt budget but not the identity, and a
claim given back (instance disabled meanwhile) is a different claim when it
is made again. Before every attempt the current
registry is read again: a binding that is gone cancels the record, a
disabled instance or a channel the connector no longer owns leaves it
waiting. Attempts are bounded (`handoff_max_attempts`); a record that used
them up is parked as `handoff_failed`, kept with its files until the owner
retries or cancels it (`woltspace connector handoffs`), never aged out. The
guarantee at each boundary:

    provider -> core      at least once from the provider; core answers
                          exactly once per (account, event_id) for 7 days
    core -> session       bounded at-least-once attempts: a crash after the
                          router pasted but before the receipt was written
                          repeats the paste. The session text carries the
                          event id, so a reader can tell; the session itself
                          cannot. Attachments are moved to event-owned,
                          persisted destinations before the paste, so every
                          attempt names the same files.
    receipt -> cursor     the connector advances its cursor only past
                          positions core has answered, and only in the order
                          it fetched them: core checks the position just
                          below the cursor, the rest is the connector's
                          ordered-batch responsibility (see `set_cursor`), not
                          a frontier core observes. A crash before the advance
                          re-fetches them and core answers from dedup without
                          a second handoff. The position is part of the
                          event's identity: a replay with another cursor is a
                          conflict, never a relabel.

The provider cursor and the map from provider message ids (every part of a
split delivery, sent parts of a failed one included) to deliveries live
here too, scoped to the instance's account and conversation, so reply
routing survives a connector restart.

Which runtime owns a channel (`telegram.runtime` in the lodge's
woltspace.json: the connector by default, or the old in-process adapter) is read
on every operation: the instance of a channel whose runtime is not the
connector hands out nothing, accepts nothing and advances no cursor. A
running connector process holds the channel's run lock
(`.space/connectors/run-<channel>.lock`, published by `state`) for its whole
life; the switch takes that lock and the lodge's own, so it only ever
changes the file while neither runtime can be mid-call.

Stdlib only; the connector API and the CLI both import this.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import outbox

TOKEN_PREFIX = "wct_"
_ID_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-_")


@dataclass(frozen=True)
class Limits:
    """Every bound the protocol enforces. Tests shrink them; nothing widens them."""

    lease_seconds: float = 60.0
    # Failed attempts (a nack, or a lease that expired) before the delivery is
    # failed for good. The schedule: after failure n the delivery waits
    # backoff_seconds[n - 1]; the fifth failure is terminal, so only four
    # delays are ever used. An expired lease re-queues at once.
    max_attempts: int = 5
    backoff_seconds: tuple[float, ...] = (2.0, 5.0, 15.0, 30.0)
    # Enabled time only: a disabled instance's clock stands still.
    retry_window_seconds: float = 600.0
    dedup_retention_seconds: float = 7 * 86400.0
    upload_ttl_seconds: float = 3600.0
    upload_max_bytes: int = 50 * 1024 * 1024
    # One outbound attachment; v1 sends at most one per delivery.
    outbound_file_max_bytes: int = 50 * 1024 * 1024
    # Every byte an instance has on disk at once: stored handles, claimed
    # ones a router has not released yet, and uploads still arriving.
    upload_disk_cap_bytes: int = 200 * 1024 * 1024
    file_retention_seconds: float = 86400.0
    text_max_bytes: int = 16 * 1024
    outbound_text_max_bytes: int = 64 * 1024
    # The whole JSON request body, counted before it is parsed.
    request_max_bytes: int = 64 * 1024
    id_max_chars: int = 256
    display_max_chars: int = 256
    error_max_chars: int = 1000
    event_max_attachments: int = 10
    lease_max_batch: int = 10
    lease_max_wait_seconds: float = 30.0
    max_in_flight: int = 50
    max_queued: int = 1000
    events_per_minute: int = 120
    uploads_per_hour: int = 60
    upload_bytes_per_hour: int = 500 * 1024 * 1024
    journal_max_bytes: int = 10 * 1024 * 1024
    # Handoff to the session: retried on every read after this long, this often.
    handoff_retry_seconds: float = 30.0
    handoff_max_attempts: int = 5
    ack_max_parts: int = 50
    cursor_max_chars: int = 32
    # A pending (handoff) generation must be activated within this long of its mint.
    handoff_window_seconds: float = 10.0
    # Inline-button callbacks: how long an issued id stays valid, how many a binding may hold.
    callback_ttl_seconds: float = 3600.0
    callbacks_per_binding: int = 50
    # `/new` defaults when the owner sets no limit on the binding.
    create_per_hour: int = 6
    create_concurrent: int = 2
    max_buttons: int = 20
    # Voice transcription (owner opt-in per binding). Bounds are bytes and
    # requests, measured on the stored file; no duration is claimed.
    transcribe_max_bytes: int = 10 * 1024 * 1024
    transcribe_bytes_per_day: int = 100 * 1024 * 1024
    transcribe_requests_per_day: int = 200
    transcribe_max_attempts: int = 2
    transcribe_claim_seconds: float = 120.0


class ConnectorStateUnreadable(RuntimeError):
    """A state file does not parse. Nothing is served from a guess."""


class ConnectorError(ValueError):
    """A request the protocol refuses. `reason` is machine-readable.

    `retry` says whether the same request may succeed later: a 429 is a
    limit that clears with time (an upload it refused kept no bytes, so it
    is uploaded again); everything else is final for that request.
    """

    def __init__(self, status: int, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.status = status
        self.reason = reason
        self.retry = status == 429


class NoConnectorBinding(LookupError):
    """Core wants to send somewhere no enabled connector is bound to."""


@dataclass(frozen=True)
class Principal:
    """What a connector token proves: one instance, one process generation.

    A Principal is a snapshot. Every store operation re-reads the registry
    under its lock and refuses a principal whose token was revoked or whose
    generation moved on in the meantime.
    """

    instance_id: str
    generation: int
    channel: str
    account: str
    enabled: bool
    digest: str = field(repr=False, compare=False, default="")


@dataclass(frozen=True)
class AcceptedEvent:
    """What the router seam receives once core has admitted an event."""

    instance_id: str
    channel: str
    account: str
    event_id: str
    binding: dict
    sender: dict
    conversation: dict
    text: str
    # [{"upload_id", "path", "name", "size"}]: the router owns these bytes
    # from here on. They count against the instance's disk cap until the
    # router calls `release_uploads` (after moving them) or the TTL sweep
    # deletes them.
    attachments: tuple[dict, ...]
    # The session the replied-to delivery came from, when reply_to resolved.
    reply_to_session: str | None
    # The provider's position for this event (Telegram's update_id), or None.
    cursor: int | None = None
    # An inline-button press: the opaque id core issued, resolved at execution.
    callback: str | None = None
    # What the replied-to delivery said, from core's own record of it:
    # {"text", "file", "voice"} (see `_quote_of`); None when not a reply to one.
    reply_quote: dict | None = None

    @property
    def dedup_key(self) -> str:
        """What a router keys its own idempotency on: one per account and event."""
        return f"{self.account}\x00{self.event_id}"

    def to_record(self) -> dict:
        return {
            "instance_id": self.instance_id, "channel": self.channel, "account": self.account,
            "event_id": self.event_id, "binding": self.binding, "sender": self.sender,
            "conversation": self.conversation, "text": self.text,
            "attachments": list(self.attachments), "reply_to_session": self.reply_to_session,
            "cursor": self.cursor, "callback": self.callback, "reply_quote": self.reply_quote,
        }

    @classmethod
    def from_record(cls, record: dict) -> "AcceptedEvent":
        return cls(
            record["instance_id"], record["channel"], record["account"], record["event_id"],
            record["binding"], record["sender"], record["conversation"], record["text"],
            tuple(record["attachments"]), record.get("reply_to_session"), record.get("cursor"),
            record.get("callback"), record.get("reply_quote"),
        )


@dataclass
class _Queue:
    deliveries: list = field(default_factory=list)
    dedup: dict = field(default_factory=dict)
    uploads: dict = field(default_factory=dict)
    counters: dict = field(default_factory=lambda: {"events": [], "uploads": []})
    # Accepted events and their handoff state: {dedup key: {"state", "event", ...}}.
    inbound: dict = field(default_factory=dict)
    # {"<account>\x00<provider_message_id>": {"delivery_id", "binding_id", "session", "at"}}
    provider_messages: dict = field(default_factory=dict)
    # Per-binding routing memory: {binding_id: {"active_session", "active_wolt",
    # "sessions": {name: {"via", "at"}}, "creates": [ts, ...]}}.
    routing: dict = field(default_factory=dict)
    # Inline-button callbacks core issued: {id: {"binding_id", "action", "expires", "consumed_by"}}.
    callbacks: dict = field(default_factory=dict)
    # Durable command operations keyed by the event's dedup key: {key: {"command",
    # "argument", "binding_id", "state", "session", "wolt", "at", ...}}.
    operations: dict = field(default_factory=dict)
    # Voice transcriptions, one record per (event, attachment): {"<key>\x00<upload_id>":
    # {"state": "claimed"|"done", "claim", "claimed_at", "attempts", "text"|"reason"}}.
    transcriptions: dict = field(default_factory=dict)
    # The instance's UTC-day transcription budget: {"day", "bytes", "requests"}.
    transcribe_budget: dict = field(default_factory=dict)
    # The provider cursor the connector last confirmed, and the highest
    # position core has answered (accepted or dropped).
    cursor: int | None = None
    answered_cursor: int | None = None

    def to_json(self) -> dict:
        return {
            "version": 1, "deliveries": self.deliveries, "dedup": self.dedup,
            "uploads": self.uploads, "counters": self.counters, "inbound": self.inbound,
            "provider_messages": self.provider_messages, "routing": self.routing,
            "callbacks": self.callbacks, "operations": self.operations,
            "transcriptions": self.transcriptions, "transcribe_budget": self.transcribe_budget,
            "cursor": self.cursor, "answered_cursor": self.answered_cursor,
        }

    @classmethod
    def from_json(cls, data: dict) -> "_Queue":
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ConnectorStateUnreadable("queue file has an unknown shape")
        queue = cls(
            data.get("deliveries", []), data.get("dedup", {}),
            data.get("uploads", {}), data.get("counters", {"events": [], "uploads": []}),
            data.get("inbound", {}), data.get("provider_messages", {}), data.get("routing", {}),
            data.get("callbacks", {}), data.get("operations", {}),
            data.get("transcriptions", {}), data.get("transcribe_budget", {}),
            data.get("cursor"), data.get("answered_cursor"),
        )
        if not (isinstance(queue.deliveries, list) and isinstance(queue.dedup, dict)
                and isinstance(queue.uploads, dict) and isinstance(queue.counters, dict)
                and isinstance(queue.inbound, dict) and isinstance(queue.provider_messages, dict)
                and isinstance(queue.routing, dict) and isinstance(queue.callbacks, dict)
                and isinstance(queue.operations, dict) and isinstance(queue.transcriptions, dict)
                and isinstance(queue.transcribe_budget, dict)):
            raise ConnectorStateUnreadable("queue file has an unknown shape")
        for value in (queue.cursor, queue.answered_cursor):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise ConnectorStateUnreadable("queue file has an unknown shape")
        queue.counters.setdefault("events", [])
        queue.counters.setdefault("uploads", [])
        return queue


_TERMINAL = frozenset({"delivered", "failed", "cancelled"})
_EVENT_KEYS = frozenset({"event_id", "sender", "conversation", "text", "attachments", "reply_to", "cursor", "callback"})
_CALLBACK_KEYS = frozenset({"id"})
CALLBACK_PREFIX = "cb_"
_SENDER_KEYS = frozenset({"provider_id", "display"})
_CONVERSATION_KEYS = frozenset({"provider_id", "thread"})
_ACK_KEYS = frozenset({"attempt_id", "ok", "provider_message_id", "provider_message_ids", "error", "retry"})
_REPLY_TO_KEYS = frozenset({"delivery_id", "provider_message_id"})
_IGNORE_KEYS = frozenset({"event_id", "cursor", "ignore"})
RUNTIMES = ("adapter", "connector")


QUOTE_MAX_CHARS = 200
CHAT_WOLT_TYPES = frozenset({"raccoon", "beaver", "otter", "rodent"})
ROUTE_LOCK_STRIPES = 1024


def _quote_of(delivery: dict) -> dict:
    """What a reply to this delivery quotes: its text (footer kept apart already), its file or voice note."""
    if "quote" in delivery:
        return delivery["quote"]
    attachment = next(iter(delivery.get("attachments") or ()), None) or {}
    return {
        "text": str(delivery.get("text") or "")[:QUOTE_MAX_CHARS],
        "file": attachment.get("name") if attachment.get("kind") == "file" else None,
        "voice": attachment.get("kind") == "voice",
    }


def may_start(binding: dict, wolt: str | None) -> bool:
    """`may_create`, for a wolt this binding may talk to: a selectable one or its default."""
    return bool(binding.get("may_create")) and wolt is not None and (
        wolt == binding["default_wolt"] or wolt in (binding.get("selectable_wolts") or ())
    )


def default_channel_runtime(channel: str) -> str:
    """The connector, for every channel; Telegram's old in-process adapter only when the owner says `adapter`."""
    return "connector"


def channel_runtime(config_path: Path | None, channel: str) -> str:
    """Which runtime owns `channel`: `<channel>.runtime` in the lodge's woltspace.json.

    A missing or unreadable file, or no setting, is the default (the
    connector): only an owner statement brings the adapter back. Both
    runtimes read this one rule, so they never disagree about who polls.
    """
    default = default_channel_runtime(channel)
    if config_path is None:
        return default
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    section = data.get(channel) if isinstance(data, dict) else None
    value = section.get("runtime") if isinstance(section, dict) else None
    return value if value in RUNTIMES else default


def set_channel_runtime(config_path: Path, channel: str, runtime: str) -> None:
    """Write the owner's choice; the other keys of woltspace.json are kept."""
    if runtime not in RUNTIMES:
        raise ConnectorError(400, "invalid_runtime", f"runtime must be one of {', '.join(RUNTIMES)}")
    if not valid_id(channel, 32):
        raise ConnectorError(400, "invalid_channel")
    try:
        data = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    except (OSError, ValueError):
        raise ConnectorStateUnreadable(f"{config_path.name} does not parse") from None
    if not isinstance(data, dict):
        raise ConnectorStateUnreadable(f"{config_path.name} has an unknown shape")
    section = data.get(channel)
    section = dict(section) if isinstance(section, dict) else {}
    section["runtime"] = runtime
    data[channel] = section
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_path.with_name(f".{config_path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, config_path.stat().st_mode & 0o7777)
        except FileNotFoundError:
            pass
        os.replace(tmp, config_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def valid_id(value: object, limit: int = 256) -> bool:
    """An owner- or install-chosen id: lower-case, no path or space characters."""
    return (
        isinstance(value, str) and 0 < len(value) <= limit
        and set(value) <= _ID_CHARS and value[0] not in "-_"
    )


def _provider_id(value: object, limit: int) -> str | None:
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return None
    return value


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _canonical(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def run_lock_path(root: Path, channel: str) -> Path:
    """The file a running connector of `channel` holds an exclusive flock on, for life."""
    return Path(root) / f"run-{channel}.lock"


@contextlib.contextmanager
def held_run_lock(path: Path):
    """Take a channel's run lock without waiting; `BlockingIOError` means a connector runs."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(path, "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _pid_alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class _Tx:
    """One locked operation's bookkeeping: what it loaded, changed and committed.

    A terminal transition is `pending` until the queue holding it is written
    (or deleted) and `committed` after; only committed ones are dispatched.
    """

    def __init__(self):
        self.pending: list[tuple[str, dict]] = []
        self.committed: list[tuple[str, dict]] = []
        self.queues: dict[str, _Queue] = {}
        self.dirty: set[str] = set()
        # Handoff claims: (instance_id, dedup key, claim id). Pending until
        # the queue holding the claim is written, then committed; only
        # committed claims are dispatched once the lock is gone.
        self.pending_handoffs: list[tuple[str, str, str]] = []
        self.committed_handoffs: list[tuple[str, str, str]] = []
        # Owner and diagnostic reads never claim a handoff.
        self.dispatch: bool = True

    def settle(self, instance_id: str) -> None:
        """The queue is on disk (or gone): its transitions and claims are committed."""
        self.dirty.discard(instance_id)
        kept = []
        for entry in self.pending:
            (self.committed if entry[0] == instance_id else kept).append(entry)
        self.pending = kept
        kept_claims = []
        for claim in self.pending_handoffs:
            (self.committed_handoffs if claim[0] == instance_id else kept_claims).append(claim)
        self.pending_handoffs = kept_claims


class ConnectorStore:
    """The lodge's side of the connector protocol, on disk."""

    def __init__(
        self, root: Path, *, clock: Callable[[], float] = time.time,
        limits: Limits | None = None, runtime_config: Path | None = None,
    ):
        self.root = Path(root)
        self.clock = clock
        self.limits = limits or Limits()
        # The lodge's woltspace.json, where `<channel>.runtime` lives. Derived
        # from the usual `wolts/.space/connectors` layout; None means every
        # channel's connector runtime is active (tests, odd roots).
        if runtime_config is None and self.root.parent.name == ".space":
            runtime_config = self.root.parent.parent / "woltspace.json"
        self.runtime_config = runtime_config
        # The dispatcher. None (the CLI, tests of the store alone) means
        # accepted events wait; the lodge installs its router.
        self.router: Callable[[AcceptedEvent], dict] | None = None
        self._failure_listeners: list[Callable[[str, dict], None]] = [self.record_failure]
        self._held = threading.local()

    def runtime_active(self, channel: str) -> bool:
        """Whether the connector runtime owns `channel` right now (always, with no config)."""
        if self.runtime_config is None:
            return True
        return channel_runtime(self.runtime_config, channel) == "connector"

    def lodge_wolts(self) -> list[str]:
        """The lodge's chat wolts (rodents; the wolf and the dog are not chat targets), by name.

        Read from the wolts folder this store lives in; none when the store
        is not in the usual `wolts/.space/connectors` layout.
        """
        if self.root.parent.name != ".space":
            return []
        names = []
        for manifest in sorted(self.root.parent.parent.glob("*/wolt/wolt.json")):
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            name = manifest.parent.parent.name
            if isinstance(data, dict) and data.get("type", "rodent") in CHAT_WOLT_TYPES and valid_id(name, 64):
                names.append(name)
        return names

    def _effective(self, binding: dict) -> dict:
        """A copy of the binding with `any_wolt` resolved: every chat wolt of the lodge, now, is selectable."""
        binding = dict(binding)
        if binding.get("any_wolt"):
            selectable = list(binding.get("selectable_wolts") or ())
            selectable += [name for name in self.lodge_wolts() if name not in selectable]
            binding["selectable_wolts"] = selectable
        return binding

    # ----------------------------------------------------------------- files

    @property
    def registry_path(self) -> Path:
        return self.root / "registry.json"

    def _queue_path(self, instance_id: str) -> Path:
        return self.root / "queues" / f"{instance_id}.json"

    def _upload_dir(self, instance_id: str) -> Path:
        return self.root / "uploads" / instance_id

    def _part_path(self, instance_id: str, upload_id: str) -> Path:
        return self._upload_dir(instance_id) / f".{upload_id}.part"

    def _file_path(self, file_id: str) -> Path:
        return self.root / "files" / file_id

    @contextlib.contextmanager
    def _lock(self):
        """Serialize every read-modify-write across the lodge and the CLI.

        Not re-entrant, and says so: a call from inside a locked operation
        (a listener or router that ran under the lock) is a bug that would
        otherwise block the lodge for good.
        """
        if getattr(self._held, "depth", 0):
            raise RuntimeError("connector store called from inside its own lock")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(self.root / "lock", "w") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            self._held.depth = 1
            try:
                yield
            finally:
                self._held.depth = 0
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextlib.contextmanager
    def command_lock(self, binding_id: str):
        """Serialize one binding's chat commands, across threads and processes.

        Taken before a command reads its operation, samples live sessions or
        decides how to resume, and held through the session call to the
        settlement; the owner's resolution takes it too. Lock order is
        always this lock, then the store lock briefly inside it: never wait
        for this one while holding the store lock.
        """
        if getattr(self._held, "depth", 0):
            raise RuntimeError("command lock requested under the store lock")
        directory = self.root / "commands"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        name = hashlib.sha256(str(binding_id).encode()).hexdigest()[:32]
        fd = os.open(directory / f"{name}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._held.nested = getattr(self._held, "nested", 0) + 1
            try:
                yield
            finally:
                self._held.nested -= 1
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def binding_now(self, binding_id: str) -> dict | None:
        """The binding as the registry has it now (its current grants), or None when it is gone."""
        with self._lock():
            registry = self._registry()
            binding = next((b for b in registry["bindings"] if b["binding_id"] == binding_id), None)
            return self._effective(binding) if binding is not None else None

    @contextlib.contextmanager
    def _transaction(self, *, dispatch: bool = True):
        """A locked operation whose callbacks run after the lock is released.

        Whatever the sweep changed is written on every way out that the
        protocol defines, a normal return and a refusal alike, so the disk
        never lags the clock. An unexpected exception writes nothing more.
        Listeners then hear about committed transitions only, at most once:
        a crash between the write and the dispatch loses that notification.
        """
        tx = _Tx()
        # Never claim or run a handoff from inside another one, nor while
        # this thread holds a command lock: the nested dispatch could need
        # that same lock (or another binding's, in a cycle). Unclaimed work
        # stays for the next read made outside.
        tx.dispatch = dispatch and self.router is not None and not getattr(self._held, "nested", 0)
        try:
            with self._lock():
                try:
                    yield tx
                except ConnectorError:
                    self._flush(tx)
                    raise
                self._flush(tx)
        finally:
            for instance_id, delivery in tx.committed:
                for listener in list(self._failure_listeners):
                    try:
                        listener(instance_id, dict(delivery))
                    except Exception:
                        pass
            for instance_id, key, claim in tx.committed_handoffs:
                self._hand_off(instance_id, key, claim)

    def _load(self, tx: _Tx, instance_id: str) -> _Queue:
        queue = self._queue(instance_id)
        tx.queues[instance_id] = queue
        return queue

    def _commit(self, tx: _Tx, instance_id: str) -> None:
        self._save_queue(instance_id, tx.queues[instance_id])
        tx.settle(instance_id)

    def _flush(self, tx: _Tx) -> None:
        """Write every swept queue the operation did not write itself."""
        for instance_id in list(tx.dirty):
            if instance_id in tx.queues:
                self._commit(tx, instance_id)

    def _read_json(self, path: Path, default: dict) -> dict:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return default
        except OSError as exc:
            raise ConnectorStateUnreadable(f"{path.name}: {exc.strerror}") from None
        try:
            data = json.loads(raw)
        except ValueError:
            raise ConnectorStateUnreadable(f"{path.name} does not parse") from None
        if not isinstance(data, dict):
            raise ConnectorStateUnreadable(f"{path.name} has an unknown shape")
        return data

    @staticmethod
    def _write_json(path: Path, data: dict) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    @contextlib.contextmanager
    def _journal_lock(self):
        """Serialize journal rotation plus append, and nothing else.

        Journals are written by listeners, which run outside the store lock
        and possibly from several processes at once; two rotations racing
        would displace a segment that was just rotated.
        """
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(self.root / "journal.lock", "w") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _append_line(self, name: str, record: dict) -> None:
        """Append one journal line, fsynced; the journal rotates once at its bound."""
        line = json.dumps(record, sort_keys=True) + "\n"
        with self._journal_lock():
            path = self.root / name
            try:
                if path.stat().st_size >= self.limits.journal_max_bytes:
                    os.replace(path, path.with_name(f"{name}.1"))
            except FileNotFoundError:
                pass
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())

    def _registry(self) -> dict:
        data = self._read_json(
            self.registry_path, {"version": 1, "instances": {}, "bindings": [], "tokens": []},
        )
        if data.get("version") != 1 or not (
            isinstance(data.get("instances"), dict) and isinstance(data.get("bindings"), list)
            and isinstance(data.get("tokens"), list)
        ):
            raise ConnectorStateUnreadable("registry.json has an unknown shape")
        return data

    def _queue(self, instance_id: str) -> _Queue:
        data = self._read_json(self._queue_path(instance_id), {})
        return _Queue() if not data else _Queue.from_json(data)

    def _save_queue(self, instance_id: str, queue: _Queue) -> None:
        self._write_json(self._queue_path(instance_id), queue.to_json())

    # ------------------------------------------------------------- instances

    def list_instances(self) -> list[dict]:
        registry = self._registry()
        return [
            {"instance_id": instance_id, **record}
            for instance_id, record in sorted(registry["instances"].items())
        ]

    def add_instance(self, instance_id: str, channel: str, account: str, *, adoption: str | None = None) -> dict:
        """Register an instance, disabled: nothing routes to it until `enable`.

        Registration only: this slice installs no code. `adoption` marks an
        instance the lodge registered itself while carrying an adapter setup
        over ("pending" until that is complete, then "done"); an instance
        without it is the owner's, and the lodge never changes it.
        """
        if adoption not in (None, "pending", "done"):
            raise ConnectorError(400, "invalid_adoption")
        if not valid_id(instance_id, 64):
            raise ConnectorError(400, "invalid_instance_id")
        if not valid_id(channel, 32):
            raise ConnectorError(400, "invalid_channel")
        if _provider_id(account, self.limits.id_max_chars) is None:
            raise ConnectorError(400, "invalid_account")
        with self._lock():
            registry = self._registry()
            if instance_id in registry["instances"]:
                raise ConnectorError(409, "instance_exists")
            registry["instances"][instance_id] = {
                "channel": channel, "account": account, "enabled": False,
                "generation": 0, "created": self.clock(), "disabled_at": self.clock(),
                "paused_seconds": 0.0,
            }
            if adoption is not None:
                registry["instances"][instance_id]["adoption"] = adoption
            self._write_json(self.registry_path, registry)
            return {"instance_id": instance_id, **registry["instances"][instance_id]}

    def finish_adoption(self, instance_id: str) -> None:
        """Mark a lodge-registered instance's carry-over complete; never touches an owner's instance."""
        with self._lock():
            registry = self._registry()
            instance = self._instance(registry, instance_id)
            if instance.get("adoption") != "pending":
                raise ConnectorError(409, "not_adopting")
            instance["adoption"] = "done"
            self._write_json(self.registry_path, registry)

    def _instance(self, registry: dict, instance_id: str) -> dict:
        try:
            return registry["instances"][instance_id]
        except KeyError:
            raise ConnectorError(404, "unknown_instance") from None

    def enable(self, instance_id: str) -> None:
        """Resume routing; the retry clocks resume by derivation. One write."""
        with self._lock():
            registry = self._registry()
            instance = self._instance(registry, instance_id)
            for other_id, other in registry["instances"].items():
                if other_id != instance_id and other["enabled"] and other["channel"] == instance["channel"]:
                    raise ConnectorError(
                        409, "channel_taken",
                        f"{other_id} already serves {instance['channel']}; disable it first",
                    )
            if instance.get("disabled_at") is not None:
                instance["paused_seconds"] = instance.get("paused_seconds", 0.0) + max(
                    0.0, self.clock() - instance["disabled_at"],
                )
                instance["disabled_at"] = None
            instance["enabled"] = True
            self._write_json(self.registry_path, registry)

    def disable(self, instance_id: str) -> None:
        """Pause: nothing is leased, the retry window stops, files are kept. One write."""
        with self._lock():
            registry = self._registry()
            instance = self._instance(registry, instance_id)
            if instance["enabled"] or instance.get("disabled_at") is None:
                instance["disabled_at"] = self.clock()
            instance["enabled"] = False
            self._write_json(self.registry_path, registry)

    def remove_instance(self, instance_id: str) -> dict:
        """Revoke, detach and cancel the queue. Transfer is not in this slice.

        The registry write comes first; the queue is then an orphan and is
        cancelled, here or by `recover()` after a crash. Either way it is
        cancelled exactly once.
        """
        with self._transaction(dispatch=False) as tx:
            registry = self._registry()
            self._recover_orphans(registry, tx)
            self._instance(registry, instance_id)
            now = self.clock()
            for token in registry["tokens"]:
                if token["instance_id"] == instance_id and token.get("revoked") is None:
                    token["revoked"] = now
            registry["bindings"] = [
                b for b in registry["bindings"] if b["instance_id"] != instance_id
            ]
            del registry["instances"][instance_id]
            self._write_json(self.registry_path, registry)
            return {"cancelled": self._cancel_orphan_queue(instance_id, tx), "transferred": 0}

    def recover(self) -> int:
        """Finish interrupted removals: cancel every queue whose instance is gone."""
        with self._transaction(dispatch=False) as tx:
            return self._recover_orphans(self._registry(), tx)

    def _recover_orphans(self, registry: dict, tx: _Tx) -> int:
        cancelled = 0
        try:
            entries = sorted((self.root / "queues").glob("*.json"))
        except OSError:
            return 0
        for path in entries:
            instance_id = path.stem
            if instance_id not in registry["instances"]:
                cancelled += self._cancel_orphan_queue(instance_id, tx)
        for directory in (self.root / "uploads").glob("*"):
            if directory.is_dir() and directory.name not in registry["instances"]:
                shutil.rmtree(directory, ignore_errors=True)
        return cancelled

    def _cancel_orphan_queue(self, instance_id: str, tx: _Tx) -> int:
        queue = self._load(tx, instance_id)
        cancelled = 0
        for delivery in queue.deliveries:
            if delivery["status"] not in _TERMINAL:
                self._finish(instance_id, delivery, "cancelled", "instance removed", tx)
                cancelled += 1
        for upload_id in queue.uploads:
            self._part_path(instance_id, upload_id).unlink(missing_ok=True)
        self._queue_path(instance_id).unlink(missing_ok=True)
        shutil.rmtree(self._upload_dir(instance_id), ignore_errors=True)
        # Gone from disk is as committed as written.
        tx.settle(instance_id)
        return cancelled

    # -------------------------------------------------------------- bindings

    def list_bindings(self, instance_id: str | None = None) -> list[dict]:
        registry = self._registry()
        return [
            dict(b) for b in registry["bindings"]
            if instance_id is None or b["instance_id"] == instance_id
        ]

    def bind(
        self, instance_id: str, *, sender: str, conversation: str, default_wolt: str,
        thread: str | None = None, may_wake: bool = False,
        selectable_wolts: tuple[str, ...] | list[str] = (), may_create: bool = False,
        may_stop_own_sessions: bool = False, create_per_hour: int | None = None,
        create_concurrent: int | None = None, may_transcribe: bool = False,
        may_manage_apps: bool = False, any_wolt: bool = False,
        _adopt_route: dict | None = None,
    ) -> dict | None:
        """Allow one provider sender in one conversation; the owner's grant, typed.

        Binding the same sender, conversation and thread again changes the
        grant in place and keeps its id, so deliveries made under it stand.

        The chat-command grants are the owner's and only the owner's:
        `selectable_wolts` (which wolts `/wolt` and `/new` may pick),
        `may_create` (`/new`, with its hourly and concurrent limits; distinct
        from `may_wake`, which is only resuming), `may_stop_own_sessions`
        (`/kill`, and only a session this binding created or routed to),
        `may_transcribe` (voice notes are sent to OpenAI for transcription
        with the lodge's key, within the daily byte and request budget),
        `may_manage_apps` (`/apps`: list the lodge's apps, start and stop
        any of them; lodge-wide, so an owner's grant), `any_wolt` (every chat
        wolt the lodge has, read at each use, is selectable: the owner's own
        chat, as the adapter treated it).
        Residual trust, stated plainly: a malicious connector can forge an
        allowed sender and use any grant turned on here.
        """
        limit = self.limits.id_max_chars
        if _provider_id(sender, limit) is None:
            raise ConnectorError(400, "invalid_sender")
        if _provider_id(conversation, limit) is None:
            raise ConnectorError(400, "invalid_conversation")
        if thread is not None and _provider_id(thread, limit) is None:
            raise ConnectorError(400, "invalid_thread")
        if not valid_id(default_wolt, 64):
            raise ConnectorError(400, "invalid_wolt")
        selectable = []
        for name in selectable_wolts:
            if not valid_id(name, 64):
                raise ConnectorError(400, "invalid_wolt", f"{name!r} is not a wolt name")
            if name not in selectable:
                selectable.append(name)
        for value, label in ((create_per_hour, "create_per_hour"), (create_concurrent, "create_concurrent")):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 1000):
                raise ConnectorError(400, "invalid_limit", f"{label} must be an integer from 0 to 1000")
        with self._lock():
            registry = self._registry()
            instance = self._instance(registry, instance_id)
            key = (instance_id, sender, conversation, thread)
            existing = next(
                (b for b in registry["bindings"]
                 if (b["instance_id"], b["sender"], b["conversation"], b.get("thread")) == key),
                None,
            )
            if _adopt_route is not None:
                # The lodge's own carry-over (see adopt_chat): once per sender,
                # never over a binding that is already there.
                if instance.get("adoption") != "pending":
                    raise ConnectorError(409, "not_adopting")
                adopted = instance.setdefault("adopted", [])
                if sender in adopted:
                    return None
                if existing is not None:
                    adopted.append(sender)
                    self._write_json(self.registry_path, registry)
                    return None
            binding = {
                "binding_id": existing["binding_id"] if existing else secrets.token_hex(8),
                "instance_id": instance_id, "account": instance["account"],
                "sender": sender, "conversation": conversation, "thread": thread,
                "default_wolt": default_wolt, "may_wake": bool(may_wake),
                "selectable_wolts": selectable, "may_create": bool(may_create),
                "may_stop_own_sessions": bool(may_stop_own_sessions),
                "may_transcribe": bool(may_transcribe),
                "may_manage_apps": bool(may_manage_apps),
                "any_wolt": bool(any_wolt),
                "create_per_hour": self.limits.create_per_hour if create_per_hour is None else create_per_hour,
                "create_concurrent": self.limits.create_concurrent if create_concurrent is None else create_concurrent,
            }
            if _adopt_route is not None:
                # The route first (a crash after it leaves an unused entry and
                # the sender not adopted: redone whole), then the binding and
                # the progress mark in one registry write.
                queue = self._queue(instance_id)
                queue.routing[binding["binding_id"]] = dict(_adopt_route)
                self._save_queue(instance_id, queue)
                instance["adopted"].append(sender)
            registry["bindings"] = [b for b in registry["bindings"] if b is not existing]
            registry["bindings"].append(binding)
            self._write_json(self.registry_path, registry)
            return dict(binding)

    def adopt_chat(self, instance_id: str, *, sender: str, route: dict, **grants) -> dict | None:
        """Carry one adapter-era chat over, once: its binding, its route and the progress mark together.

        Only on an instance the lodge is still adopting (`adoption:
        pending`). A sender adopted before is never bound again, even after
        the owner removed or changed that binding; a binding the owner made
        for that chat first is left as it is. Answers the new binding, or
        None when there was nothing to do.
        """
        return self.bind(instance_id, sender=sender, conversation=sender, _adopt_route=route, **grants)

    def unbind(self, instance_id: str, *, sender: str, conversation: str, thread: str | None = None) -> bool:
        """Withdraw a grant. Its queued and leased deliveries are cancelled on the next read."""
        with self._lock():
            registry = self._registry()
            before = len(registry["bindings"])
            registry["bindings"] = [
                b for b in registry["bindings"]
                if (b["instance_id"], b["sender"], b["conversation"], b.get("thread"))
                != (instance_id, sender, conversation, thread)
            ]
            if len(registry["bindings"]) != before:
                self._write_json(self.registry_path, registry)
            return len(registry["bindings"]) != before

    @staticmethod
    def _binding_for(registry: dict, instance_id: str, sender: str, conversation: str, thread: str | None):
        """The binding an event lands in, or the reason it has none."""
        sender_known = False
        for binding in registry["bindings"]:
            if binding["instance_id"] != instance_id or binding["sender"] != sender:
                continue
            sender_known = True
            if binding["conversation"] != conversation:
                continue
            if binding.get("thread") is not None and binding["thread"] != thread:
                continue
            return binding, None
        return None, "conversation_not_bound" if sender_known else "sender_not_allowed"

    # ------------------------------------------------------------ credentials

    def lodge_id(self) -> str:
        """This lodge's identity for handoffs: random, made once, kept in the registry."""
        with self._lock():
            registry = self._registry()
            if not registry.get("lodge_id"):
                registry["lodge_id"] = secrets.token_hex(16)
                self._write_json(self.registry_path, registry)
            return registry["lodge_id"]

    def issue_token(self, instance_id: str, *, pending: bool = False) -> str:
        """Mint the next generation's token; see `mint`."""
        return self.mint(instance_id, pending=pending)["token"]

    def mint(self, instance_id: str, *, pending: bool = False) -> dict:
        """Mint the next generation's token; older generations are revoked.

        One registry write, and everything the caller must know about what
        it minted comes back from inside that same lock: `token`,
        `generation`, `issued` and `lodge_id`. Nothing is re-read afterwards,
        so a concurrent mint can never be mistaken for this one. Leases the
        old generation held are released when the queue is next read; the
        queue itself is untouched. Called when a runner is actually
        relaunched, never because the lodge restarted. A `pending` token (a
        handoff in flight) authenticates nothing until `activate`; an unused
        one is revoked by whoever minted it.
        """
        with self._lock():
            registry = self._registry()
            instance = self._instance(registry, instance_id)
            if not registry.get("lodge_id"):
                registry["lodge_id"] = secrets.token_hex(16)
            instance["generation"] += 1
            now = self.clock()
            for token in registry["tokens"]:
                if token["instance_id"] == instance_id and token.get("revoked") is None:
                    token["revoked"] = now
            plain = TOKEN_PREFIX + secrets.token_urlsafe(32)
            registry["tokens"].append({
                "digest": _digest(plain), "instance_id": instance_id,
                "generation": instance["generation"], "issued": now, "revoked": None,
                "pending": pending,
            })
            registry["tokens"] = [
                t for t in registry["tokens"]
                if t.get("revoked") is None or now - t["revoked"] < 86400
            ]
            self._write_json(self.registry_path, registry)
            return {
                "token": plain, "generation": instance["generation"], "issued": now,
                "lodge_id": registry["lodge_id"], "instance_id": instance_id,
            }

    def revoke_generation(self, instance_id: str, generation: int) -> int:
        """Revoke exactly one generation's tokens; a newer generation is untouched."""
        with self._lock():
            registry = self._registry()
            self._instance(registry, instance_id)
            now, count = self.clock(), 0
            for token in registry["tokens"]:
                if (
                    token["instance_id"] == instance_id and token["generation"] == generation
                    and token.get("revoked") is None
                ):
                    token["revoked"] = now
                    count += 1
            if count:
                self._write_json(self.registry_path, registry)
            return count

    def token_state(self, instance_id: str, generation: int) -> str | None:
        """'pending', 'active' or 'revoked' for a generation's token; None if there is none."""
        registry = self._registry()
        for token in registry["tokens"]:
            if token["instance_id"] == instance_id and token["generation"] == generation:
                if token.get("revoked") is not None:
                    return "revoked"
                return "pending" if token.get("pending") else "active"
        return None

    def revoke_stale_pending(self, older_than: float) -> list[tuple[str, int]]:
        """Revoke every pending generation issued before `older_than`; the handoff never came."""
        with self._lock():
            registry = self._registry()
            now, revoked = self.clock(), []
            for token in registry["tokens"]:
                if token.get("pending") and token.get("revoked") is None and token["issued"] < older_than:
                    token["revoked"] = now
                    revoked.append((token["instance_id"], token["generation"]))
            if revoked:
                self._write_json(self.registry_path, registry)
            return revoked

    def revoke_tokens(self, instance_id: str) -> int:
        with self._lock():
            registry = self._registry()
            self._instance(registry, instance_id)
            now, count = self.clock(), 0
            for token in registry["tokens"]:
                if token["instance_id"] == instance_id and token.get("revoked") is None:
                    token["revoked"] = now
                    count += 1
            if count:
                self._write_json(self.registry_path, registry)
            return count

    def _principal(self, registry: dict, digest: str) -> Principal | None:
        for record in registry["tokens"]:
            if hmac.compare_digest(record["digest"], digest) and record.get("revoked") is None:
                if record.get("pending"):
                    # A handoff in flight: nothing but activation accepts it.
                    return None
                instance = registry["instances"].get(record["instance_id"])
                if instance is None or instance["generation"] != record["generation"]:
                    return None
                return Principal(
                    record["instance_id"], record["generation"], instance["channel"],
                    instance["account"], bool(instance["enabled"]), digest,
                )
        return None

    def authenticate(self, token: object) -> Principal | None:
        """The principal a bearer token proves, or None for anything else.

        An unreadable registry raises: no token is believed on a guess, and
        the caller must never fall back to the token-less owner path. A
        pending token (a handoff not yet activated) is refused here.
        """
        if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX) or len(token) > 128:
            return None
        return self._principal(self._registry(), _digest(token))

    def activate(self, token: object, *, lodge_id: object, instance_id: object, generation: object) -> Principal:
        """Turn a pending generation into a live one: the connector's acknowledgement.

        Under the lock, so exactly one activation wins and a revocation racing
        it leaves one outcome: the token must be pending and unrevoked, what
        the caller expects (lodge, instance, generation, told to it apart
        from the file) must be what the registry says, and the window since
        the mint must not have run out. Anything else is a 401 and leaves
        the record as it was; a timed-out generation is revoked on the spot.
        """
        if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX) or len(token) > 128:
            raise ConnectorError(401, "connector_token_required")
        digest = _digest(token)
        with self._lock():
            registry = self._registry()
            record = next(
                (r for r in registry["tokens"] if hmac.compare_digest(r["digest"], digest)), None,
            )
            if record is None or record.get("revoked") is not None or not record.get("pending"):
                raise ConnectorError(401, "connector_token_required")
            now = self.clock()
            if now - record["issued"] > self.limits.handoff_window_seconds:
                record["revoked"] = now
                self._write_json(self.registry_path, registry)
                raise ConnectorError(401, "connector_token_required", "the handoff window has passed")
            instance = registry["instances"].get(record["instance_id"])
            if (
                instance is None or str(lodge_id) != str(registry.get("lodge_id"))
                or str(instance_id) != record["instance_id"]
                or str(generation) != str(record["generation"])
                or instance["generation"] != record["generation"]
            ):
                raise ConnectorError(401, "connector_token_required", "the activation does not match the registry")
            record["pending"] = False
            record["activated"] = now
            self._write_json(self.registry_path, registry)
            return Principal(
                record["instance_id"], record["generation"], instance["channel"],
                instance["account"], bool(instance["enabled"]), digest,
            )

    def _live(self, registry: dict, who: Principal) -> Principal:
        """The principal as the registry has it now, or a refusal.

        Called under the lock by every operation: a request that waited on
        its body or a stream carries a snapshot, and the owner may have
        revoked, rotated or removed in the meantime.
        """
        current = self._principal(registry, who.digest) if who.digest else None
        if current is None or (current.instance_id, current.generation) != (who.instance_id, who.generation):
            raise ConnectorError(401, "connector_token_required", "connector token required")
        return current

    # ------------------------------------------------------------- sweeping

    def _finish(self, instance_id: str, delivery: dict, status: str, error: str, tx: _Tx) -> None:
        delivery.update({
            "status": status, "lease": None, "error": error[: self.limits.error_max_chars],
            "finished": self.clock(),
        })
        self._drop_files(delivery)
        tx.pending.append((instance_id, dict(delivery)))
        tx.dirty.add(instance_id)

    def _drop_files(self, delivery: dict) -> None:
        for attachment in delivery.get("attachments", ()):
            self._file_path(attachment["file_id"]).unlink(missing_ok=True)

    def _attempt_failed(self, instance_id: str, delivery: dict, error: str, tx: _Tx, *, backoff: bool) -> None:
        """One more failed attempt; retry (with backoff after a nack), or give up."""
        delivery["failed_attempts"] = delivery.get("failed_attempts", 0) + 1
        delivery["lease"] = None
        delivery["last_error"] = error[: self.limits.error_max_chars]
        if delivery["failed_attempts"] >= self.limits.max_attempts:
            self._finish(
                instance_id, delivery, "failed", f"{delivery['failed_attempts']} failed attempts: {error}", tx,
            )
            return
        step = min(delivery["failed_attempts"], len(self.limits.backoff_seconds)) - 1
        delivery["status"] = "queued"
        delivery["not_before"] = self.clock() + (self.limits.backoff_seconds[step] if backoff else 0)

    def _window_used(self, delivery: dict, instance: dict) -> float:
        """Enabled seconds since the first lease: disabled time is subtracted."""
        started = delivery.get("clock_started")
        if started is None:
            return 0.0
        now = self.clock()
        paused = instance.get("paused_seconds", 0.0) - delivery.get("paused_base", 0.0)
        if instance.get("disabled_at") is not None:
            paused += max(0.0, now - max(instance["disabled_at"], started))
        return max(0.0, now - started - paused)

    def _sweep(self, instance_id: str, queue: _Queue, registry: dict, tx: _Tx) -> None:
        """Apply the registry and the clocks to a queue, before anything reads it.

        Marks the queue dirty: the transaction writes it before it ends, on
        every protocol-defined way out, whether or not the operation itself
        gets as far as a write.
        """
        now = self.clock()
        limits = self.limits
        tx.dirty.add(instance_id)
        instance = registry["instances"].get(instance_id) or {"enabled": False, "generation": 0}
        bindings = {b["binding_id"] for b in registry["bindings"] if b["instance_id"] == instance_id}
        for delivery in queue.deliveries:
            if delivery["status"] in _TERMINAL:
                continue
            if delivery.get("binding_id") not in bindings:
                # The owner withdrew the destination: queued or leased, it is over.
                self._finish(instance_id, delivery, "cancelled", "binding removed", tx)
                continue
            lease = delivery.get("lease")
            if lease and lease["generation"] < instance["generation"]:
                # Rotation: the old process is gone; not a failed attempt.
                delivery["lease"] = None
                delivery["status"] = "queued"
                lease = None
            if lease and lease["expires"] <= now:
                self._attempt_failed(instance_id, delivery, "lease expired without an ack", tx, backoff=False)
                if delivery["status"] in _TERMINAL:
                    continue
            for attachment in delivery.get("attachments", ()):
                if attachment["expires"] <= now:
                    # Disabled or not: a delivery never waits without its file.
                    self._finish(instance_id, delivery, "failed", "attachment retention expired", tx)
                    break
            if delivery["status"] in _TERMINAL:
                continue
            if (
                instance["enabled"] and not delivery.get("lease")
                and self._window_used(delivery, instance) >= limits.retry_window_seconds
            ):
                self._finish(
                    instance_id, delivery, "failed",
                    f"{delivery.get('failed_attempts', 0)} failed attempts in the retry window", tx,
                )
        queue.deliveries = [
            d for d in queue.deliveries
            if not (d["status"] in _TERMINAL and now - d.get("finished", now) > limits.dedup_retention_seconds)
        ]
        unresolved = {
            a["upload_id"] for r in queue.inbound.values() if r["state"] in ("accepted", "handoff_failed")
            for a in r["event"].get("attachments", ())
        }
        for upload_id, handle in list(queue.uploads.items()):
            if upload_id in unresolved:
                # Claimed by an event still waiting for its handoff: kept, whatever the clock says.
                continue
            if handle.get("state") == "in_flight":
                stale = now - handle.get("started", now) > limits.upload_ttl_seconds
                if stale or not _pid_alive(handle.get("pid")):
                    # The process that was streaming it is gone: reclaim the bytes.
                    self._part_path(instance_id, upload_id).unlink(missing_ok=True)
                    (self._upload_dir(instance_id) / upload_id).unlink(missing_ok=True)
                    del queue.uploads[upload_id]
            elif handle["expires"] <= now:
                (self._upload_dir(instance_id) / upload_id).unlink(missing_ok=True)
                del queue.uploads[upload_id]
        try:
            for part in self._upload_dir(instance_id).glob(".*.part"):
                if part.name[1:-5] not in queue.uploads:
                    part.unlink(missing_ok=True)
        except OSError:
            pass
        queue.dedup = {
            key: record for key, record in queue.dedup.items()
            if now - record["at"] < limits.dedup_retention_seconds
        }
        queue.counters["events"] = [t for t in queue.counters["events"] if now - t < 60]
        queue.counters["uploads"] = [
            entry for entry in queue.counters["uploads"] if now - entry[0] < 3600
        ]
        for key, record in list(queue.inbound.items()):
            if record["state"] in ("handed_off", "cancelled") and now - record["at"] >= limits.dedup_retention_seconds:
                del queue.inbound[key]
            elif record["state"] == "accepted" and tx.dispatch and (
                record.get("tried") is None or now - record["tried"] >= limits.handoff_retry_seconds
            ):
                # Still waiting for its receipt. Claim another attempt only
                # under the current policy: the binding must still exist and
                # the instance must be enabled and owned by the connector.
                binding_id = record["event"]["binding"].get("binding_id")
                if binding_id not in bindings:
                    record["state"] = "cancelled"
                    record["result"] = {"routed": "cancelled", "reason": "binding_removed"}
                    record["finished"] = now
                    continue
                if not (instance["enabled"] and self.runtime_active(instance.get("channel", ""))):
                    continue
                if self._routing_now(instance_id, key):
                    # An attempt is routing it right now: neither a new claim
                    # nor exhaustion; its receipt settles the event.
                    continue
                if record.get("attempts", 0) >= limits.handoff_max_attempts:
                    record["state"] = "handoff_failed"
                    continue
                record["tried"] = now
                record["attempts"] = record.get("attempts", 0) + 1
                record["claim"] = secrets.token_hex(8)
                tx.pending_handoffs.append((instance_id, key, record["claim"]))
        for tkey, record in list(queue.transcriptions.items()):
            inbound = queue.inbound.get(tkey.rsplit("\x00", 1)[0])
            if inbound is None:
                # The event is gone (past dedup retention): nothing can retry it.
                del queue.transcriptions[tkey]
            elif inbound["state"] in ("handed_off", "cancelled") and "text" in (record.get("disposition") or {}):
                # Delivered or cancelled: keep the tombstone (its attempt count), not the body.
                record["disposition"] = {"reason": "delivered" if inbound["state"] == "handed_off" else "cancelled"}
        queue.provider_messages = {
            key: record for key, record in queue.provider_messages.items()
            if now - record["at"] < limits.dedup_retention_seconds
        }
        queue.callbacks = {
            key: record for key, record in queue.callbacks.items() if record["expires"] > now
        }
        queue.operations = {
            key: record for key, record in queue.operations.items()
            if record["state"] in ("reserved", "effect_in_progress", "started", "uncertain") or now - record["at"] < limits.dedup_retention_seconds
        }

    # --------------------------------------------------------------- inbound

    def _parse_event(self, payload: object) -> dict:
        """Bound and normalize an inbound event, or say what is wrong with it.

        The schema is closed: a key the protocol does not name is a fault.
        """
        limits = self.limits
        if not isinstance(payload, dict):
            raise ConnectorError(400, "malformed_event", "event must be an object")
        if "ignore" in payload:
            # A terminal-ignore disposition: the connector saw an update it
            # cannot carry (a service message, an unsupported kind) and wants
            # the position answered so its cursor can pass it.
            if not set(payload) <= _IGNORE_KEYS:
                raise ConnectorError(400, "malformed_event", "unknown field in ignore")
            event_id = _provider_id(payload.get("event_id"), limits.id_max_chars)
            reason = payload.get("ignore")
            if event_id is None or not valid_id(reason, 64):
                raise ConnectorError(400, "malformed_event", "ignore needs an event_id and a reason id")
            cursor = payload.get("cursor")
            if cursor is not None and (
                isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0
                or len(str(cursor)) > limits.cursor_max_chars
            ):
                raise ConnectorError(400, "malformed_event", "cursor must be a non-negative integer")
            return {"event_id": event_id, "ignore": reason, "cursor": cursor, "attachments": []}
        if not set(payload) <= _EVENT_KEYS:
            raise ConnectorError(400, "malformed_event", "unknown field in event")
        event_id = _provider_id(payload.get("event_id"), limits.id_max_chars)
        if event_id is None:
            raise ConnectorError(400, "malformed_event", "event_id is required")
        sender, conversation = payload.get("sender"), payload.get("conversation")
        if not isinstance(sender, dict) or not isinstance(conversation, dict):
            raise ConnectorError(400, "malformed_event", "sender and conversation are required")
        if not set(sender) <= _SENDER_KEYS or not set(conversation) <= _CONVERSATION_KEYS:
            raise ConnectorError(400, "malformed_event", "unknown field in sender or conversation")
        sender_id = _provider_id(sender.get("provider_id"), limits.id_max_chars)
        conversation_id = _provider_id(conversation.get("provider_id"), limits.id_max_chars)
        if sender_id is None or conversation_id is None:
            raise ConnectorError(400, "malformed_event", "provider ids are required")
        display = sender.get("display", "")
        if not isinstance(display, str) or len(display) > limits.display_max_chars:
            raise ConnectorError(400, "malformed_event", "display is not a short string")
        thread = conversation.get("thread")
        if thread is not None and _provider_id(thread, limits.id_max_chars) is None:
            raise ConnectorError(400, "malformed_event", "thread is not a provider id")
        callback = payload.get("callback")
        callback_id = None
        if callback is not None:
            if not isinstance(callback, dict) or set(callback) != _CALLBACK_KEYS:
                raise ConnectorError(400, "malformed_event", "callback needs exactly an id")
            callback_id = callback["id"]
            if (
                not isinstance(callback_id, str) or not callback_id.startswith(CALLBACK_PREFIX)
                or outbox.ENTRY_ID_RE.fullmatch(callback_id[len(CALLBACK_PREFIX):]) is None
            ):
                raise ConnectorError(400, "malformed_event", "callback id is not one core issued")
        text = payload.get("text", "")
        if not isinstance(text, str):
            raise ConnectorError(400, "malformed_event", "text must be a string")
        if len(text.encode("utf-8")) > limits.text_max_bytes:
            raise ConnectorError(413, "text_too_long", f"text is over {limits.text_max_bytes} bytes")
        attachments = payload.get("attachments")
        attachments = [] if attachments is None else attachments
        if not isinstance(attachments, list) or len(attachments) > limits.event_max_attachments:
            raise ConnectorError(400, "malformed_event", "too many attachments")
        upload_ids = []
        for entry in attachments:
            if not isinstance(entry, dict) or set(entry) != {"upload_id"}:
                raise ConnectorError(400, "malformed_event", "attachment needs exactly an upload_id")
            upload_id = entry["upload_id"]
            if not isinstance(upload_id, str) or outbox.ENTRY_ID_RE.fullmatch(upload_id) is None:
                raise ConnectorError(400, "malformed_event", "attachment needs an upload_id")
            upload_ids.append(upload_id)
        if len(set(upload_ids)) != len(upload_ids):
            raise ConnectorError(400, "malformed_event", "an upload is named twice")
        reply_to = payload.get("reply_to")
        reply_ref = None
        if reply_to is not None:
            if (
                not isinstance(reply_to, dict) or len(reply_to) != 1
                or not set(reply_to) <= _REPLY_TO_KEYS
            ):
                raise ConnectorError(
                    400, "malformed_event", "reply_to needs exactly a delivery_id or a provider_message_id",
                )
            (kind, value), = reply_to.items()
            if kind == "delivery_id":
                if not isinstance(value, str) or outbox.ENTRY_ID_RE.fullmatch(value) is None:
                    raise ConnectorError(400, "malformed_event", "reply_to needs a delivery_id")
            elif _provider_id(value, limits.id_max_chars) is None:
                raise ConnectorError(400, "malformed_event", "reply_to needs a provider message id")
            reply_ref = [kind, value]
        cursor = payload.get("cursor")
        if cursor is not None and (
            isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0
            or len(str(cursor)) > limits.cursor_max_chars
        ):
            raise ConnectorError(400, "malformed_event", "cursor must be a non-negative integer")
        return {
            "event_id": event_id, "sender": sender_id, "display": display,
            "conversation": conversation_id, "thread": thread, "text": text,
            "attachments": upload_ids, "reply_to": reply_ref, "cursor": cursor,
            "callback": callback_id,
        }

    def accept_event(self, who: Principal, payload: object) -> tuple[int, dict]:
        """Admit one provider event: dedup, binding, reply scope, uploads, route.

        Returns (status, body). Policy drops are 200 with accepted=false and
        are final: a provider must not retry them, and the same event id
        gets the same answer for seven days, whatever changed since. That
        includes `instance_disabled`: an event that reaches a disabled
        instance is dropped, not buffered, so a connector stops forwarding
        while its lease polls answer `paused`. Protocol faults are 4xx;
        a 429 may be retried.

        The claim (dedup record, upload handles) is on disk before the
        router runs, outside the lock. A crash between the two can lose or
        repeat the event at the session: the fixture-slice limit the module
        docstring names.
        """
        event = self._parse_event(payload)
        # The digest leaves out the display name (shown, never routed on) and
        # keeps the cursor: the position is part of the event's identity, so a
        # replay cannot relabel it.
        digest = _canonical({k: v for k, v in event.items() if k != "display"})
        key = f"{who.account}\x00{event['event_id']}"
        with self._transaction() as tx:
            registry = self._registry()
            who = self._live(registry, who)
            if not self.runtime_active(who.channel):
                raise ConnectorError(
                    409, "runtime_not_connector", f"{who.channel} is served by its adapter, not a connector",
                )
            queue = self._load(tx, who.instance_id)
            self._sweep(who.instance_id, queue, registry, tx)
            now = self.clock()

            def answered() -> None:
                # A final answer for this position: the connector may move past it.
                if event["cursor"] is not None:
                    queue.answered_cursor = max(queue.answered_cursor or -1, event["cursor"])
                    tx.dirty.add(who.instance_id)

            record = queue.dedup.get(key)
            if record is not None:
                # Same answer first, before any question about its uploads.
                if record["digest"] != digest:
                    self._release_unclaimed(who.instance_id, queue, event["attachments"])
                    self._commit(tx, who.instance_id)
                    raise ConnectorError(409, "event_conflict", "event_id seen with another payload")
                answered()
                return record["status"], dict(record["body"])
            if len(queue.counters["events"]) >= self.limits.events_per_minute:
                self._commit(tx, who.instance_id)
                raise ConnectorError(429, "events_rate_limited")
            queue.counters["events"].append(now)

            def drop(reason: str) -> tuple[int, dict]:
                body = {"accepted": False, "reason": reason, "event_id": event["event_id"]}
                self._release_unclaimed(who.instance_id, queue, event["attachments"])
                queue.dedup[key] = {"digest": digest, "status": 200, "body": body, "at": now}
                answered()
                self._commit(tx, who.instance_id)
                return 200, body

            if event.get("ignore"):
                return drop(f"ignored_{event['ignore']}")
            if not event["text"] and not event["attachments"] and not event.get("callback"):
                return drop("empty_event")
            if not who.enabled:
                return drop("instance_disabled")
            binding, reason = self._binding_for(
                registry, who.instance_id, event["sender"], event["conversation"], event["thread"],
            )
            if binding is None:
                return drop(reason)
            reply_session = None
            reply_quote = None
            if event["reply_to"] is not None:
                kind, value = event["reply_to"]
                if kind == "delivery_id":
                    target = next(
                        (d for d in queue.deliveries if d["delivery_id"] == value), None,
                    )
                    target = {
                        "binding_id": target.get("binding_id"), "session": target.get("session"),
                        "quote": _quote_of(target),
                    } if target else None
                else:
                    # Keyed by account and conversation: a message id is only
                    # unique inside its chat, and another account's never resolve here.
                    target = queue.provider_messages.get(f"{who.account}\x00{event['conversation']}\x00{value}")
                # The exact binding, not a conversation that looks the same.
                if target is None or target.get("binding_id") != binding["binding_id"]:
                    return drop("reply_to_out_of_binding")
                reply_session = target.get("session")
                reply_quote = target.get("quote")
            claimed = []
            for upload_id in event["attachments"]:
                handle = queue.uploads.get(upload_id)
                if handle is None or handle.get("state") != "stored" or handle["expires"] <= now:
                    return drop("upload_expired")
                if handle.get("claimed_by") is not None:
                    return drop("upload_consumed")
                claimed.append((upload_id, handle))
            for upload_id, handle in claimed:
                handle["claimed_by"] = event["event_id"]
            body = {
                "accepted": True, "event_id": event["event_id"],
                "wolt": binding["default_wolt"], "may_wake": binding["may_wake"],
                "session": reply_session,
            }
            accepted = AcceptedEvent(
                who.instance_id, who.channel, who.account, event["event_id"], self._effective(binding),
                {"provider_id": event["sender"], "display": outbox.display_name(event["display"]) if event["display"] else ""},
                {"provider_id": event["conversation"], "thread": event["thread"]},
                event["text"],
                tuple(
                    {
                        "upload_id": upload_id, "path": str(self._upload_dir(who.instance_id) / upload_id),
                        "name": handle["name"], "size": handle["size"],
                    } for upload_id, handle in claimed
                ),
                reply_session, event["cursor"], event.get("callback"), reply_quote,
            )
            queue.dedup[key] = {"digest": digest, "status": 200, "body": body, "at": now}
            queue.inbound[key] = {"state": "accepted", "event": accepted.to_record(), "at": now}
            if tx.dispatch:
                claim = secrets.token_hex(8)
                queue.inbound[key].update({"tried": now, "attempts": 1, "claim": claim})
                tx.pending_handoffs.append((who.instance_id, key, claim))
            answered()
            self._commit(tx, who.instance_id)
        # Durable first; the dispatcher ran after the lock (see _transaction)
        # and its result, if any, is in the record by now.
        with self._lock():
            record = self._queue(who.instance_id).inbound.get(key) or {}
        return 200, {**body, **(record.get("result") or {"routed": "pending"})}

    def _hand_off(self, instance_id: str, key: str, claim: str) -> dict:
        """Run the dispatcher for one committed claim and write its receipt.

        The current policy is read again first: a binding that is gone
        cancels the record; a disabled instance or a channel the connector
        does not own leaves it waiting (the claim is given back, and the
        next one has a new identity). The event's binding snapshot is
        refreshed, so a changed grant (default wolt, may_wake) is what the
        router sees.

        One attempt at a time decides where the event goes and acts on it:
        the whole route decision and its effect (a paste, a session start)
        run under the event's route lock, taken without waiting, before the
        claim is checked. An attempt that finds it held gives its claim back
        and acts on nothing; one that takes it after another attempt
        finished finds the event over. A claim can be overtaken while its
        attempt still runs (the retry interval passed, the owner retried or
        paused and resumed), but no other attempt can act meanwhile, so the
        holder's receipt is the event's: it is written whenever the event is
        still waiting, not only while `claim` is still the record's. An
        event resolved or cancelled meanwhile keeps that outcome.
        """
        if self.router is None:
            return {}
        try:
            with self._route_lock(instance_id, key):
                return self._hand_off_owned(instance_id, key, claim)
        except BlockingIOError:
            # Another attempt is routing this event right now: never a second
            # decision or effect beside it. This claim is given back.
            with self._lock():
                record = self._queue(instance_id).inbound.get(key)
                if record is not None and record["state"] == "accepted" and record.get("claim") == claim:
                    queue = self._queue(instance_id)
                    record = queue.inbound[key]
                    record["attempts"] = max(record.get("attempts", 1) - 1, 0)
                    record["tried"] = self.clock()
                    record["claim"] = None
                    self._save_queue(instance_id, queue)
            return {"routed": "deferred", "reason": "routing_in_progress"}

    @contextlib.contextmanager
    def _route_lock(self, instance_id: str, key: str):
        """The event's route lock: one attempt decides and acts at a time, across threads and processes.

        One of a fixed set of lock files, chosen by the event, so they never
        pile up. Two events sharing one while both route is rare and only
        defers the later one to its next attempt.
        """
        stripe = int(hashlib.sha256(f"{instance_id}\x00{key}".encode()).hexdigest(), 16) % ROUTE_LOCK_STRIPES
        path = self.root / "routing" / f"{stripe:04d}.lock"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(path, "a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _routing_now(self, instance_id: str, key: str) -> bool:
        """Whether an attempt holds the event's route lock at this moment (never waits)."""
        try:
            with self._route_lock(instance_id, key):
                return False
        except BlockingIOError:
            return True

    def _hand_off_owned(self, instance_id: str, key: str, claim: str) -> dict:
        """`_hand_off` with the event's route lock held."""
        with self._lock():
            registry = self._registry()
            queue = self._queue(instance_id)
            record = queue.inbound.get(key)
            if record is None or record["state"] != "accepted" or record.get("claim") != claim:
                return {}
            instance = registry["instances"].get(instance_id)
            current = next(
                (b for b in registry["bindings"]
                 if b["instance_id"] == instance_id
                 and b["binding_id"] == record["event"]["binding"].get("binding_id")),
                None,
            )
            if instance is None or current is None:
                record.update({
                    "state": "cancelled", "finished": self.clock(),
                    "result": {"routed": "cancelled", "reason": "binding_removed"},
                })
                self._save_queue(instance_id, queue)
                return {}
            if not (instance["enabled"] and self.runtime_active(instance["channel"])):
                # Give the claim back: not an attempt, just not now. Its
                # identity is spent; the next claim is a new one.
                record["attempts"] = record.get("attempts", 1) - 1
                record["tried"] = None
                record["claim"] = None
                self._save_queue(instance_id, queue)
                return {}
            record["event"]["binding"] = dict(current)
            self._save_queue(instance_id, queue)
            event = AcceptedEvent.from_record(record["event"])
        self._held.nested = getattr(self._held, "nested", 0) + 1
        try:
            routed = self.router(event) or {}
        except Exception as exc:
            routed = {"routed": "error", "error": str(exc)[: self.limits.error_max_chars]}
        finally:
            self._held.nested -= 1
        with self._lock():
            queue = self._queue(instance_id)
            record = queue.inbound.get(key)
            if record is None or record["state"] != "accepted":
                # Resolved or cancelled meanwhile: that outcome stands. A claim
                # taken meanwhile acted on nothing (it could not take the
                # route lock), so this attempt's receipt is the event's.
                return {}
            record["claim"] = claim
            if routed.get("routed") == "deferred":
                # Waiting on something bounded elsewhere (a transcription in
                # flight): give the claim back without spending an attempt;
                # the next try waits the ordinary retry interval.
                record["attempts"] = max(record.get("attempts", 1) - 1, 0)
                record["tried"] = self.clock()
                record["claim"] = None
                self._save_queue(instance_id, queue)
                return routed
            if routed.get("routed") == "error":
                # No receipt: the next connector-driven read claims another
                # attempt, up to the cap; then the record is parked.
                record["last_error"] = routed.get("error", "")
                if record.get("attempts", 0) >= self.limits.handoff_max_attempts:
                    record["state"] = "handoff_failed"
            else:
                record["state"] = "handed_off"
                record["finished"] = self.clock()
            record["result"] = routed
            dedup = queue.dedup.get(key)
            if dedup is not None:
                dedup["body"] = {**dedup["body"], **routed}
            self._save_queue(instance_id, queue)
        return routed

    def record_attachment_destinations(self, instance_id: str, key: str, destinations: dict) -> None:
        """Persist where each of an event's files is going before it moves.

        Event-owned and stable: every later attempt names the same paths.
        """
        with self._lock():
            queue = self._queue(instance_id)
            record = queue.inbound.get(key)
            if record is None:
                return
            for attachment in record["event"].get("attachments", ()):
                dest = destinations.get(attachment["upload_id"])
                if dest is not None:
                    attachment["dest"] = str(dest)
            self._save_queue(instance_id, queue)

    def handoff_key(self, instance_id: str, event_id: str) -> str:
        """The internal record key for an event id the owner typed: the instance's account qualifies it."""
        registry = self._registry()
        return f"{self._instance(registry, instance_id)['account']}\x00{event_id}"

    def handoffs(self, instance_id: str) -> list[dict]:
        """Owner view: every accepted event not yet handed off, parked ones included.

        `event_id` is what the owner passes back to `retry_handoff` and
        `cancel_handoff`; the account-qualified key stays internal.
        """
        with self._lock():
            return [
                {"event_id": record["event"]["event_id"], **record}
                for record in self._queue(instance_id).inbound.values()
                if record["state"] in ("accepted", "handoff_failed")
            ]

    def retry_handoff(self, instance_id: str, event_id: str) -> bool:
        """Owner recovery: the attempt budget is reset, the claim identity is not reused.

        A dispatch still running from before the retry writes no receipt:
        its claim is no longer the record's.
        """
        key = self.handoff_key(instance_id, event_id)
        with self._lock():
            queue = self._queue(instance_id)
            record = queue.inbound.get(key)
            if record is None or record["state"] not in ("accepted", "handoff_failed"):
                return False
            record.update({"state": "accepted", "attempts": 0, "tried": None, "claim": None})
            self._save_queue(instance_id, queue)
            return True

    def cancel_handoff(self, instance_id: str, event_id: str) -> bool:
        """Owner recovery: give the event up; its files are released."""
        key = self.handoff_key(instance_id, event_id)
        with self._transaction(dispatch=False) as tx:
            queue = self._load(tx, instance_id)
            record = queue.inbound.get(key)
            if record is None or record["state"] not in ("accepted", "handoff_failed"):
                return False
            record.update({
                "state": "cancelled", "finished": self.clock(),
                "result": {"routed": "cancelled", "reason": "owner"},
            })
            self._release_unclaimed(instance_id, queue, [])
            for attachment in record["event"].get("attachments", ()):
                queue.uploads.pop(attachment["upload_id"], None)
                (self._upload_dir(instance_id) / attachment["upload_id"]).unlink(missing_ok=True)
            self._commit(tx, instance_id)
            return True

    def pending_handoffs(self, instance_id: str) -> list[dict]:
        """Accepted events whose receipt is not written yet (owner/diagnostic view)."""
        with self._lock():
            return [
                dict(record) for record in self._queue(instance_id).inbound.values()
                if record["state"] == "accepted"
            ]

    # ---------------------------------------------------------------- cursor

    def state(self, who: Principal) -> dict:
        """What a connector needs at start: its cursor and whether it may run. Claims nothing."""
        with self._transaction(dispatch=False) as tx:
            registry = self._registry()
            who = self._live(registry, who)
            queue = self._load(tx, who.instance_id)
            self._sweep(who.instance_id, queue, registry, tx)
            return {
                "instance_id": who.instance_id, "channel": who.channel, "account": who.account,
                "cursor": queue.cursor, "answered_cursor": queue.answered_cursor,
                "paused": not who.enabled, "runtime_active": self.runtime_active(who.channel),
                "run_lock": str(run_lock_path(self.root, who.channel)),
            }

    def set_cursor(self, who: Principal, cursor: object) -> dict:
        """Advance the provider cursor, never past what core has answered.

        `cursor` is the next position the connector will ask for (Telegram's
        offset: the last answered update_id plus one). It may equal the
        current cursor, move forward to at most answered + 1, and never move
        back; a channel whose runtime is not the connector keeps its cursor.

        What core can check is that the position just below the cursor got
        a final answer. That every earlier fetched update did too is the
        connector's ordered-batch responsibility: it answers a fetched batch
        in order, stops at the first answer that is not final, and posts
        only then. Provider ids may have gaps (positions that were never
        fetched are nobody's to answer); a position that was fetched is
        answered as an event or as an ignore before anything past it.
        """
        if (
            isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0
            or len(str(cursor)) > self.limits.cursor_max_chars
        ):
            raise ConnectorError(400, "malformed_cursor", "cursor must be a non-negative integer")
        with self._transaction() as tx:
            registry = self._registry()
            who = self._live(registry, who)
            if not self.runtime_active(who.channel):
                raise ConnectorError(409, "runtime_not_connector", f"{who.channel} is served by its adapter")
            queue = self._load(tx, who.instance_id)
            self._sweep(who.instance_id, queue, registry, tx)
            if queue.cursor is not None and cursor < queue.cursor:
                raise ConnectorError(409, "cursor_regressed", "the cursor never moves back")
            answered = -1 if queue.answered_cursor is None else queue.answered_cursor
            if cursor > answered + 1:
                raise ConnectorError(
                    409, "cursor_ahead", "core has not answered every update below that cursor",
                )
            queue.cursor = cursor
            self._commit(tx, who.instance_id)
            return {"cursor": queue.cursor, "answered_cursor": queue.answered_cursor}

    def seed_cursor(self, instance_id: str, cursor: int | None) -> None:
        """Owner-side: set the cursor outright (a handover from the adapter)."""
        with self._lock():
            registry = self._registry()
            self._instance(registry, instance_id)
            queue = self._queue(instance_id)
            queue.cursor = cursor
            if cursor is not None:
                queue.answered_cursor = max(queue.answered_cursor or -1, cursor - 1)
            self._save_queue(instance_id, queue)

    # --------------------------------------------------------------- routing

    def routing_state(self, instance_id: str, binding_id: str) -> dict:
        with self._lock():
            return dict(self._queue(instance_id).routing.get(binding_id) or {})

    def remember_route(self, instance_id: str, binding_id: str, **fields) -> None:
        """Per-binding routing memory for the router (the active session, for one)."""
        with self._lock():
            queue = self._queue(instance_id)
            state = dict(queue.routing.get(binding_id) or {})
            state.update(fields)
            queue.routing[binding_id] = state
            self._save_queue(instance_id, queue)

    def associate_session(self, instance_id: str, binding_id: str, session: str, *, via: str, wolt: str = "") -> None:
        """An explicit association: this binding created (`via="created"`) or routed to the session.

        `/sessions` lists and `/kill` may stop nothing else. The wolt is kept
        so a later send can check the session is still one this chat may
        talk to. A creation's hourly charge is recorded at reservation time
        (see `reserve_create`), not here.
        """
        with self._lock():
            queue = self._queue(instance_id)
            self._associate(queue, binding_id, session, via, wolt)
            self._save_queue(instance_id, queue)

    def _associate(self, queue: _Queue, binding_id: str, session: str, via: str, wolt: str) -> None:
        state = dict(queue.routing.get(binding_id) or {})
        sessions = dict(state.get("sessions") or {})
        if session not in sessions or via == "created":
            sessions[session] = {"via": via, "at": self.clock(), "wolt": wolt}
        elif wolt and not sessions[session].get("wolt"):
            sessions[session]["wolt"] = wolt
        state["sessions"] = sessions
        queue.routing[binding_id] = state

    def claim_callback(self, instance_id: str, binding_id: str, callback_id: object, key: str) -> dict | None:
        """The action behind a button press, bound to the operation `key`.

        Same binding, not expired, and either never consumed or consumed by
        this very operation (a retry resumes); anything else is None. A
        foreign id reveals nothing about the binding it belongs to.
        """
        if not isinstance(callback_id, str):
            return None
        with self._lock():
            queue = self._queue(instance_id)
            record = queue.callbacks.get(callback_id)
            if (
                record is None or record["binding_id"] != binding_id or record["expires"] <= self.clock()
                or record.get("consumed_by") not in (None, key)
            ):
                return None
            action = dict(record["action"])
            if record.get("consumed_by") != key or key not in queue.operations:
                # Consumption and the operation's durable action in one write:
                # a crash between them cannot lose the press, and a retry of
                # this event resumes from the operation whatever the TTL.
                record["consumed_by"] = key
                queue.operations.setdefault(key, {
                    "command": str(action.get("command", "")), "argument": action.get("argument"),
                    "binding_id": binding_id, "state": "reserved", "at": self.clock(),
                    "session": None, "wolt": None, "callback": callback_id,
                })
                self._save_queue(instance_id, queue)
            return action

    # ------------------------------------------------------------ operations

    def operation(self, instance_id: str, key: str) -> dict | None:
        with self._lock():
            record = self._queue(instance_id).operations.get(key)
            return dict(record) if record else None

    def begin_operation(self, instance_id: str, key: str, binding_id: str, command: str, argument: str | None) -> dict:
        """The durable record of one command, created once per event; a retry gets the same one back."""
        with self._lock():
            queue = self._queue(instance_id)
            record = queue.operations.get(key)
            if record is None:
                record = {
                    "command": command, "argument": argument, "binding_id": binding_id,
                    "state": "reserved", "at": self.clock(), "session": None, "wolt": None,
                }
                queue.operations[key] = record
                self._save_queue(instance_id, queue)
            return dict(record)

    def reserve_create(self, instance_id: str, key: str, binding_id: str, wolt: str, live_created: int) -> dict:
        """Reserve one `/new` for this event, or say why not; one locked read-modify-write.

        The grant is read again from the registry here: `may_create`, for a
        wolt the binding may talk to (`may_start`). The hourly count includes every creation
        reserved or made in the last hour; the concurrent count is
        `live_created` (the caller's liveness check of this binding's created
        sessions, made outside the lock) plus every reservation still in
        progress. A reservation already made for this event is returned as
        it is, so a retry never reserves twice.
        """
        with self._lock():
            registry = self._registry()
            binding = next((b for b in registry["bindings"] if b["binding_id"] == binding_id), None)
            binding = self._effective(binding) if binding is not None else None
            queue = self._queue(instance_id)
            existing = queue.operations.get(key)
            if existing is not None and existing["state"] != "reserved" or (
                existing is not None and existing.get("wolt")
            ):
                return dict(existing)
            if binding is None or not binding.get("may_create"):
                return {"refused": "no_grant"}
            if not may_start(binding, wolt):
                return {"refused": "not_selectable"}
            now = self.clock()
            def holds_a_reservation(r: dict) -> bool:
                # A record begun but not yet reserved (no wolt) holds nothing.
                return r["command"] == "new" and r["binding_id"] == binding_id and bool(r.get("wolt"))

            # In progress, or parked unresolved: each holds its concurrency slot.
            in_progress = [
                r for k, r in queue.operations.items()
                if k != key and holds_a_reservation(r)
                and r["state"] in ("reserved", "effect_in_progress", "started", "uncertain")
            ]
            # Charged for the hour: everything but a released (failed) reservation,
            # and not this event's own record-in-the-making.
            charged = [
                r for k, r in queue.operations.items()
                if k != key and holds_a_reservation(r) and r["state"] != "failed" and now - r["at"] < 3600
            ]
            if len(charged) >= binding.get("create_per_hour", 0):
                return {"refused": "rate_limited"}
            if live_created + len(in_progress) >= binding.get("create_concurrent", 0):
                return {"refused": "concurrency_limited"}
            record = existing or {
                "command": "new", "argument": wolt, "binding_id": binding_id, "state": "reserved",
                "at": now, "session": None,
            }
            record["wolt"] = wolt
            queue.operations[key] = record
            self._save_queue(instance_id, queue)
            return dict(record)

    def settle_operation(
        self, instance_id: str, key: str, state: str, *, expect: tuple[str, ...] | None = None,
        grant: str | None = None, target_wolt: str | None = None, **fields,
    ) -> dict:
        """Move an operation on: effect_in_progress, started(session), associated, answered, failed, uncertain.

        `effect_in_progress` is written before the external call; found
        after a crash it is never run again on its own. `associated` also
        records the association and the active route in the same write, so
        a crash cannot leave a started session unknown to its binding once
        this returns. `failed` releases the reservation (no hourly charge,
        no concurrency slot); `uncertain` keeps both until the owner
        resolves it.
        """
        with self._lock():
            queue = self._queue(instance_id)
            record = queue.operations.get(key)
            if record is None:
                raise ConnectorError(409, "unknown_operation")
            current = record["state"]
            if grant is not None and state == "effect_in_progress":
                # The durable effect boundary: the binding, its instance, the
                # channel's runtime and the grant are read again here, in the
                # same locked write that lets the effect happen.
                registry = self._registry()
                binding = next((b for b in registry["bindings"] if b["binding_id"] == record["binding_id"]), None)
                binding = self._effective(binding) if binding is not None else None
                instance = registry["instances"].get(instance_id)
                allowed = (
                    binding is not None and instance is not None and instance.get("enabled")
                    and self.runtime_active(instance.get("channel", ""))
                )
                if allowed and grant == "create":
                    allowed = may_start(binding, record.get("wolt"))
                elif allowed and grant == "stop":
                    # The boolean grant, the explicit association, and the
                    # target's real wolt (looked up by the caller from the
                    # session registry just before) against the binding's
                    # current selectable set and default wolt.
                    target = fields.get("session") or record.get("session")
                    associated = (queue.routing.get(record["binding_id"]) or {}).get("sessions") or {}
                    current_wolts = set(binding.get("selectable_wolts") or ()) | {binding["default_wolt"]}
                    allowed = (
                        bool(binding.get("may_stop_own_sessions")) and target in associated
                        and target_wolt is not None and target_wolt in current_wolts
                    )
                if not allowed:
                    raise ConnectorError(409, "grant_withdrawn")
            # Fenced: a transition applies only from the state its caller saw,
            # and nothing moves an operation out of answered, nor out of
            # failed/uncertain (the owner's word) except to record the answer.
            if expect is not None and current not in expect:
                raise ConnectorError(409, "stale_operation")
            if current == "answered" and state != "answered":
                raise ConnectorError(409, "stale_operation")
            if current in ("failed", "uncertain") and state not in ("answered", current):
                raise ConnectorError(409, "stale_operation")
            record.update(fields)
            if state == "answered" and record["state"] in ("uncertain", "failed"):
                # The answer is kept; a parked or released operation stays what it is.
                self._save_queue(instance_id, queue)
                return dict(record)
            record["state"] = state
            if state == "failed":
                record["at"] = 0.0      # released: no longer counted for the hour
            if state == "associated" and record.get("session"):
                self._associate(queue, record["binding_id"], record["session"], "created", record.get("wolt") or "")
                routing = dict(queue.routing.get(record["binding_id"]) or {})
                routing.update({"active_wolt": record.get("wolt"), "active_session": record["session"]})
                queue.routing[record["binding_id"]] = routing
            self._save_queue(instance_id, queue)
            return dict(record)

    def uncertain_operations(self, instance_id: str) -> list[dict]:
        """Owner view: commands whose effect may or may not have happened."""
        with self._lock():
            return [
                {"key": key, "event_id": key.split("\x00", 1)[-1], **record}
                for key, record in self._queue(instance_id).operations.items()
                if record["state"] == "uncertain"
            ]

    def resolve_operation(
        self, instance_id: str, event_id: str, *, session: str | None = None, abandon: bool = False,
    ) -> bool:
        """Owner recovery for a parked operation: done (a `/new` names its session), or abandoned.

        The owner says what happened; nothing is rerun. A `/new` marked done
        must name the session it made (it gets its association); a `/kill`
        marked done names none. Abandoned, a `/new` releases its slot and
        hourly charge. Done and abandoned together, or a `/new` done without
        a session, is refused, never read as the other. Takes the binding's
        command lock first, like a command, so it cannot interleave with a
        resumed attempt.
        """
        if abandon and session is not None:
            raise ConnectorError(400, "done_or_abandon")
        key = self.handoff_key(instance_id, event_id)
        first = self.operation(instance_id, key)
        if first is None:
            return False
        with self.command_lock(first["binding_id"]):
            with self._lock():
                queue = self._queue(instance_id)
                record = queue.operations.get(key)
                if record is None or record["state"] != "uncertain":
                    return False
                if abandon:
                    record["state"] = "failed"
                    record["at"] = 0.0
                    record["result"] = {"routed": "command", "command": record["command"], "outcome": "abandoned"}
                elif record["command"] == "new":
                    if session is None:
                        raise ConnectorError(400, "session_required")
                    if not valid_id(session, 128):
                        raise ConnectorError(400, "invalid_session")
                    record["session"] = session
                    record["state"] = "answered"
                    record["result"] = {"routed": "command", "command": "new", "outcome": "resolved", "session": session}
                    self._associate(queue, record["binding_id"], session, "created", record.get("wolt") or "")
                else:
                    if session is not None and session != record.get("session"):
                        raise ConnectorError(400, "session_mismatch")
                    record["state"] = "answered"
                    record["result"] = {"routed": "command", "command": record["command"], "outcome": "resolved",
                                        "session": record.get("session")}
                self._save_queue(instance_id, queue)
                return True

    def claim_transcription(
        self, instance_id: str, key: str, upload_id: str, size: int, *, key_present: bool = True,
    ) -> dict:
        """Claim one attachment's transcription, or say why not; one locked write.

        Returns `{"claim": id}` to go ahead, or `{"done": {"text"|"reason"}}`
        for a disposition already settled (cached, reused by every retry),
        or one settled now: the binding's current `may_transcribe`, the
        per-attachment attempt
        cap (across restarts and owner retries), and the instance's UTC-day
        byte and request budget, charged here and kept whatever happens
        next (the provider may have billed). A live claim held by another
        attempt answers `{"busy": True}`; a stale one is reclaimed within the
        cap. A disabled instance, or one whose channel the connector does not
        own now, answers `{"paused": True}` and settles nothing. Results are cached; a crash after the call and before its
        settlement can call again, at most `transcribe_max_attempts` times.
        """
        tkey = f"{key}\x00{upload_id}"
        with self._lock():
            registry = self._registry()
            queue = self._queue(instance_id)
            inbound = queue.inbound.get(key)
            if (
                inbound is None or inbound["state"] != "accepted"
                or upload_id not in [a.get("upload_id") for a in inbound["event"].get("attachments", ())]
            ):
                # The event is over (cancelled, delivered, pruned) or does not
                # own this file: no call, and nothing for a session either.
                return {"stopped": True}
            record = queue.transcriptions.get(tkey)
            if record is not None and record["state"] == "done":
                return {"done": dict(record["disposition"])}
            now = self.clock()

            def settle(reason: str) -> dict:
                entry = queue.transcriptions.setdefault(tkey, {"attempts": 0})
                entry.update({"state": "done", "disposition": {"reason": reason}, "claim": None})
                self._save_queue(instance_id, queue)
                return {"done": {"reason": reason}}

            if record is not None and record["state"] == "claimed" and now - record.get("claimed_at", 0) < self.limits.transcribe_claim_seconds:
                return {"busy": True}
            binding_id = ((inbound.get("event") or {}).get("binding") or {}).get("binding_id")
            binding = next((b for b in registry["bindings"] if b["binding_id"] == binding_id), None)
            instance = registry["instances"].get(instance_id)
            if binding is None or instance is None:
                return settle("not_allowed")
            if not instance.get("enabled") or not self.runtime_active(instance.get("channel", "")):
                # A pause, not a refusal: no claim, no charge, nothing settled;
                # the event waits and is transcribed once the instance is back.
                return {"paused": True}
            if not binding.get("may_transcribe"):
                return settle("not_enabled")
            if not key_present:
                return settle("no_key")         # before any charge: no call will be made
            if not isinstance(size, int) or size <= 0:
                return settle("unreadable")
            if size > self.limits.transcribe_max_bytes:
                return settle("too_large")
            attempts = (record or {}).get("attempts", 0)
            if attempts >= self.limits.transcribe_max_attempts:
                return settle("retries_exhausted")
            day = time.strftime("%Y-%m-%d", time.gmtime(now))
            budget = queue.transcribe_budget
            if budget.get("day") != day:
                budget.clear()
                budget.update({"day": day, "bytes": 0, "requests": 0})
            if (budget["requests"] + 1 > self.limits.transcribe_requests_per_day
                    or budget["bytes"] + size > self.limits.transcribe_bytes_per_day):
                return settle("over_budget")
            budget["requests"] += 1
            budget["bytes"] += size
            claim = secrets.token_hex(8)
            queue.transcriptions[tkey] = {
                "state": "claimed", "claim": claim, "claimed_at": now, "attempts": attempts + 1,
            }
            self._save_queue(instance_id, queue)
            return {"claim": claim}

    def dispatch_allowed(self, instance_id: str, key: str, binding_id: str) -> tuple[str, dict | None]:
        """May this event be delivered now: `("ok", binding)`, `("later", None)` or `("gone", None)`.

        Gone: the event is no longer waiting for its receipt (cancelled,
        delivered, pruned) or its binding was removed. Later: the instance is
        disabled or its runtime is not running; the event stays queued and
        is tried again, never dropped.
        """
        with self._lock():
            registry = self._registry()
            inbound = self._queue(instance_id).inbound.get(key)
            binding = next((b for b in registry["bindings"] if b["binding_id"] == binding_id), None)
            instance = registry["instances"].get(instance_id)
            if inbound is None or inbound["state"] != "accepted" or binding is None or instance is None:
                return "gone", None
            if not instance.get("enabled") or not self.runtime_active(instance.get("channel", "")):
                return "later", None
            return "ok", self._effective(binding)

    def settle_transcription(self, instance_id: str, key: str, upload_id: str, claim: str, disposition: dict) -> dict:
        """Persist the terminal disposition of a claim: `{"text"}` or `{"reason"}`.

        Answers `{"done": disposition}` (saved now, or already saved),
        `{"pending": True}` when a replacement claim owns the record, or
        `{"stopped": True}` when the event is over or pruned.
        """
        tkey = f"{key}\x00{upload_id}"
        with self._lock():
            queue = self._queue(instance_id)
            record = queue.transcriptions.get(tkey)
            inbound = queue.inbound.get(key)
            if record is None or inbound is None or inbound["state"] != "accepted":
                # Pruned, or the event is over: never recreated, no body
                # restored, nothing for a session.
                return {"stopped": True}
            if record["state"] == "done":
                return {"done": dict(record["disposition"])}
            if record.get("claim") != claim:
                # A replacement attempt owns it now: its result is the one.
                return {"pending": True}
            record.update({"state": "done", "disposition": dict(disposition), "claim": None})
            self._save_queue(instance_id, queue)
            return {"done": dict(disposition)}

    def record_command(self, instance_id: str, record: dict) -> None:
        """Audit: one line per command executed or refused, never the token or a body."""
        self._append_line("commands.jsonl", {"at": self.clock(), "instance_id": instance_id, **record})

    def _release_unclaimed(self, instance_id: str, queue: _Queue, upload_ids: list[str]) -> None:
        """A rejected event gives back the handles it named and nobody else holds."""
        for upload_id in upload_ids:
            handle = queue.uploads.get(upload_id)
            if handle is not None and handle.get("state") == "stored" and handle.get("claimed_by") is None:
                del queue.uploads[upload_id]
                (self._upload_dir(instance_id) / upload_id).unlink(missing_ok=True)

    def release_uploads(self, instance_id: str, upload_ids: list[str]) -> None:
        """The router has moved or consumed these claimed uploads: free their bytes."""
        with self._lock():
            queue = self._queue(instance_id)
            for upload_id in upload_ids:
                if queue.uploads.pop(upload_id, None) is not None:
                    (self._upload_dir(instance_id) / upload_id).unlink(missing_ok=True)
            self._save_queue(instance_id, queue)

    def record_inbound(self, event: AcceptedEvent) -> dict:
        """The default router: a durable record, until sessions take events."""
        self._append_line("inbound.jsonl", {
            "at": self.clock(), "instance_id": event.instance_id, "channel": event.channel,
            "account": event.account, "event_id": event.event_id,
            "wolt": event.binding["default_wolt"], "may_wake": event.binding["may_wake"],
            "session": event.reply_to_session, "sender": event.sender,
            "conversation": event.conversation, "text": event.text,
            "attachments": list(event.attachments), "cursor": event.cursor,
        })
        return {"routed": "recorded"}

    # --------------------------------------------------------------- uploads

    @staticmethod
    def _held_bytes(queue: _Queue, *, except_id: str | None = None) -> int:
        return sum(h["size"] for u, h in queue.uploads.items() if u != except_id)

    @staticmethod
    def _hourly_bytes(queue: _Queue, *, except_id: str | None = None) -> int:
        return sum(entry[1] for entry in queue.counters["uploads"] if entry[2] != except_id)

    def begin_upload(self, who: Principal) -> tuple[str, Path]:
        """Open an upload: the slot and the disk cap are reserved before a byte lands.

        The reservation is a handle in the queue file, so two uploads arriving
        together see each other, and one that outlives its process is
        reclaimed by the sweep. Quota is the instance's, whoever the eventual
        sender turns out to be: an unbound sender's bytes count exactly like
        a bound one's, so the event's sender check (which comes later) can
        never be used to fill the disk for free.
        """
        with self._transaction() as tx:
            registry = self._registry()
            who = self._live(registry, who)
            queue = self._load(tx, who.instance_id)
            self._sweep(who.instance_id, queue, registry, tx)
            if len(queue.counters["uploads"]) >= self.limits.uploads_per_hour:
                self._commit(tx, who.instance_id)
                raise ConnectorError(429, "uploads_rate_limited")
            if self._hourly_bytes(queue) >= self.limits.upload_bytes_per_hour:
                self._commit(tx, who.instance_id)
                raise ConnectorError(429, "upload_bytes_rate_limited")
            if self._held_bytes(queue) >= self.limits.upload_disk_cap_bytes:
                self._commit(tx, who.instance_id)
                raise ConnectorError(429, "upload_quota")
            upload_id = secrets.token_hex(16)
            now = self.clock()
            queue.uploads[upload_id] = {
                "state": "in_flight", "size": 0, "pid": os.getpid(), "started": now,
            }
            queue.counters["uploads"].append([now, 0, upload_id])
            self._upload_dir(who.instance_id).mkdir(mode=0o700, parents=True, exist_ok=True)
            self._commit(tx, who.instance_id)
            return upload_id, self._part_path(who.instance_id, upload_id)

    def account_upload_bytes(self, who: Principal, upload_id: str, received: int) -> None:
        """Count bytes as they arrive; raises once the instance is over a cap.

        Checked against what every other handle holds right now, under the
        lock, so overlapping uploads cannot each fit under the cap alone.
        """
        if received > self.limits.upload_max_bytes:
            raise ConnectorError(413, "upload_too_large")
        with self._lock():
            registry = self._registry()
            who = self._live(registry, who)
            queue = self._queue(who.instance_id)
            handle = queue.uploads.get(upload_id)
            if handle is None or handle.get("state") != "in_flight":
                raise ConnectorError(409, "upload_reclaimed", "the upload was reclaimed")
            if self._held_bytes(queue, except_id=upload_id) + received > self.limits.upload_disk_cap_bytes:
                raise ConnectorError(429, "upload_quota")
            if self._hourly_bytes(queue, except_id=upload_id) + received > self.limits.upload_bytes_per_hour:
                raise ConnectorError(429, "upload_bytes_rate_limited")
            handle["size"] = received
            for entry in queue.counters["uploads"]:
                if entry[2] == upload_id:
                    entry[1] = received
            self._save_queue(who.instance_id, queue)

    def abort_upload(self, who: Principal, upload_id: str, part: Path) -> None:
        """Drop an upload that did not finish; its bytes still count against the hour."""
        part.unlink(missing_ok=True)
        try:
            with self._lock():
                queue = self._queue(who.instance_id)
                if queue.uploads.get(upload_id, {}).get("state") == "in_flight":
                    del queue.uploads[upload_id]
                    self._save_queue(who.instance_id, queue)
        except (ConnectorStateUnreadable, OSError, RuntimeError):
            pass

    def commit_upload(self, who: Principal, upload_id: str, part: Path, name: object) -> dict:
        try:
            with self._lock():
                registry = self._registry()
                who = self._live(registry, who)
                queue = self._queue(who.instance_id)
                handle = queue.uploads.get(upload_id)
                if handle is None or handle.get("state") != "in_flight":
                    raise ConnectorError(409, "upload_reclaimed", "the upload was reclaimed")
                size = part.stat().st_size
                if size == 0:
                    raise ConnectorError(400, "upload_empty")
                now = self.clock()
                queue.uploads[upload_id] = {
                    "state": "stored", "name": outbox.display_name(name), "size": size,
                    "created": now, "expires": now + self.limits.upload_ttl_seconds, "claimed_by": None,
                }
                for entry in queue.counters["uploads"]:
                    if entry[2] == upload_id:
                        entry[1] = size
                os.replace(part, self._upload_dir(who.instance_id) / upload_id)
                self._save_queue(who.instance_id, queue)
                return {"upload_id": upload_id, "size": size, "expires": queue.uploads[upload_id]["expires"]}
        except BaseException:
            self.abort_upload(who, upload_id, part)
            raise

    # -------------------------------------------------------------- outbound

    def enqueue(
        self, *, channel: str, account: str | None, conversation: str, text: str,
        thread: str | None = None, binding_id: str | None = None,
        attachment: Path | None = None, attachment_name: str = "",
        attachment_kind: str = "file", reply_footer: str = "", session: str | None = None,
        buttons: list[list[dict]] | None = None,
    ) -> str:
        """Queue one message for the enabled connector bound to that conversation.

        This is the seam notify will call. The destination is a binding of
        the one enabled instance for the channel and account: `binding_id`
        when the caller answers a particular event, else the single binding
        that covers the conversation (two senders bound in one conversation
        make it ambiguous, and ambiguity is refused). The attachment is
        copied into core's own files, so the connector only ever reads a
        lodge-owned path. `buttons` are rows of `{"text", "action"}`: core
        issues an opaque, binding-scoped, expiring callback id for each and
        the connector renders them as inline buttons.
        """
        if not isinstance(text, str) or len(text.encode("utf-8")) > self.limits.outbound_text_max_bytes:
            raise ConnectorError(413, "text_too_long")
        rows = buttons or []
        if sum(len(row) for row in rows) > self.limits.max_buttons:
            raise ConnectorError(400, "too_many_buttons")
        if attachment is not None and attachment.stat().st_size > self.limits.outbound_file_max_bytes:
            raise ConnectorError(413, "attachment_too_large")
        with self._transaction() as tx:
            registry = self._registry()
            # `account=None`: the one enabled instance of the channel (notify
            # knows the chat, not the bot).
            instance_id = next(
                (i for i, r in registry["instances"].items()
                 if r["channel"] == channel and r["enabled"] and account in (None, r["account"])),
                None,
            )
            if instance_id is None:
                raise NoConnectorBinding(f"no enabled {channel} connector for account {account or 'any'}")
            account = registry["instances"][instance_id]["account"]
            candidates = [
                b for b in registry["bindings"]
                if b["instance_id"] == instance_id and b["conversation"] == conversation
                and (b.get("thread") is None or b["thread"] == thread)
            ]
            if binding_id is not None:
                candidates = [b for b in candidates if b["binding_id"] == binding_id]
            if len(candidates) != 1:
                raise NoConnectorBinding(
                    f"{channel} {conversation} has {len(candidates)} bindings on {instance_id}; need exactly one",
                )
            binding = candidates[0]
            queue = self._load(tx, instance_id)
            self._sweep(instance_id, queue, registry, tx)
            if sum(1 for d in queue.deliveries if d["status"] not in _TERMINAL) >= self.limits.max_queued:
                self._commit(tx, instance_id)
                raise ConnectorError(429, "queue_full")
            now = self.clock()
            delivery_id = secrets.token_hex(16)
            rendered = []
            if rows:
                issued = sum(1 for c in queue.callbacks.values() if c["binding_id"] == binding["binding_id"] and c["expires"] > now)
                for row in rows:
                    line = []
                    for button in row:
                        if issued >= self.limits.callbacks_per_binding:
                            raise ConnectorError(429, "callbacks_exhausted")
                        callback_id = CALLBACK_PREFIX + secrets.token_hex(16)
                        queue.callbacks[callback_id] = {
                            "binding_id": binding["binding_id"], "action": dict(button["action"]),
                            "expires": now + self.limits.callback_ttl_seconds, "consumed_by": None,
                        }
                        issued += 1
                        line.append({"text": str(button["text"])[:64], "callback_id": callback_id})
                    rendered.append(line)
            attachments = []
            if attachment is not None:
                file_id = secrets.token_hex(16)
                target = self._file_path(file_id)
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                tmp = target.with_name(f".{file_id}.tmp")
                try:
                    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, "wb") as sink, open(attachment, "rb") as source:
                        shutil.copyfileobj(source, sink)
                    os.replace(tmp, target)
                except BaseException:
                    tmp.unlink(missing_ok=True)
                    raise
                attachments.append({
                    "file_id": file_id, "name": outbox.display_name(attachment_name or attachment.name),
                    "kind": attachment_kind if attachment_kind in ("file", "voice") else "file",
                    "size": target.stat().st_size,
                    "expires": now + self.limits.file_retention_seconds,
                })
            queue.deliveries.append({
                "delivery_id": delivery_id, "binding_id": binding["binding_id"],
                "account": account, "conversation": conversation,
                "thread": thread, "text": text, "attachments": attachments,
                "reply_footer": reply_footer, "session": session, "created": now,
                "buttons": rendered,
                "status": "queued", "failed_attempts": 0, "lease": None, "acks": {},
                "not_before": now, "clock_started": None, "paused_base": 0.0,
            })
            self._commit(tx, instance_id)
            return delivery_id

    def list_deliveries(self, instance_id: str) -> list[dict]:
        """Owner view of a queue, with the registry and the clocks applied first. Claims nothing."""
        with self._transaction(dispatch=False) as tx:
            registry = self._registry()
            self._instance(registry, instance_id)
            queue = self._load(tx, instance_id)
            self._sweep(instance_id, queue, registry, tx)
            self._commit(tx, instance_id)
            return [dict(d) for d in queue.deliveries]

    def lease(self, who: Principal, max_items: int) -> dict:
        """Hand out up to `max_items` queued deliveries, each leased for 60 s."""
        if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 1:
            raise ConnectorError(400, "malformed_lease", "max must be a positive integer")
        max_items = min(max_items, self.limits.lease_max_batch)
        with self._transaction() as tx:
            registry = self._registry()
            who = self._live(registry, who)
            instance = registry["instances"][who.instance_id]
            queue = self._load(tx, who.instance_id)
            self._sweep(who.instance_id, queue, registry, tx)
            now = self.clock()
            leased = []
            active = who.enabled and self.runtime_active(who.channel)
            if active:
                in_flight = sum(1 for d in queue.deliveries if d["status"] == "leased")
                if in_flight >= self.limits.max_in_flight:
                    self._commit(tx, who.instance_id)
                    raise ConnectorError(429, "deliveries_in_flight")
                for delivery in queue.deliveries:
                    if len(leased) >= max_items or in_flight + len(leased) >= self.limits.max_in_flight:
                        break
                    if delivery["status"] != "queued" or delivery["not_before"] > now:
                        continue
                    attempt_id = secrets.token_hex(16)
                    delivery["lease"] = {
                        "attempt_id": attempt_id, "generation": who.generation,
                        "expires": now + self.limits.lease_seconds,
                    }
                    delivery["status"] = "leased"
                    if delivery.get("clock_started") is None:
                        delivery["clock_started"] = now
                        delivery["paused_base"] = instance.get("paused_seconds", 0.0)
                    leased.append({
                        "delivery_id": delivery["delivery_id"], "attempt_id": attempt_id,
                        "lease_expires": delivery["lease"]["expires"],
                        "conversation": {"provider_id": delivery["conversation"], "thread": delivery.get("thread")},
                        "text": delivery["text"],
                        "attachments": [
                            {k: a[k] for k in ("file_id", "name", "kind", "size")}
                            for a in delivery["attachments"]
                        ],
                        "reply_footer": delivery.get("reply_footer", ""),
                        "buttons": delivery.get("buttons", []),
                    })
            self._commit(tx, who.instance_id)
            return {"deliveries": leased, "paused": not active}

    def ack(self, who: Principal, delivery_id: object, body: object) -> dict:
        """Close an attempt. A repeat for the same attempt returns the first answer."""
        if not isinstance(body, dict) or not set(body) <= _ACK_KEYS:
            raise ConnectorError(400, "malformed_ack")
        attempt_id = body.get("attempt_id")
        if not isinstance(attempt_id, str) or outbox.ENTRY_ID_RE.fullmatch(attempt_id) is None:
            raise ConnectorError(400, "malformed_ack", "attempt_id is required")
        if not isinstance(body.get("ok"), bool):
            raise ConnectorError(400, "malformed_ack", "ok must be true or false")
        provider_message_id = body.get("provider_message_id")
        if provider_message_id is not None and _provider_id(provider_message_id, self.limits.id_max_chars) is None:
            raise ConnectorError(400, "malformed_ack", "provider_message_id is not a provider id")
        parts = body.get("provider_message_ids")
        if parts is None:
            parts = [] if provider_message_id is None else [provider_message_id]
        if (
            not isinstance(parts, list) or len(parts) > self.limits.ack_max_parts
            or any(_provider_id(part, self.limits.id_max_chars) is None for part in parts)
        ):
            raise ConnectorError(400, "malformed_ack", "provider_message_ids must be a short list of provider ids")
        if provider_message_id is not None and provider_message_id not in parts:
            parts = [provider_message_id, *parts]
        error = body.get("error", "")
        if not isinstance(error, str):
            raise ConnectorError(400, "malformed_ack", "error must be a string")
        retry = body.get("retry", True)
        if not isinstance(retry, bool):
            raise ConnectorError(400, "malformed_ack", "retry must be true or false")
        with self._transaction() as tx:
            registry = self._registry()
            who = self._live(registry, who)
            queue = self._load(tx, who.instance_id)
            delivery = next(
                (d for d in queue.deliveries if d["delivery_id"] == delivery_id), None,
            )
            if delivery is not None and attempt_id in delivery.get("acks", {}):
                return dict(delivery["acks"][attempt_id])
            self._sweep(who.instance_id, queue, registry, tx)
            lease = delivery.get("lease") if delivery is not None else None
            if (
                delivery is None or lease is None or lease["attempt_id"] != attempt_id
                or lease["generation"] != who.generation
            ):
                # Foreign, stale, expired or cancelled: one answer for all, so
                # a connector learns nothing about queues that are not its own.
                self._commit(tx, who.instance_id)
                raise ConnectorError(409, "stale_attempt", "no active lease for that attempt")
            # Every part the provider accepted, this attempt or an earlier
            # one, sent or not the whole message: a reply to any of them
            # finds the delivery, inside its conversation.
            known = [p for p in delivery.get("provider_message_ids", [])]
            known.extend(p for p in parts if p not in known)
            delivery["provider_message_ids"] = known
            for part in parts:
                queue.provider_messages[f"{who.account}\x00{delivery['conversation']}\x00{part}"] = {
                    "delivery_id": delivery["delivery_id"], "binding_id": delivery.get("binding_id"),
                    "session": delivery.get("session"), "at": self.clock(), "quote": _quote_of(delivery),
                }
            if body["ok"]:
                delivery.update({
                    "status": "delivered", "lease": None, "finished": self.clock(),
                    "provider_message_id": known[0] if known else None,
                })
                self._drop_files(delivery)
            elif retry:
                self._attempt_failed(
                    who.instance_id, delivery, error or "connector reported a failure", tx, backoff=True,
                )
            else:
                self._finish(who.instance_id, delivery, "failed", error or "connector refused for good", tx)
            result = {
                "ok": True, "status": delivery["status"], "failed_attempts": delivery["failed_attempts"],
            }
            if delivery["status"] == "queued":
                result["retry_at"] = delivery["not_before"]
            delivery.setdefault("acks", {})[attempt_id] = result
            self._commit(tx, who.instance_id)
            return dict(result)

    def open_file(self, who: Principal, file_id: object, attempt_id: object) -> tuple[Path, dict]:
        """The path of an attachment this attempt may read, and its metadata.

        Live token of the current generation, active lease for exactly that
        attempt, enabled instance, binding still granted: anything less is
        refused the same way, and the file itself is never named by anything
        the connector chose.
        """
        if (
            not isinstance(file_id, str) or outbox.ENTRY_ID_RE.fullmatch(file_id) is None
            or not isinstance(attempt_id, str) or outbox.ENTRY_ID_RE.fullmatch(attempt_id) is None
        ):
            raise ConnectorError(404, "file_not_available")
        with self._transaction() as tx:
            registry = self._registry()
            who = self._live(registry, who)
            queue = self._load(tx, who.instance_id)
            self._sweep(who.instance_id, queue, registry, tx)
            self._commit(tx, who.instance_id)
            for delivery in queue.deliveries:
                lease = delivery.get("lease")
                if (
                    delivery["status"] != "leased" or lease is None
                    or lease["attempt_id"] != attempt_id or lease["generation"] != who.generation
                ):
                    continue
                for attachment in delivery["attachments"]:
                    if attachment["file_id"] == file_id and who.enabled:
                        return self._file_path(file_id), dict(attachment)
        raise ConnectorError(404, "file_not_available")

    def stream_allowed(self, who: Principal, file_id: str, attempt_id: str) -> bool:
        """Whether an open file stream may send its next chunk: same checks as `open_file`."""
        try:
            self.open_file(who, file_id, attempt_id)
        except (ConnectorError, ConnectorStateUnreadable):
            return False
        return True

    # -------------------------------------------------------------- failures

    def on_failure(self, listener: Callable[[str, dict], None]) -> None:
        """Hear about every delivery that ends without a provider message.

        The seam that will tell the session. Listeners hear about committed
        transitions only, run after the lock is released (so they may enqueue
        through this store), and at most once: a crash between the write and
        the dispatch loses that notification. The default writes a record.
        """
        self._failure_listeners.append(listener)

    def record_failure(self, instance_id: str, delivery: dict) -> None:
        self._append_line("failures.jsonl", {
            "at": self.clock(), "instance_id": instance_id,
            "delivery_id": delivery["delivery_id"], "status": delivery["status"],
            "failed_attempts": delivery.get("failed_attempts", 0), "error": delivery.get("error", ""),
            "session": delivery.get("session"), "conversation": delivery["conversation"],
            "delivered": False,
        })
