"""The seam between the directory and its database: one transaction at a time, SQLite or
PostgreSQL (application port).

Statements use `:name` parameters, which both drivers accept. What differs between the two
databases stays in their adapters:
- `locking` is the clause a read appends to lock the rows it returns until the transaction
  ends: `FOR UPDATE` on PostgreSQL, nothing on SQLite, whose `BEGIN IMMEDIATE` already holds the
  only write lock;
- the schema and its migrations, how the stored version is read, and how two migrators are kept
  apart (`migration_transaction`).

A read-only transaction takes no write lock. A transaction commits when its block ends and
rolls back when the block raises. The adapters turn their drivers' errors into the exceptions
below, so the directory never names a driver.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from typing import Protocol

type Row = tuple[object, ...]


class UniqueViolation(Exception):
    """A row would break a uniqueness constraint (another writer may have won the race)."""


class UnreadableValue(Exception):
    """A stored value cannot be decoded as text (planted bytes the database itself let in)."""


class TransientConflict(Exception):
    """The database gave up on the transaction (a deadlock, a busy database): run it again."""


class Transaction(Protocol):
    locking: str

    def rows(self, sql: str, /, **parameters: object) -> list[Row]: ...

    def run(self, sql: str, /, **parameters: object) -> None: ...


class Store(Protocol):
    # Each migration is a list of statements; migration N brings the store to version N.
    migrations: Sequence[Sequence[str]]

    def transaction(self, *, read_only: bool = False) -> AbstractContextManager[Transaction]:
        """A write transaction, or a read-only one that sees one consistent state."""
        ...

    def stored_version(self, transaction: Transaction) -> int:
        """The version the store is at; 0 for an empty store."""
        ...

    def remove_audit_through(self, transaction: Transaction, seq: int) -> None:
        """Remove the audit events up to `seq`: the one removal the log's tables allow."""
        ...

    def migration_transaction(self) -> AbstractContextManager[Transaction]:
        """A write transaction that a second migrator of the same store waits for before it
        reads anything, so it sees the first one's version."""
        ...
