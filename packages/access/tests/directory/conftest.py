"""Directory fixtures: every directory test runs on the SQLite store and on the PostgreSQL store
(`tests/support/access_directory.py`)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from orvex.access.directory import (
    Bootstrap,
    Directory,
    DirectoryEvent,
    PostgresStore,
    SqliteStore,
    migrate,
    read_settings,
)

from tests.support.access_directory import Backend, World, postgres_connection, superuser


@pytest.fixture(params=["sqlite", "postgres"])
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Backend]:
    if request.param == "sqlite":
        path = tmp_path / "access.sqlite"
        made = Backend("sqlite", lambda: SqliteStore(path))
        yield made
        for store in made.opened:
            store.close()
        return
    connection, password = postgres_connection()
    schema = f"t_{uuid.uuid4().hex[:16]}"
    settings = read_settings(connection, password, schema=schema)
    made = Backend("postgres", lambda: PostgresStore(settings), schema)
    yield made
    for store in made.opened:
        store.close()
    admin = superuser(connection, password)
    try:
        admin.run(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    finally:
        admin.close()


@pytest.fixture
def world(backend: Backend, tmp_path: Path) -> World:
    store = backend.store()
    events: list[DirectoryEvent] = []
    recovery = tmp_path / "access-recovery.key"
    migrate(store)
    first = Bootstrap(store, recovery, events.append).create_administrator("root", "Root")
    events.clear()
    return World(backend, store, Directory(store, events.append), events, first.id, recovery)
