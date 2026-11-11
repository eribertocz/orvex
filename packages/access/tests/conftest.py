"""Store fixtures for the access tests: every directory and audit test runs on the SQLite store
and on the PostgreSQL store (`tests/support/access_directory.py`)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from orvex.access.audit import open_audit_log
from orvex.access.directory import Bootstrap, Directory
from orvex.access.storage import PostgresStore, SqliteStore, read_settings

from tests.support.access_directory import Backend, World, access_role, postgres_connection


@pytest.fixture(params=["sqlite", "postgres"])
def backend(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Backend]:
    if request.param == "sqlite":
        path = tmp_path / "access.sqlite"
        made = Backend("sqlite", lambda: SqliteStore(path))
        yield made
        for store in made.opened:
            store.close()
        return
    with access_role(*postgres_connection()) as (connection, password):
        schema = f"t_{uuid.uuid4().hex[:16]}"
        settings = read_settings(connection, password, schema=schema)
        made = Backend("postgres", lambda: PostgresStore(settings), schema)
        yield made
        for store in made.opened:
            store.close()


@pytest.fixture
def world(backend: Backend, tmp_path: Path) -> World:
    store = backend.store()
    recovery = tmp_path / "access-recovery.key"
    audit = open_audit_log(store, tmp_path / "audit-signing.key", tmp_path / "audit-anchors.log")
    first = Bootstrap(store, recovery, audit).create_administrator("root", "Root")
    made = World(backend, store, audit, Directory(store, audit), first.id, recovery)
    made.baseline = len(made.events())
    return made
