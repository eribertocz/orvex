"""The SQLite access store of local mode (adapter); server mode always uses PostgreSQL.

- One file, created owner-only (`0600`); SQLite gives its `-wal` and `-shm` files the same
  permissions. A file others can read or write, or a link, is refused.
- Every connection turns foreign keys on and checks that they are on; a connection without them
  is refused.
- A write transaction starts with `BEGIN IMMEDIATE`, which takes the database's one write lock at
  once. Two transactions that would each count a project's Builders therefore run one after the
  other, and neither reads a count the other is about to change.
- Each thread has its own connection, never used by another thread; `close` closes them all.
- The tables are `STRICT`, so a value of the wrong type is refused, and every constraint the
  directory relies on is also written here.
"""

from __future__ import annotations

import errno
import os
import sqlite3
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

from orvex.access.errors import ACCESS_STORE_UNSAFE, AccessError
from orvex.access.storage.store import (
    Row,
    Transaction,
    TransientConflict,
    UniqueViolation,
    UnreadableValue,
)

BUSY_TIMEOUT_S = 10.0

_ID = "length({c}) BETWEEN 1 AND 128 AND {c} NOT GLOB '*[^A-Za-z0-9_-]*'"
_FINGERPRINT = "length({c}) = 71 AND {c} GLOB 'sha256:*' AND substr({c}, 8) NOT GLOB '*[^0-9a-f]*'"
_HEX = "length({c}) = {n} AND {c} NOT GLOB '*[^0-9a-f]*'"
_INSTANT = "length({c}) = 27 AND {c} GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-3][0-9]T*Z'"
# Every character spelled out, so no byte that is not text can hide behind a wildcard.
_EXACT_INSTANT = (
    "length({c}) = 27 AND {c} GLOB '[0-9][0-9][0-9][0-9]-[01][0-9]-[0-3][0-9]"
    "T[0-2][0-9]:[0-5][0-9]:[0-5][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'"
)

MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        """CREATE TABLE store_meta (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            schema_version INTEGER NOT NULL CHECK (schema_version >= 0),
            recovery_digest TEXT CHECK (
                recovery_digest IS NULL
                OR (length(recovery_digest) = 64 AND recovery_digest NOT GLOB '*[^0-9a-f]*')
            )
        ) STRICT""",
        "INSERT INTO store_meta (id, schema_version) VALUES (1, 0)",
        f"""CREATE TABLE users (
            id TEXT PRIMARY KEY NOT NULL CHECK ({_ID.format(c="id")}),
            user_name TEXT NOT NULL CHECK (
                length(user_name) BETWEEN 1 AND 128
                AND user_name GLOB '[A-Za-z0-9]*'
                AND user_name NOT GLOB '*[^A-Za-z0-9._@+-]*'
            ),
            display_name TEXT NOT NULL CHECK (length(display_name) BETWEEN 1 AND 200),
            state TEXT NOT NULL CHECK (state IN ('active', 'deactivated')),
            administrator INTEGER NOT NULL CHECK (administrator IN (0, 1)),
            authz_version INTEGER NOT NULL CHECK (authz_version >= 1),
            created_at TEXT NOT NULL CHECK ({_INSTANT.format(c="created_at")}),
            changed_at TEXT NOT NULL CHECK ({_INSTANT.format(c="changed_at")})
        ) STRICT""",
        "CREATE UNIQUE INDEX users_user_name ON users (lower(user_name))",
        f"""CREATE TABLE memberships (
            project TEXT NOT NULL CHECK ({_ID.format(c="project")}),
            user_id TEXT NOT NULL REFERENCES users (id),
            role TEXT NOT NULL CHECK (role IN ('builder', 'author', 'viewer')),
            PRIMARY KEY (project, user_id)
        ) STRICT""",
        "CREATE INDEX memberships_user ON memberships (user_id)",
        f"""CREATE TABLE project_groups (
            project TEXT NOT NULL CHECK ({_ID.format(c="project")}),
            name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 64),
            PRIMARY KEY (project, name)
        ) STRICT""",
        """CREATE TABLE group_members (
            project TEXT NOT NULL,
            group_name TEXT NOT NULL,
            user_id TEXT NOT NULL,
            PRIMARY KEY (project, group_name, user_id),
            FOREIGN KEY (project, group_name)
                REFERENCES project_groups (project, name) ON DELETE CASCADE,
            FOREIGN KEY (project, user_id)
                REFERENCES memberships (project, user_id) ON DELETE CASCADE
        ) STRICT""",
        "CREATE INDEX group_members_member ON group_members (project, user_id)",
    ),
    # 2: the audit log (#437). Append-only: the triggers refuse every change but retention's.
    # The key columns are checked by `verify`, which trusts a key only through signatures, as on
    # PostgreSQL, where a foreign key to `audit_keys` would need an UPDATE grant.
    (
        f"""CREATE TABLE instance (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            instance_id TEXT NOT NULL CHECK ({_ID.format(c="instance_id")})
        ) STRICT""",
        "INSERT INTO instance (id, instance_id) VALUES (1, lower(hex(randomblob(16))))",
        f"""CREATE TABLE audit_events (
            seq INTEGER PRIMARY KEY CHECK (seq >= 1),
            id TEXT NOT NULL UNIQUE CHECK ({_ID.format(c="id")}),
            at TEXT NOT NULL CHECK ({_EXACT_INSTANT.format(c="at")}),
            kind TEXT NOT NULL CHECK (length(kind) BETWEEN 1 AND 64),
            document TEXT NOT NULL CHECK (length(document) BETWEEN 2 AND 65536),
            hash TEXT NOT NULL CHECK ({_HEX.format(c="hash", n=64)})
        ) STRICT""",
        f"""CREATE TABLE audit_head (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            seq INTEGER NOT NULL CHECK (seq >= 0),
            hash TEXT NOT NULL CHECK ({_HEX.format(c="hash", n=64)}),
            at TEXT CHECK (at IS NULL OR ({_EXACT_INSTANT.format(c="at")})),
            key TEXT,
            signature TEXT CHECK (signature IS NULL OR ({_HEX.format(c="signature", n=128)}))
        ) STRICT""",
        "INSERT INTO audit_head (id, seq, hash) VALUES (1, 0, '" + "0" * 64 + "')",
        f"""CREATE TABLE audit_keys (
            key TEXT PRIMARY KEY NOT NULL CHECK ({_FINGERPRINT.format(c="key")}),
            public_key TEXT NOT NULL UNIQUE CHECK ({_HEX.format(c="public_key", n=64)}),
            created_at TEXT NOT NULL CHECK ({_EXACT_INSTANT.format(c="created_at")}),
            previous TEXT,
            previous_signs TEXT CHECK (
                previous_signs IS NULL OR ({_HEX.format(c="previous_signs", n=128)})
            ),
            signs_previous TEXT CHECK (
                signs_previous IS NULL OR ({_HEX.format(c="signs_previous", n=128)})
            ),
            CHECK ((previous IS NULL) = (previous_signs IS NULL)),
            CHECK ((previous IS NULL) = (signs_previous IS NULL))
        ) STRICT""",
        f"""CREATE TABLE audit_checkpoints (
            seq INTEGER PRIMARY KEY CHECK (seq >= 1),
            hash TEXT NOT NULL CHECK ({_HEX.format(c="hash", n=64)}),
            at TEXT NOT NULL CHECK ({_EXACT_INSTANT.format(c="at")}),
            key TEXT NOT NULL CHECK ({_FINGERPRINT.format(c="key")}),
            signature TEXT NOT NULL CHECK ({_HEX.format(c="signature", n=128)})
        ) STRICT""",
        """CREATE TABLE audit_retention (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            through INTEGER NOT NULL CHECK (through >= 0)
        ) STRICT""",
        "INSERT INTO audit_retention (id, through) VALUES (1, 0)",
        """CREATE TRIGGER audit_events_append_only BEFORE UPDATE ON audit_events
            BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END""",
        """CREATE TRIGGER audit_events_retention_only BEFORE DELETE ON audit_events
            WHEN OLD.seq > (SELECT through FROM audit_retention)
            BEGIN SELECT RAISE(ABORT, 'audit events are removed only by retention'); END""",
        """CREATE TRIGGER audit_checkpoints_append_only BEFORE UPDATE ON audit_checkpoints
            BEGIN SELECT RAISE(ABORT, 'audit checkpoints are append-only'); END""",
        """CREATE TRIGGER audit_checkpoints_kept BEFORE DELETE ON audit_checkpoints
            BEGIN SELECT RAISE(ABORT, 'audit checkpoints are never removed'); END""",
        """CREATE TRIGGER audit_keys_append_only BEFORE UPDATE ON audit_keys
            BEGIN SELECT RAISE(ABORT, 'audit keys are append-only'); END""",
        """CREATE TRIGGER audit_keys_kept BEFORE DELETE ON audit_keys
            BEGIN SELECT RAISE(ABORT, 'audit keys are never removed'); END""",
    ),
)


def _write_ahead_log(connection: sqlite3.Connection) -> None:
    """Switch the file to WAL, which it keeps from then on. The switch needs the file to itself,
    and SQLite does not wait for that: another connection opening the same new file at the same
    moment makes it fail at once with `database is locked`, so it is tried again for a while."""
    deadline = time.monotonic() + BUSY_TIMEOUT_S
    while True:
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as error:
            if "database is locked" not in str(error) or time.monotonic() > deadline:
                connection.close()
                raise TransientConflict(str(error)) from error
            time.sleep(0.01)


class _Transaction:
    locking = ""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def rows(self, sql: str, /, **parameters: object) -> list[Row]:
        cursor = self._execute(sql, parameters)
        try:
            return [tuple(row) for row in cursor.fetchall()]
        except sqlite3.OperationalError as error:
            if str(error).startswith("Could not decode to UTF-8"):
                raise UnreadableValue(str(error)) from error
            raise

    def run(self, sql: str, /, **parameters: object) -> None:
        self._execute(sql, parameters)

    def _execute(self, sql: str, parameters: dict[str, object]) -> sqlite3.Cursor:
        try:
            return self._connection.execute(sql, parameters)
        except sqlite3.IntegrityError as error:
            if str(error).startswith("UNIQUE constraint failed"):
                raise UniqueViolation(str(error)) from error
            raise
        except sqlite3.OperationalError as error:
            if "database is locked" in str(error):
                raise TransientConflict(str(error)) from error
            raise


class SqliteStore:
    """The access store in one SQLite file."""

    def __init__(self, path: Path, migrations: Sequence[Sequence[str]] = MIGRATIONS) -> None:
        self.path = path
        self.migrations = migrations
        self._local = threading.local()
        self._guard = threading.Lock()
        self._open: list[sqlite3.Connection] = []

    def _connection(self) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = getattr(self._local, "connection", None)
        if connection is not None:
            return connection
        # Created owner-only if missing (two threads may race here: neither truncates), never
        # through a link, and refused when someone else can read or write it.
        try:
            descriptor = os.open(
                self.path, os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600
            )
        except OSError as error:
            if error.errno != errno.ELOOP:
                raise
            raise AccessError(
                ACCESS_STORE_UNSAFE, "The access store's file is a link.", {}
            ) from error
        try:
            if os.fstat(descriptor).st_mode & 0o077:
                raise AccessError(
                    ACCESS_STORE_UNSAFE, "Others can read or write the access store's file.", {}
                )
        finally:
            os.close(descriptor)
        connection = sqlite3.connect(
            # Used only by the thread that opened it; `close` may close it from another one.
            self.path,
            isolation_level=None,
            timeout=BUSY_TIMEOUT_S,
            check_same_thread=False,
        )
        connection.execute("PRAGMA foreign_keys = ON")
        _write_ahead_log(connection)
        connection.execute("PRAGMA synchronous = FULL")
        if connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
            connection.close()
            raise AccessError(
                ACCESS_STORE_UNSAFE, "This SQLite build cannot enforce foreign keys.", {}
            )
        self._local.connection = connection
        with self._guard:
            self._open.append(connection)
        return connection

    @contextmanager
    def transaction(self, *, read_only: bool = False) -> Iterator[Transaction]:
        connection = self._connection()
        try:
            connection.execute("BEGIN DEFERRED" if read_only else "BEGIN IMMEDIATE")
        except sqlite3.OperationalError as error:
            raise TransientConflict(str(error)) from error
        try:
            yield _Transaction(connection)
        except BaseException:
            if connection.in_transaction:  # some errors end the transaction themselves
                connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")

    def stored_version(self, transaction: Transaction) -> int:
        tables = transaction.rows(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'store_meta'"
        )
        if not tables:
            return 0
        found = transaction.rows("SELECT schema_version FROM store_meta")
        if len(found) != 1 or not isinstance(found[0][0], int):
            raise AccessError(ACCESS_STORE_UNSAFE, "The access store's version is unreadable.", {})
        return found[0][0]

    def remove_audit_through(self, transaction: Transaction, seq: int) -> None:
        """Open the retention window to `seq`, remove, and close it again, in one transaction:
        the delete trigger refuses any other removal."""
        transaction.run("UPDATE audit_retention SET through = :seq", seq=seq)
        transaction.run("DELETE FROM audit_events WHERE seq <= :seq", seq=seq)
        transaction.run("UPDATE audit_retention SET through = 0")

    def migration_transaction(self) -> AbstractContextManager[Transaction]:
        """A write transaction: `BEGIN IMMEDIATE` already makes a second migrator wait."""
        return self.transaction()

    def close(self) -> None:
        """Close every thread's connection; call it once no transaction is running."""
        with self._guard:
            opened, self._open = self._open, []
        for connection in opened:
            connection.close()
        self._local = threading.local()
