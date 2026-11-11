"""The audit log: the one writer of `audit-event` v1, its checkpoints, retention, key rotation
and verification (application, #437).

- **One total order.** `record` locks the chain head first (`FOR UPDATE` on PostgreSQL;
  `BEGIN IMMEDIATE` already serializes SQLite), so `seq` is gapless and `at` never goes back.
- **Atomic with what it records.** `record` writes into the caller's transaction: an access
  change and its event commit together or not at all.
- **Fail closed.** An event that breaks the contract (`AUDIT_EVENT_INVALID`), or a key the log
  does not know (`AUDIT_KEY_UNSAFE`), raises inside the caller's transaction, which rolls back.
  `record_alone`, for an event with no change of its own (a read, a refusal), raises
  `AUDIT_UNAVAILABLE` when it cannot commit, so the result it would have recorded is not
  delivered. Nothing is buffered in memory.
- **No recursion.** A refusal whose own event cannot be recorded goes to the process log as one
  line, without details, and is counted. It is never retried through the audit log.
- **Signatures.** Every write signs the new head. A checkpoint is signed every
  `checkpoint_every` events, and when the last one is older than `checkpoint_period`. After each
  commit, the checkpoints not yet in the anchor file are appended to it.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import secrets
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from functools import cache
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator
from orvex.access.audit import chain
from orvex.access.audit.canonical import NotInProfile, canonical
from orvex.access.audit.signing_key import SigningKey
from orvex.access.errors import (
    AUDIT_EVENT_INVALID,
    AUDIT_KEY_UNSAFE,
    AUDIT_RETENTION_INVALID,
    AUDIT_UNAVAILABLE,
    AccessError,
)
from orvex.access.storage import retry
from orvex.access.storage.store import (
    Row,
    Store,
    Transaction,
    TransientConflict,
    UnreadableValue,
)

SCHEMA_FILE = "audit-event-v1.json"
CHECKPOINT_EVERY = 1000
CHECKPOINT_PERIOD = timedelta(days=1)
MINIMUM_RETENTION_DAYS = 30
SYSTEM = "system"
LOG = logging.getLogger(__name__)

type Outcome = Literal["allowed", "refused"]


@cache
def _validator() -> Draft202012Validator:
    schema = json.loads(files("orvex.access").joinpath(SCHEMA_FILE).read_text("utf-8"))
    return Draft202012Validator(schema)


def _instant(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _event_id() -> str:
    return secrets.token_urlsafe(16)


def _read(transaction: Transaction, table: str, sql: str) -> list[Row]:
    try:
        return transaction.rows(sql)
    except UnreadableValue as error:
        raise chain.unreadable(table) from error


def _checkpoints(transaction: Transaction) -> list[chain.CheckpointRow]:
    return [
        chain.CheckpointRow(int(str(s)), str(h), str(a), str(k), str(g))
        for s, h, a, k, g in _read(
            transaction,
            "audit_checkpoints",
            "SELECT seq, hash, at, key, signature FROM audit_checkpoints ORDER BY seq",
        )
    ]


def _invalid_event(reason: str) -> AccessError:
    return AccessError(AUDIT_EVENT_INVALID, "The audit event breaks its contract.", {"at": reason})


class AuditLog:
    def __init__(
        self,
        store: Store,
        key: SigningKey,
        anchors: Path,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_id: Callable[[], str] = _event_id,
        checkpoint_every: int = CHECKPOINT_EVERY,
        checkpoint_period: timedelta = CHECKPOINT_PERIOD,
    ) -> None:
        self._store = store
        self._key = key
        self._anchors = anchors
        self._clock = clock
        self._new_id = new_id
        self._every = checkpoint_every
        self._period = checkpoint_period
        self.unrecorded = 0  # actions or refusals whose event could not be recorded
        self.unanchored = 0  # anchor-file writes that failed and wait for the next commit
        self._unanchored = True  # a checkpoint may be missing from the anchor file

    @property
    def signing_key(self) -> SigningKey:
        return self._key

    @property
    def anchors(self) -> Path:
        return self._anchors

    # --- writing -------------------------------------------------------------------------------

    def record(
        self,
        transaction: Transaction,
        kind: str,
        actor: str,
        details: Mapping[str, Any],
        *,
        outcome: Outcome = "allowed",
        project: str | None = None,
    ) -> str:
        """Append one event inside `transaction`; return its id. Any failure but a transient
        conflict (which the caller retries) is `AUDIT_*`: the caller's transaction rolls back."""
        try:
            return self._append(transaction, kind, actor, details, outcome, project)
        except AccessError, TransientConflict:
            raise
        except Exception as error:
            raise AccessError(AUDIT_UNAVAILABLE, "The audit log cannot record now.", {}) from error

    def _append(
        self,
        transaction: Transaction,
        kind: str,
        actor: str,
        details: Mapping[str, Any],
        outcome: Outcome,
        project: str | None,
    ) -> str:
        self._known_key(transaction)
        ((seq, previous, last_at),) = transaction.rows(
            f"SELECT seq, hash, at FROM audit_head WHERE id = 1{transaction.locking}"
        )
        now = _instant(self._clock())
        at = max(now, str(last_at)) if last_at is not None else now
        ((instance,),) = transaction.rows("SELECT instance_id FROM instance")
        document: dict[str, Any] = {
            "version": 1,
            "id": self._new_id(),
            "at": at,
            "actor": actor,
            "instance": str(instance),
            "kind": kind,
            "outcome": outcome,
            "details": dict(details),
        }
        if project is not None:
            document["project"] = project
        error = next(iter(_validator().iter_errors(document)), None)
        if error is not None:
            raise _invalid_event("/".join(map(str, error.absolute_path)) or "document")
        try:
            data = canonical(document)
        except NotInProfile as problem:
            raise _invalid_event(str(problem)) from problem
        current = int(str(seq)) + 1
        hashed = chain.link(str(previous), data)
        transaction.run(
            "INSERT INTO audit_events (seq, id, at, kind, document, hash) "
            "VALUES (:seq, :id, :at, :kind, :document, :hash)",
            seq=current,
            id=document["id"],
            at=at,
            kind=kind,
            document=data.decode("utf-8"),
            hash=hashed,
        )
        if self._checkpoint_due(transaction, current, at):
            self._checkpoint(transaction, current, hashed, at)
        signature = self._key.sign(chain.head_payload(current, hashed)).hex()
        transaction.run(
            "UPDATE audit_head SET seq = :seq, hash = :hash, at = :at, key = :key, "
            "signature = :signature WHERE id = 1",
            seq=current,
            hash=hashed,
            at=at,
            key=self._key.fingerprint,
            signature=signature,
        )
        return str(document["id"])

    def record_alone(
        self,
        kind: str,
        actor: str,
        details: Mapping[str, Any],
        *,
        outcome: Outcome = "allowed",
        project: str | None = None,
    ) -> str:
        """Record an event that has no change of its own, in its own transaction. When it
        cannot be committed, `AUDIT_UNAVAILABLE`: what it would have recorded must not happen."""
        try:
            recorded = retry.write(
                self._store,
                lambda transaction: self.record(
                    transaction, kind, actor, details, outcome=outcome, project=project
                ),
            )
        except AccessError:
            raise
        except Exception as error:
            raise AccessError(AUDIT_UNAVAILABLE, "The audit log cannot record now.", {}) from error
        self.committed()
        return recorded

    def refusal(
        self,
        operation: str,
        actor: str,
        code: str,
        *,
        project: str | None = None,
        target: str | None = None,
    ) -> None:
        """Record a refusal. If even that fails, one process-log line and a count; never a
        second attempt through the log, and never an exception that hides the refusal."""
        details: dict[str, str] = {"operation": operation, "code": code}
        if target is not None:
            details["target"] = target
        try:
            self.record_alone("access.refused", actor, details, outcome="refused", project=project)
        except AccessError:
            self.unrecorded += 1
            LOG.error("audit: a refusal could not be recorded")

    def committed(self) -> None:
        """Call after a transaction that recorded events committed: anchors its checkpoints, if
        it wrote any (and, the first time, any an earlier process left unanchored). The action
        is committed already, so a failure here is never reported as its failure: one
        process-log line, counted, and the next commit tries again."""
        if not self._unanchored:
            return
        try:
            self.sync_anchors()
        except Exception:  # noqa: BLE001 - the action is committed: nothing may fail it now
            self.unanchored += 1
            LOG.error("audit: the anchor file could not be written")
            return
        self._unanchored = False

    def unavailable(self) -> None:
        """The audit log failed during an action, which is refused: one process-log line and a
        count, and no second attempt through the log (#437 item 6)."""
        self.unrecorded += 1
        LOG.error("audit: an action was refused because its event could not be recorded")

    # --- checkpoints and anchors ---------------------------------------------------------------

    def _known_key(self, transaction: Transaction) -> None:
        """The log signs only with the newest key it knows; the first key is registered."""
        rows = transaction.rows("SELECT key, previous FROM audit_keys")
        if not rows:
            transaction.run(
                "INSERT INTO audit_keys (key, public_key, created_at) VALUES (:key, :public, :at)",
                key=self._key.fingerprint,
                public=self._key.public.hex(),
                at=_instant(self._clock()),
            )
            return
        names = {str(key) for key, _ in rows}
        replaced = {str(previous) for _, previous in rows if previous is not None}
        if self._key.fingerprint not in names or self._key.fingerprint in replaced:
            raise AccessError(
                AUDIT_KEY_UNSAFE, "The signing key is not the audit log's current key.", {}
            )

    def _checkpoint_due(self, transaction: Transaction, seq: int, at: str) -> bool:
        if seq % self._every == 0:
            return True
        ((last,),) = transaction.rows("SELECT max(at) FROM audit_checkpoints")
        if last is None:
            return seq == 1
        then = datetime.strptime(str(last), "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
        now = datetime.strptime(at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
        return now - then >= self._period

    def _checkpoint(self, transaction: Transaction, seq: int, hashed: str, at: str) -> None:
        self._unanchored = True
        key = self._key.fingerprint
        signature = self._key.sign(chain.checkpoint_payload(seq, hashed, at, key)).hex()
        transaction.run(
            "INSERT INTO audit_checkpoints (seq, hash, at, key, signature) "
            "VALUES (:seq, :hash, :at, :key, :signature)",
            seq=seq,
            hash=hashed,
            at=at,
            key=key,
            signature=signature,
        )

    def checkpoints(self) -> list[chain.CheckpointRow]:
        with self._store.transaction(read_only=True) as transaction:
            return _checkpoints(transaction)

    def _local_anchors(self) -> list[str]:
        """The anchor file's complete lines; a partial last line (a cut append) is left out."""
        try:
            text = self._anchors.read_text("ascii")
        except FileNotFoundError:
            return []
        except (OSError, UnicodeDecodeError) as error:
            raise AccessError(AUDIT_UNAVAILABLE, "The anchor file cannot be read.", {}) from error
        lines = text.splitlines()
        if text and not text.endswith("\n"):
            lines = lines[:-1]
        return [line for line in lines if line.strip()]

    def sync_anchors(self) -> None:
        """Append to the anchor file every checkpoint it does not hold yet. A process lock keeps
        two writers from appending the same line twice."""
        flags = os.O_CREAT | os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self._anchors, flags, 0o600)
        with os.fdopen(descriptor, "r+", encoding="ascii") as file:
            fcntl.flock(file, fcntl.LOCK_EX)
            file.seek(0)
            text = file.read()
            lines = [line for line in text.splitlines() if line.strip()]
            if text and not text.endswith("\n"):
                # A crash cut the last append short: drop the partial line, keep the rest.
                lines = lines[:-1]
                file.seek(0)
                file.truncate()
                file.write("".join(line + "\n" for line in lines))
            last = int(json.loads(lines[-1])["seq"]) if lines else 0
            fresh = [row for row in self.checkpoints() if row.seq > last]
            if fresh:
                file.write("".join(row.anchor() + "\n" for row in fresh))
                file.flush()
                os.fsync(file.fileno())

    # --- retention -----------------------------------------------------------------------------

    def retain(self, retention_days: int) -> tuple[int, int] | None:
        """Remove the events older than `retention_days`, up to a signed checkpoint, which stays.
        Return the range removed, or None when nothing is that old."""
        if retention_days < MINIMUM_RETENTION_DAYS:
            raise AccessError(
                AUDIT_RETENTION_INVALID,
                f"Audit retention is at least {MINIMUM_RETENTION_DAYS} days.",
                {"retention_days": str(retention_days)},
            )
        cutoff = _instant(self._clock() - timedelta(days=retention_days))

        def work(transaction: Transaction) -> tuple[int, int] | None:
            transaction.rows(f"SELECT seq FROM audit_head WHERE id = 1{transaction.locking}")
            # Only a log that verifies is trimmed: its `at` columns then equal the signed,
            # chained documents, so no database writer can make old what is recent.
            self._verify(transaction, self._local_anchors())
            ((oldest, first),) = transaction.rows(
                "SELECT max(seq), min(seq) FROM audit_events WHERE at < :cutoff", cutoff=cutoff
            )
            if oldest is None:
                return None
            ((through,),) = transaction.rows(
                "SELECT max(seq) FROM audit_checkpoints WHERE seq <= :oldest AND seq >= :first",
                oldest=oldest,
                first=first,
            )
            if through is None:
                raise AccessError(
                    AUDIT_RETENTION_INVALID,
                    "No signed checkpoint covers the events retention would remove.",
                    {"reason": "no_checkpoint"},
                )
            removed = (int(str(first)), int(str(through)))
            self._store.remove_audit_through(transaction, removed[1])
            self.record(
                transaction,
                "audit.retention",
                SYSTEM,
                {"first_seq": removed[0], "last_seq": removed[1], "checkpoint_seq": removed[1]},
            )
            return removed

        removed = retry.write(self._store, work)
        self.committed()
        return removed

    # --- key rotation --------------------------------------------------------------------------

    def rotate(self, new_key: SigningKey, actor: str = SYSTEM) -> None:
        """Make `new_key` the log's key. Each key signs the other, and the rotation is recorded
        (signed by the old key, which is still trusted)."""
        old = self._key

        def work(transaction: Transaction) -> None:
            self._known_key(transaction)
            transaction.run(
                "INSERT INTO audit_keys "
                "(key, public_key, created_at, previous, previous_signs, signs_previous) "
                "VALUES (:key, :public, :at, :previous, :previous_signs, :signs_previous)",
                key=new_key.fingerprint,
                public=new_key.public.hex(),
                at=_instant(self._clock()),
                previous=old.fingerprint,
                previous_signs=old.sign(chain.next_key_payload(new_key.public)).hex(),
                signs_previous=new_key.sign(chain.previous_key_payload(old.public)).hex(),
            )
            self._key = new_key
            try:
                self.record(
                    transaction,
                    "audit.key_rotated",
                    actor,
                    {"previous_key": old.fingerprint, "key": new_key.fingerprint},
                )
            except BaseException:
                self._key = old
                raise

        try:
            retry.write(self._store, work)
        except BaseException:
            self._key = old
            raise
        self._unanchored = True
        self.committed()

    # --- verification --------------------------------------------------------------------------

    def verify(self, anchors: Sequence[str] = ()) -> int:
        """Check the whole log against the current key and `anchors` (lines shipped outside);
        return how many events verified, or raise the first problem."""
        with self._store.transaction(read_only=True) as transaction:
            return self._verify(transaction, anchors)

    def _verify(self, transaction: Transaction, anchors: Sequence[str]) -> int:
        events = [
            chain.EventRow(int(str(s)), str(i), str(a), str(k), str(d), str(h))
            for s, i, a, k, d, h in _read(
                transaction,
                "audit_events",
                "SELECT seq, id, at, kind, document, hash FROM audit_events ORDER BY seq",
            )
        ]
        keys = [
            chain.KeyRow(
                str(k),
                str(p),
                None if v is None else str(v),
                None if a is None else str(a),
                None if b is None else str(b),
            )
            for k, p, v, a, b in _read(
                transaction,
                "audit_keys",
                "SELECT key, public_key, previous, previous_signs, signs_previous FROM audit_keys",
            )
        ]
        ((seq, hashed, key, signature),) = _read(
            transaction,
            "audit_head",
            "SELECT seq, hash, key, signature FROM audit_head WHERE id = 1",
        )
        checkpoints = _checkpoints(transaction)
        head = chain.HeadRow(
            int(str(seq)),
            str(hashed),
            None if key is None else str(key),
            None if signature is None else str(signature),
        )
        trusted = chain.trusted_keys(self._key.public, keys)
        return chain.verify(events, checkpoints, head, trusted, anchors)
