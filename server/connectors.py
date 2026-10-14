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

Dedup-before-router is a fixture-slice arrangement: the event is recorded as
accepted before the router has it, so a crash in between loses or repeats it
at the session. That window must close before a real provider (Telegram)
advances its durable accept cursor on this store's answer.

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


@dataclass
class _Queue:
    deliveries: list = field(default_factory=list)
    dedup: dict = field(default_factory=dict)
    uploads: dict = field(default_factory=dict)
    counters: dict = field(default_factory=lambda: {"events": [], "uploads": []})

    def to_json(self) -> dict:
        return {
            "version": 1, "deliveries": self.deliveries, "dedup": self.dedup,
            "uploads": self.uploads, "counters": self.counters,
        }

    @classmethod
    def from_json(cls, data: dict) -> "_Queue":
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ConnectorStateUnreadable("queue file has an unknown shape")
        queue = cls(
            data.get("deliveries", []), data.get("dedup", {}),
            data.get("uploads", {}), data.get("counters", {"events": [], "uploads": []}),
        )
        if not (isinstance(queue.deliveries, list) and isinstance(queue.dedup, dict)
                and isinstance(queue.uploads, dict) and isinstance(queue.counters, dict)):
            raise ConnectorStateUnreadable("queue file has an unknown shape")
        queue.counters.setdefault("events", [])
        queue.counters.setdefault("uploads", [])
        return queue


_TERMINAL = frozenset({"delivered", "failed", "cancelled"})
_EVENT_KEYS = frozenset({"event_id", "sender", "conversation", "text", "attachments", "reply_to"})
_SENDER_KEYS = frozenset({"provider_id", "display"})
_CONVERSATION_KEYS = frozenset({"provider_id", "thread"})
_ACK_KEYS = frozenset({"attempt_id", "ok", "provider_message_id", "error", "retry"})


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

    def settle(self, instance_id: str) -> None:
        """The queue is on disk (or gone): its transitions are committed."""
        self.dirty.discard(instance_id)
        kept = []
        for entry in self.pending:
            (self.committed if entry[0] == instance_id else kept).append(entry)
        self.pending = kept


class ConnectorStore:
    """The lodge's side of the connector protocol, on disk."""

    def __init__(
        self, root: Path, *, clock: Callable[[], float] = time.time,
        limits: Limits | None = None,
    ):
        self.root = Path(root)
        self.clock = clock
        self.limits = limits or Limits()
        self.router: Callable[[AcceptedEvent], dict] = self.record_inbound
        self._failure_listeners: list[Callable[[str, dict], None]] = [self.record_failure]
        self._held = threading.local()

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
    def _transaction(self):
        """A locked operation whose callbacks run after the lock is released.

        Whatever the sweep changed is written on every way out that the
        protocol defines, a normal return and a refusal alike, so the disk
        never lags the clock. An unexpected exception writes nothing more.
        Listeners then hear about committed transitions only, at most once:
        a crash between the write and the dispatch loses that notification.
        """
        tx = _Tx()
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

    def add_instance(self, instance_id: str, channel: str, account: str) -> dict:
        """Register an instance, disabled: nothing routes to it until `enable`.

        Registration only: this slice installs no code.
        """
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
            self._write_json(self.registry_path, registry)
            return {"instance_id": instance_id, **registry["instances"][instance_id]}

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
        with self._transaction() as tx:
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
        with self._transaction() as tx:
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
    ) -> dict:
        """Allow one provider sender in one conversation; the owner's grant, typed.

        Binding the same sender, conversation and thread again changes the
        grant in place and keeps its id, so deliveries made under it stand.
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
        with self._lock():
            registry = self._registry()
            instance = self._instance(registry, instance_id)
            key = (instance_id, sender, conversation, thread)
            existing = next(
                (b for b in registry["bindings"]
                 if (b["instance_id"], b["sender"], b["conversation"], b.get("thread")) == key),
                None,
            )
            binding = {
                "binding_id": existing["binding_id"] if existing else secrets.token_hex(8),
                "instance_id": instance_id, "account": instance["account"],
                "sender": sender, "conversation": conversation, "thread": thread,
                "default_wolt": default_wolt, "may_wake": bool(may_wake),
            }
            registry["bindings"] = [b for b in registry["bindings"] if b is not existing]
            registry["bindings"].append(binding)
            self._write_json(self.registry_path, registry)
            return dict(binding)

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

    def issue_token(self, instance_id: str) -> str:
        """Mint the next generation's token; older generations are revoked.

        One registry write. Leases the old generation held are released when
        the queue is next read (its acks are refused anyway); the queue itself
        is untouched. Called when a runner is actually relaunched, never
        because the lodge restarted.
        """
        with self._lock():
            registry = self._registry()
            instance = self._instance(registry, instance_id)
            instance["generation"] += 1
            now = self.clock()
            for token in registry["tokens"]:
                if token["instance_id"] == instance_id and token.get("revoked") is None:
                    token["revoked"] = now
            plain = TOKEN_PREFIX + secrets.token_urlsafe(32)
            registry["tokens"].append({
                "digest": _digest(plain), "instance_id": instance_id,
                "generation": instance["generation"], "issued": now, "revoked": None,
            })
            registry["tokens"] = [
                t for t in registry["tokens"]
                if t.get("revoked") is None or now - t["revoked"] < 86400
            ]
            self._write_json(self.registry_path, registry)
            return plain

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
        the caller must never fall back to the token-less owner path.
        """
        if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX) or len(token) > 128:
            return None
        return self._principal(self._registry(), _digest(token))

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
        for upload_id, handle in list(queue.uploads.items()):
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

    # --------------------------------------------------------------- inbound

    def _parse_event(self, payload: object) -> dict:
        """Bound and normalize an inbound event, or say what is wrong with it.

        The schema is closed: a key the protocol does not name is a fault.
        """
        limits = self.limits
        if not isinstance(payload, dict):
            raise ConnectorError(400, "malformed_event", "event must be an object")
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
        reply_delivery = None
        if reply_to is not None:
            if not isinstance(reply_to, dict) or set(reply_to) != {"delivery_id"}:
                raise ConnectorError(400, "malformed_event", "reply_to needs exactly a delivery_id")
            reply_delivery = reply_to["delivery_id"]
            if not isinstance(reply_delivery, str) or outbox.ENTRY_ID_RE.fullmatch(reply_delivery) is None:
                raise ConnectorError(400, "malformed_event", "reply_to needs a delivery_id")
        return {
            "event_id": event_id, "sender": sender_id, "display": display,
            "conversation": conversation_id, "thread": thread, "text": text,
            "attachments": upload_ids, "reply_to": reply_delivery,
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
        # The digest leaves out the display name: it is shown, never routed on.
        digest = _canonical({k: v for k, v in event.items() if k != "display"})
        key = f"{who.account}\x00{event['event_id']}"
        with self._transaction() as tx:
            registry = self._registry()
            who = self._live(registry, who)
            queue = self._load(tx, who.instance_id)
            self._sweep(who.instance_id, queue, registry, tx)
            now = self.clock()
            record = queue.dedup.get(key)
            if record is not None:
                # Same answer first, before any question about its uploads.
                if record["digest"] != digest:
                    self._release_unclaimed(who.instance_id, queue, event["attachments"])
                    self._commit(tx, who.instance_id)
                    raise ConnectorError(409, "event_conflict", "event_id seen with another payload")
                return record["status"], dict(record["body"])
            if len(queue.counters["events"]) >= self.limits.events_per_minute:
                self._commit(tx, who.instance_id)
                raise ConnectorError(429, "events_rate_limited")
            queue.counters["events"].append(now)

            def drop(reason: str) -> tuple[int, dict]:
                body = {"accepted": False, "reason": reason, "event_id": event["event_id"]}
                self._release_unclaimed(who.instance_id, queue, event["attachments"])
                queue.dedup[key] = {"digest": digest, "status": 200, "body": body, "at": now}
                self._commit(tx, who.instance_id)
                return 200, body

            if not who.enabled:
                return drop("instance_disabled")
            binding, reason = self._binding_for(
                registry, who.instance_id, event["sender"], event["conversation"], event["thread"],
            )
            if binding is None:
                return drop(reason)
            reply_session = None
            if event["reply_to"] is not None:
                target = next(
                    (d for d in queue.deliveries if d["delivery_id"] == event["reply_to"]), None,
                )
                # The exact binding, not a conversation that looks the same.
                if target is None or target.get("binding_id") != binding["binding_id"]:
                    return drop("reply_to_out_of_binding")
                reply_session = target.get("session")
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
            queue.dedup[key] = {"digest": digest, "status": 200, "body": body, "at": now}
            self._commit(tx, who.instance_id)
            accepted = AcceptedEvent(
                who.instance_id, who.channel, who.account, event["event_id"], dict(binding),
                {"provider_id": event["sender"], "display": outbox.display_name(event["display"]) if event["display"] else ""},
                {"provider_id": event["conversation"], "thread": event["thread"]},
                event["text"],
                tuple(
                    {
                        "upload_id": upload_id, "path": str(self._upload_dir(who.instance_id) / upload_id),
                        "name": handle["name"], "size": handle["size"],
                    } for upload_id, handle in claimed
                ),
                reply_session,
            )
        # Outside the lock, after the claim is durable: the router may use the store.
        try:
            routed = self.router(accepted) or {}
        except Exception as exc:
            routed = {"routed": "error", "error": str(exc)[: self.limits.error_max_chars]}
        with self._lock():
            queue = self._queue(who.instance_id)
            record = queue.dedup.get(key)
            if record is not None and record["digest"] == digest:
                record["body"] = {**record["body"], **routed}
                self._save_queue(who.instance_id, queue)
        return 200, {**body, **routed}

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
            "attachments": list(event.attachments),
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
        self, *, channel: str, account: str, conversation: str, text: str,
        thread: str | None = None, binding_id: str | None = None,
        attachment: Path | None = None, attachment_name: str = "",
        attachment_kind: str = "file", reply_footer: str = "", session: str | None = None,
    ) -> str:
        """Queue one message for the enabled connector bound to that conversation.

        This is the seam notify will call. The destination is a binding of
        the one enabled instance for the channel and account: `binding_id`
        when the caller answers a particular event, else the single binding
        that covers the conversation (two senders bound in one conversation
        make it ambiguous, and ambiguity is refused). The attachment is
        copied into core's own files, so the connector only ever reads a
        lodge-owned path.
        """
        if not isinstance(text, str) or len(text.encode("utf-8")) > self.limits.outbound_text_max_bytes:
            raise ConnectorError(413, "text_too_long")
        if attachment is not None and attachment.stat().st_size > self.limits.outbound_file_max_bytes:
            raise ConnectorError(413, "attachment_too_large")
        with self._transaction() as tx:
            registry = self._registry()
            instance_id = next(
                (i for i, r in registry["instances"].items()
                 if r["channel"] == channel and r["account"] == account and r["enabled"]),
                None,
            )
            if instance_id is None:
                raise NoConnectorBinding(f"no enabled {channel} connector for account {account}")
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
                "status": "queued", "failed_attempts": 0, "lease": None, "acks": {},
                "not_before": now, "clock_started": None, "paused_base": 0.0,
            })
            self._commit(tx, instance_id)
            return delivery_id

    def list_deliveries(self, instance_id: str) -> list[dict]:
        """Owner view of a queue, with the registry and the clocks applied first."""
        with self._transaction() as tx:
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
            if who.enabled:
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
                    })
            self._commit(tx, who.instance_id)
            return {"deliveries": leased, "paused": not who.enabled}

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
            if body["ok"]:
                delivery.update({
                    "status": "delivered", "lease": None, "finished": self.clock(),
                    "provider_message_id": provider_message_id,
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
