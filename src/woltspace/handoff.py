"""The connector token handoff: minted on a real relaunch, delivered by file, never by argv.

The supervisor calls `prepare()` right before it spawns the runner and
nowhere else, so a token is rotated exactly when a process is launched: the
previous generation is revoked by the mint. The mint is one locked store
operation that hands back the token with its generation, issue time and
the lodge id; nothing is re-read afterwards, and from the moment it
returns the generation is tracked for cleanup. The generation is minted
*pending*: it authenticates nothing until the runner activates it, which is
how consumption is acknowledged to the lodge.

The file lives under an anchored runtime root: the lodge's platform state
directory, canonicalized once (a platform alias such as /var -> /private/var
is fine, that is the root's own name) and from then on traversed only with
directory-relative, no-follow operations: `run` is made and opened relative
to the root and refused if it is a link, checked to be ours and 0700, and
the file is created `O_EXCL|O_NOFOLLOW` relative to it. The child learns
the root, the file's path and, independently, the lodge id, instance id and
generation it must find inside (`WOLTSPACE_CONNECTOR_*`), never the token.

It fails closed, per generation. Everything after the mint runs under a
cleanup that revokes that generation. `abandon()` revokes first and
unconditionally, then removes the file; a failure of either is kept as a
debt (`unrevoked`, `uncleaned`), retried on every later call from any
state, and shown in `status()`. `check()` on every tick: a file still there
after `CONSUME_TIMEOUT` is removed and its generation revoked; a file gone
but whose generation was not activated within the window is a failure too,
never a success. A revocation that fails is also written down as a debt in
the ledger, `run/debts.json` (0600, anchored like everything else, every
read-modify-write under `run/debts.lock`): exact (instance, generation)
records. `ConnectorRecovery`, a lodge-wide step the supervisor runs at
every start and again on later ticks whatever the plans say (connector
disabled, channel off, runtime back on the adapter, another instance
selected), settles precisely those debts for every instance and revokes
pending generations never activated within the window; a newer live
generation is never touched, a connector that legitimately survived
included. The ledger is never read as empty when it is not: a corrupt,
unreadable, non-regular or oversized file is preserved as it is, reported
as a recovery failure (status, `woltspace connector list`, doctor) and
retried; a debt that could not be written down is marked non-durable in
the handoff's status rather than called durable.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
import time
from pathlib import Path
from typing import Callable

CONSUME_TIMEOUT = 10.0
# Older than this, a pending generation and its file are debris.
STALE_AFTER = 60.0
RUN_DIR = "run"
DEBTS_FILE = "debts.json"
DEBTS_LOCK = "debts.lock"
DEBTS_MAX_BYTES = 64 * 1024


def canonical_root(root: Path) -> str:
    """The runtime root's own name: resolved once, before anything is anchored to it."""
    return os.path.realpath(root)


def open_anchored_run_dir(root: str, *, create: bool) -> tuple[int, int]:
    """(root_fd, run_fd): the root as given, `run` reached below it without following links.

    The root must be a directory we own. `run` is made relative to it when
    asked (an existing link in its place makes the mkdir say it exists and
    the no-follow open refuse it), opened `O_DIRECTORY|O_NOFOLLOW`, and must
    be ours and 0700 (set with `fchmod` on the opened directory, never
    through a path). Raises OSError; the caller closes both descriptors.
    """
    if create:
        # The lodge's own state root; making it is as harmless as it is rare.
        os.makedirs(root, mode=0o700, exist_ok=True)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        info = os.fstat(root_fd)
        if info.st_uid != os.getuid():
            raise PermissionError(f"the runtime root {root} is not ours")
        if create:
            try:
                os.mkdir(RUN_DIR, 0o700, dir_fd=root_fd)
            except FileExistsError:
                pass
        run_fd = os.open(RUN_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        try:
            run_info = os.fstat(run_fd)
            if run_info.st_uid != os.getuid():
                raise PermissionError(f"{root}/{RUN_DIR} is not ours")
            if create and stat.S_IMODE(run_info.st_mode) != 0o700:
                os.fchmod(run_fd, 0o700)
            elif stat.S_IMODE(run_info.st_mode) & 0o077:
                raise PermissionError(f"{root}/{RUN_DIR} is not private")
        except BaseException:
            os.close(run_fd)
            raise
        return root_fd, run_fd
    except BaseException:
        os.close(root_fd)
        raise


class ConnectorHandoff:
    def __init__(
        self, store_factory: Callable[[], object], instance_id: str, root: Path, *,
        clock: Callable[[], float] = time.time,
    ):
        self._store_factory = store_factory
        self.instance_id = instance_id
        # Canonicalized here, once; every later step is relative to it.
        self.root = canonical_root(root)
        self._clock = clock
        self.pending: str | None = None        # the file's name inside run/
        self.issued_at: float = 0.0
        self.generation: int | None = None
        # Debts: generations whose revocation failed, files whose removal
        # failed, each with the last error. Retried on every call.
        self.unrevoked: dict[int, str] = {}
        self.uncleaned: dict[str, str] = {}
        # Debts that are owed in memory but could not be written to the ledger.
        self.non_durable: dict[int, str] = {}
        self.activated = False

    def _store(self):
        return self._store_factory()

    @property
    def run_dir(self) -> Path:
        return Path(self.root) / RUN_DIR

    # ------------------------------------------------------------- lifecycle

    def prepare(self) -> dict[str, str]:
        """Mint the next generation (pending) and write its handoff; returns the child's extra env."""
        self.abandon()
        self.retry_debts()
        self.sweep_stale()
        minted = self._store().mint(self.instance_id, pending=True)
        # Tracked from here on: whatever fails below, this generation is revoked.
        self.generation = minted["generation"]
        self.activated = False
        try:
            name = f"handoff-{secrets.token_hex(8)}.json"
            root_fd, run_fd = open_anchored_run_dir(self.root, create=True)
            try:
                descriptor = os.open(
                    name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=run_fd,
                )
                try:
                    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                        json.dump({
                            "token": minted["token"], "lodge_id": minted["lodge_id"],
                            "instance_id": self.instance_id, "generation": minted["generation"],
                            "issued": minted["issued"],
                        }, handle)
                        handle.flush()
                        os.fsync(handle.fileno())
                except BaseException:
                    try:
                        os.unlink(name, dir_fd=run_fd)
                    except OSError:
                        pass
                    raise
            finally:
                os.close(run_fd)
                os.close(root_fd)
        except BaseException:
            self.abandon()
            raise
        self.pending = name
        self.issued_at = self._clock()
        return {
            "WOLTSPACE_CONNECTOR_TOKEN_FILE": str(self.run_dir / name),
            "WOLTSPACE_HANDOFF_ROOT": self.root,
            "WOLTSPACE_LODGE_ID": minted["lodge_id"],
            "WOLTSPACE_CONNECTOR_INSTANCE": self.instance_id,
            "WOLTSPACE_CONNECTOR_GENERATION": str(minted["generation"]),
            "WOLTSPACE_HANDOFF_WINDOW": str(CONSUME_TIMEOUT),
        }

    def check(self) -> str | None:
        """Whether this generation's handoff is still on course.

        None while the child may still take it, or once it has (the file is
        gone *and* the generation was activated by the child). Otherwise
        the reason it failed, with the generation revoked: a file still
        there after the window, or gone without activation in it. Debts are
        retried on every call, whatever the state.
        """
        self.retry_debts()
        if self.generation is None or self.activated:
            return None
        elapsed = self._clock() - self.issued_at
        state = self._token_state()
        if state == "active":
            self.activated = True
            self.pending = None
            return None
        if state == "revoked":
            self.pending_cleanup()
            self.generation = None
            return "the connector's generation was revoked before it started"
        if elapsed < CONSUME_TIMEOUT:
            return None
        taken = self.pending is not None and not self._pending_exists()
        self.abandon()
        if taken:
            return (
                f"the handoff was taken but the connector never acknowledged it within "
                f"{CONSUME_TIMEOUT:g}s; token revoked"
            )
        return f"the connector did not take its handoff within {CONSUME_TIMEOUT:g}s; token revoked"

    def abandon(self) -> None:
        """Revoke the generation in flight, whatever happens to its file; then remove the file.

        Never raises: a revocation or a removal that fails becomes a debt.
        """
        generation, self.generation = self.generation, None
        self.activated = False
        if generation is not None:
            self._revoke(generation)
        self.pending_cleanup()

    def pending_cleanup(self) -> None:
        name, self.pending = self.pending, None
        if name is not None:
            self._remove(name)

    def _remove(self, name: str) -> None:
        try:
            root_fd, run_fd = open_anchored_run_dir(self.root, create=False)
        except OSError as exc:
            self.uncleaned[name] = str(exc)
            return
        try:
            os.unlink(name, dir_fd=run_fd)
            self.uncleaned.pop(name, None)
        except FileNotFoundError:
            self.uncleaned.pop(name, None)
        except OSError as exc:
            self.uncleaned[name] = str(exc)
        finally:
            os.close(run_fd)
            os.close(root_fd)

    def _pending_exists(self) -> bool:
        try:
            info = os.lstat(self.run_dir / self.pending)
        except OSError:
            return False
        return stat.S_ISREG(info.st_mode)

    def sweep_stale(self) -> int:
        """Revoke pending generations and remove handoff files older than STALE_AFTER."""
        cutoff = self._clock() - STALE_AFTER
        try:
            self._store().revoke_stale_pending(cutoff)
            self.unrevoked.pop(-1, None)
        except Exception as exc:  # noqa: BLE001 - the registry may be unreadable; retried later
            self.unrevoked[-1] = f"stale sweep: {exc}"
        removed = 0
        try:
            root_fd, run_fd = open_anchored_run_dir(self.root, create=False)
        except OSError:
            return 0
        try:
            for entry in os.listdir(run_fd):
                if not entry.startswith("handoff-") or not entry.endswith(".json"):
                    continue
                try:
                    info = os.stat(entry, dir_fd=run_fd, follow_symlinks=False)
                    if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                        os.unlink(entry, dir_fd=run_fd)
                        removed += 1
                except OSError:
                    continue
        finally:
            os.close(run_fd)
            os.close(root_fd)
        return removed

    # ------------------------------------------------------------ the ledger

    @property
    def ledger(self) -> "DebtLedger":
        return DebtLedger(self.root)

    def _note_debt(self, generation: int, owed: bool) -> None:
        """Write the debt down (or strike it) so the next supervisor settles it.

        A ledger that cannot be written leaves the debt in memory only, and
        says so: `status()["unrevoked"][generation]["durable"]` is False.
        """
        try:
            if owed:
                self.ledger.add(self.instance_id, generation)
            else:
                self.ledger.remove(self.instance_id, generation)
            self.non_durable.pop(generation, None)
        except (OSError, DebtLedgerUnreadable) as exc:
            if owed:
                self.non_durable[generation] = str(exc)

    # ------------------------------------------------------------ revocation

    def _revoke(self, generation: int) -> None:
        try:
            self._store().revoke_generation(self.instance_id, generation)
        except Exception as exc:  # noqa: BLE001 - kept, written down, retried, shown
            self.unrevoked[generation] = str(exc)
            self._note_debt(generation, owed=True)
            return
        if self.unrevoked.pop(generation, None) is not None:
            self._note_debt(generation, owed=False)
        self.non_durable.pop(generation, None)

    def retry_debts(self) -> None:
        """Every revocation or removal that failed is tried again; nothing is forgotten."""
        for generation in list(self.unrevoked):
            if generation == -1:
                try:
                    self._store().revoke_stale_pending(self._clock() - STALE_AFTER)
                    self.unrevoked.pop(generation, None)
                except Exception as exc:  # noqa: BLE001
                    self.unrevoked[generation] = str(exc)
            else:
                self._revoke(generation)
        for name in list(self.uncleaned):
            self._remove(name)

    retry_revocations = retry_debts

    def _token_state(self) -> str | None:
        try:
            return self._store().token_state(self.instance_id, self.generation)
        except Exception:  # noqa: BLE001 - unreadable registry: treated as not activated
            return None

    def status(self) -> dict:
        """What the supervisor shows: the generation in flight and any debt still owed."""
        return {
            "generation": self.generation, "activated": self.activated,
            "pending_file": self.pending is not None,
            "unrevoked": {
                str(g): {"error": e, "durable": g not in self.non_durable}
                | ({"ledger": self.non_durable[g]} if g in self.non_durable else {})
                for g, e in self.unrevoked.items()
            },
            "uncleaned": dict(self.uncleaned),
        }


class DebtLedgerUnreadable(RuntimeError):
    """The ledger exists but cannot be trusted; it is kept as it is, never replaced."""


class DebtLedger:
    """Exact (instance, generation) revocation obligations, in the anchored run dir.

    Absence is an empty ledger. Anything else that is not a small regular
    JSON list of records is `DebtLedgerUnreadable`, and no write ever
    replaces such a file. Every read-modify-write holds `debts.lock`
    (flock, across processes); the temp name is unique per process and
    attempt and the publish is an fd-relative rename.
    """

    def __init__(self, root: str):
        self.root = root

    def _run(self, *, create: bool):
        return open_anchored_run_dir(self.root, create=create)

    def _read_in(self, run_fd: int) -> list[tuple[str, int]]:
        try:
            descriptor = os.open(DEBTS_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=run_fd)
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise DebtLedgerUnreadable(f"{DEBTS_FILE}: {exc}") from None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise DebtLedgerUnreadable(f"{DEBTS_FILE} is not a regular file")
            if info.st_size > DEBTS_MAX_BYTES:
                raise DebtLedgerUnreadable(f"{DEBTS_FILE} is over {DEBTS_MAX_BYTES} bytes")
            raw = os.read(descriptor, DEBTS_MAX_BYTES + 1)
        finally:
            os.close(descriptor)
        try:
            data = json.loads(raw)
        except ValueError:
            raise DebtLedgerUnreadable(f"{DEBTS_FILE} does not parse") from None
        if not isinstance(data, list):
            raise DebtLedgerUnreadable(f"{DEBTS_FILE} has an unknown shape")
        debts = []
        for entry in data:
            if (
                not isinstance(entry, dict) or not isinstance(entry.get("instance_id"), str)
                or isinstance(entry.get("generation"), bool) or not isinstance(entry.get("generation"), int)
            ):
                raise DebtLedgerUnreadable(f"{DEBTS_FILE} has an unknown shape")
            debts.append((entry["instance_id"], entry["generation"]))
        return debts

    def _write_in(self, run_fd: int, debts: list[tuple[str, int]]) -> None:
        tmp = f".{DEBTS_FILE}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=run_fd)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump([{"instance_id": i, "generation": g} for i, g in debts], handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.rename(tmp, DEBTS_FILE, src_dir_fd=run_fd, dst_dir_fd=run_fd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=run_fd)
            except OSError:
                pass
            raise

    def _locked(self, action, *, create: bool):
        try:
            root_fd, run_fd = self._run(create=create)
        except FileNotFoundError:
            if create:
                raise
            # No run directory yet: nothing was ever written down. Absence, not damage.
            return []
        try:
            # Created exclusively first, then opened: on macOS two openat(O_CREAT)
            # of the same new name can answer ENOENT to the loser.
            try:
                os.close(os.open(DEBTS_LOCK, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=run_fd))
            except FileExistsError:
                pass
            lock = os.open(DEBTS_LOCK, os.O_WRONLY | os.O_NOFOLLOW, dir_fd=run_fd)
            try:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    return action(run_fd)
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
            finally:
                os.close(lock)
        finally:
            os.close(run_fd)
            os.close(root_fd)

    def read(self) -> list[tuple[str, int]]:
        """Read-only: a ledger that was never started is empty; nothing is created."""
        return self._locked(self._read_in, create=False)

    def add(self, instance_id: str, generation: int) -> None:
        def action(run_fd):
            debts = self._read_in(run_fd)          # unreadable: raises, nothing replaced
            if (instance_id, generation) not in debts:
                self._write_in(run_fd, [*debts, (instance_id, generation)])
        self._locked(action, create=True)

    def remove(self, instance_id: str, generation: int) -> None:
        def action(run_fd):
            debts = self._read_in(run_fd)
            if (instance_id, generation) in debts:
                self._write_in(run_fd, [d for d in debts if d != (instance_id, generation)])
        self._locked(action, create=False)

    def status(self) -> dict:
        """{"readable": bool, "debts": [...], "error": str|None} without raising."""
        try:
            return {"readable": True, "debts": [list(d) for d in self.read()], "error": None}
        except DebtLedgerUnreadable as exc:
            return {"readable": False, "debts": [], "error": str(exc)}
        except OSError as exc:
            return {"readable": False, "debts": [], "error": f"ledger unavailable: {exc}"}


class ConnectorRecovery:
    """The lodge-wide step: every recorded debt settled, every stale pending revoked.

    Independent of launch planning: it runs whether or not any connector is
    enabled, for every instance in the ledger, and touches exactly those
    generations. An unreadable ledger is a failure to report and retry,
    never an empty ledger.
    """

    def __init__(self, root: Path, store_factory: Callable[[], object], *, clock: Callable[[], float] = time.time):
        self.root = canonical_root(root)
        self._store_factory = store_factory
        self._clock = clock
        self.last: dict = {"ran": False}

    def run(self) -> dict:
        ledger = DebtLedger(self.root)
        outcome = {"ran": True, "at": self._clock(), "settled": [], "owed": [], "errors": []}
        try:
            debts = ledger.read()
        except (DebtLedgerUnreadable, OSError) as exc:
            outcome["errors"].append(f"ledger: {exc}")
            debts = None
        if debts is not None:
            try:
                store = self._store_factory()
            except Exception as exc:  # noqa: BLE001 - the lodge's state code or registry is unavailable
                store = None
                outcome["errors"].append(f"store: {exc}")
            for instance_id, generation in debts:
                if store is None:
                    outcome["owed"].append([instance_id, generation])
                    continue
                try:
                    try:
                        store.revoke_generation(instance_id, generation)
                    except Exception as exc:  # noqa: BLE001
                        # An instance that is gone took its tokens with it; anything else is still owed.
                        if getattr(exc, "reason", "") != "unknown_instance":
                            raise
                    ledger.remove(instance_id, generation)
                    outcome["settled"].append([instance_id, generation])
                except Exception as exc:  # noqa: BLE001 - kept owed, retried next time
                    outcome["owed"].append([instance_id, generation])
                    outcome["errors"].append(f"{instance_id}/{generation}: {exc}")
        try:
            store = self._store_factory()
            for instance_id, generation in store.revoke_stale_pending(self._clock() - CONSUME_TIMEOUT):
                outcome["settled"].append([instance_id, generation])
        except Exception as exc:  # noqa: BLE001
            outcome["errors"].append(f"stale pending: {exc}")
        outcome["ok"] = not outcome["errors"] and not outcome["owed"]
        self.last = outcome
        return outcome
