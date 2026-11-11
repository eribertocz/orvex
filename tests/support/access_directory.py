"""The access directory's test helpers (#436): the two stores under test and a seeded world.

The PostgreSQL store needs `ACCESS_TEST_POSTGRES` (a connection string without password) and
`ACCESS_TEST_POSTGRES_PASSWORD`. In CI both are required; elsewhere the PostgreSQL variant is
skipped when they are missing. Each test gets a schema of its own, dropped afterwards.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pg8000.native
import pytest
from orvex.access.directory import (
    Directory,
    DirectoryEvent,
    PostgresStore,
    SqliteStore,
)

type AnyStore = SqliteStore | PostgresStore


def postgres_connection() -> tuple[str, str]:
    connection = os.environ.get("ACCESS_TEST_POSTGRES", "")
    password = os.environ.get("ACCESS_TEST_POSTGRES_PASSWORD", "")
    if not connection or not password:
        if os.environ.get("CI"):
            pytest.fail("ACCESS_TEST_POSTGRES and ACCESS_TEST_POSTGRES_PASSWORD are required in CI")
        pytest.skip("no PostgreSQL configured for the access store tests")
    return connection, password


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


@dataclass
class World:
    """A migrated store with one administrator, and the directory over it."""

    backend: Backend
    store: AnyStore
    directory: Directory
    events: list[DirectoryEvent]
    admin: str
    recovery: Path

    def user(self, name: str) -> str:
        return self.directory.create_user(self.admin, name, name.title()).id

    def project_with_builder(self, project: str, builder: str) -> None:
        self.directory.add_membership(self.admin, project, builder, "builder")

    def refusals(self) -> list[str | None]:
        return [event.code for event in self.events if event.outcome == "refused"]
