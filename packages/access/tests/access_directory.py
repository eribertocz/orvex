"""The access module's test helpers (#436, #437): the two stores under test and a seeded world.

The PostgreSQL store needs `ACCESS_TEST_POSTGRES` (a superuser's connection string, without
password) and `ACCESS_TEST_POSTGRES_PASSWORD`. In CI both are required; elsewhere the PostgreSQL
variant is skipped when they are missing. The superuser only makes, for each test, an ordinary
role (the store refuses a superuser) and drops it, with all it owns, afterwards.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pg8000.native
import pytest
from orvex.access.audit import AuditLog
from orvex.access.directory import Directory
from orvex.access.storage import PostgresStore, SqliteStore

type AnyStore = SqliteStore | PostgresStore


def postgres_connection() -> tuple[str, str]:
    connection = os.environ.get("ACCESS_TEST_POSTGRES", "")
    password = os.environ.get("ACCESS_TEST_POSTGRES_PASSWORD", "")
    if not connection or not password:
        if os.environ.get("CI"):
            pytest.fail("ACCESS_TEST_POSTGRES and ACCESS_TEST_POSTGRES_PASSWORD are required in CI")
        pytest.skip("no PostgreSQL configured for the access store tests")
    return connection, password


@contextmanager
def access_role(connection: str, password: str) -> Iterator[tuple[str, str]]:
    """An ordinary role for one test, as the store requires (it refuses a superuser): it may
    create schemas in the database, and everything it owns is dropped with it afterwards. Yields
    its connection string and password."""
    role, secret = f"access_t_{uuid.uuid4().hex[:12]}", secrets.token_hex(16)
    keywords = dict(pair.split("=", 1) for pair in connection.split())
    admin = superuser(connection, password)
    try:
        admin.run(f"CREATE ROLE {role} LOGIN PASSWORD '{secret}'")
        admin.run(f"GRANT CREATE ON DATABASE {keywords['dbname']} TO {role}")
        yield (
            " ".join(f"{k}={v}" for k, v in keywords.items() if k != "user") + f" user={role}",
            secret,
        )
    finally:
        admin.run(f"DROP OWNED BY {role} CASCADE")
        admin.run(f"DROP ROLE {role}")
        admin.close()


def superuser(connection: str, password: str) -> pg8000.native.Connection:
    """A direct connection, for what the store itself must never do: drop a test's schema,
    plant a bad row, create another role."""
    keywords = dict(pair.split("=", 1) for pair in connection.split())
    return pg8000.native.Connection(
        keywords["user"],
        password=password,
        host=keywords["host"],
        port=int(keywords.get("port", "5432")),
        database=keywords["dbname"],
    )


@dataclass
class Backend:
    """A store under test, and how to make another store on the same database."""

    kind: str
    make: Callable[[], AnyStore]
    schema: str | None = None
    opened: list[AnyStore] = field(default_factory=list)

    def store(self) -> AnyStore:
        made = self.make()
        self.opened.append(made)
        return made


def audit_events(store: AnyStore) -> list[dict[str, Any]]:
    """Every audit event of the store, in order."""
    with store.transaction(read_only=True) as transaction:
        rows = transaction.rows("SELECT document FROM audit_events ORDER BY seq")
    return [json.loads(str(document)) for (document,) in rows]


@dataclass
class World:
    """A migrated store with its audit log and one administrator, and the directory over it."""

    backend: Backend
    store: AnyStore
    audit: AuditLog
    directory: Directory
    admin: str
    recovery: Path
    baseline: int = 0

    def user(self, name: str) -> str:
        return self.directory.create_user(self.admin, name, name.title()).id

    def project_with_builder(self, project: str, builder: str) -> None:
        self.directory.add_membership(self.admin, project, builder, "builder")

    def events(self) -> list[dict[str, Any]]:
        return audit_events(self.store)

    def refusals(self) -> list[str]:
        """The codes of the refusals recorded since the world was made."""
        return [
            str(event["details"]["code"])
            for event in self.events()[self.baseline :]
            if event["kind"] == "access.refused"
        ]


class Raw:
    """A direct connection to the store under test, bypassing the module: what an attacker with
    database access, or a test planting a bad row, does."""

    def __init__(self, backend: Backend, world: World) -> None:
        self.kind = backend.kind
        if backend.kind == "sqlite":
            assert isinstance(world.store, SqliteStore)
            self._sqlite = sqlite3.connect(world.store.path, isolation_level=None)
            self._sqlite.execute("PRAGMA foreign_keys = ON")
        else:
            self._postgres = superuser(*postgres_connection())
            self._postgres.run(f"SET search_path TO {backend.schema}")

    def run(self, sql: str, /, **parameters: object) -> None:
        if self.kind == "sqlite":
            self._sqlite.execute(sql, parameters)
        else:
            self._postgres.run(sql, **parameters)

    def rows(self, sql: str, /, **parameters: object) -> list[tuple[object, ...]]:
        if self.kind == "sqlite":
            return [tuple(row) for row in self._sqlite.execute(sql, parameters).fetchall()]
        return [tuple(row) for row in self._postgres.run(sql, **parameters) or []]

    def close(self) -> None:
        if self.kind == "sqlite":
            self._sqlite.close()
        else:
            self._postgres.close()
