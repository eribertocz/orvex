"""#436 AC9 and R4: the database itself refuses bad rows; migrations are forward-only and
all-or-nothing; a store that is not the module's own, or newer than the code, is refused."""

from __future__ import annotations

import sqlite3
import stat
import threading
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pg8000.native
import pytest
from orvex.access.directory import PostgresStore, SqliteStore, migrate, read_settings
from orvex.access.directory.postgres_store import migrations_for
from orvex.access.directory.sqlite_store import MIGRATIONS
from orvex.access.errors import ACCESS_STORE_NEWER, ACCESS_STORE_UNSAFE, AccessError
from pg8000.exceptions import DatabaseError

from tests.support.access_directory import (
    AnyStore,
    Backend,
    World,
    postgres_connection,
    superuser,
)

NOW = "'2026-11-10T12:00:00.000000Z'"
USER = (
    "INSERT INTO users VALUES ({id}, {name}, 'Someone', {state}, {admin}, {version}, {now}, {now})"
)


def user_row(
    id: str = "'u2'",
    name: str = "'ben'",
    state: str = "'active'",
    admin: str = "FALSE",
    version: str = "1",
) -> str:
    return USER.format(id=id, name=name, state=state, admin=admin, version=version, now=NOW)


# Each planted row breaks one constraint of #436 item 7; `world` has created user `u1` (ana) with
# a membership in p1, and group `g` in p1.
PLANTED = {
    "primary key of users": user_row(id="'u1'"),
    "unique user name, any case": user_row(name="'ANA'"),
    "state is active or deactivated": user_row(state="'banned'"),
    "display name not null": USER.replace("'Someone'", "NULL").format(
        id="'u3'", name="'cid'", state="'active'", admin="FALSE", version="1", now=NOW
    ),
    "id shape": user_row(id="'a b'"),
    "user name is ASCII": user_row(name="'b\u0435n'"),
    "authz_version at least 1": user_row(version="0"),
    "instants are UTC with microseconds": USER.format(
        id="'u4'", name="'dee'", state="'active'", admin="FALSE", version="1", now="'yesterday'"
    ),
    "role is builder, author or viewer": "INSERT INTO memberships VALUES ('p1', 'u1', 'owner')",
    "membership names a user": "INSERT INTO memberships VALUES ('p1', 'nobody', 'viewer')",
    "one membership per project and user": "INSERT INTO memberships VALUES ('p1', 'u1', 'viewer')",
    "a group member is a project member": "INSERT INTO group_members VALUES ('p1', 'g', 'u9')",
    "a group member's group exists": "INSERT INTO group_members VALUES ('p1', 'none', 'u1')",
    "one metadata row": "INSERT INTO store_meta (id, schema_version) VALUES (2, 1)",
}


class Raw:
    """A direct connection to the store under test, bypassing the directory."""

    def __init__(self, backend: Backend, world: World) -> None:
        self.kind = backend.kind
        if backend.kind == "sqlite":
            assert isinstance(world.store, SqliteStore)
            self._sqlite = sqlite3.connect(world.store.path, isolation_level=None)
            self._sqlite.execute("PRAGMA foreign_keys = ON")
        else:
            self._postgres = superuser(*postgres_connection())
            self._postgres.run(f"SET search_path TO {backend.schema}")

    def run(self, sql: str) -> None:
        if self.kind == "sqlite":
            self._sqlite.execute(sql)
        else:
            self._postgres.run(sql)

    def close(self) -> None:
        if self.kind == "sqlite":
            self._sqlite.close()
        else:
            self._postgres.close()


@pytest.fixture
def raw(backend: Backend, world: World) -> Iterator[Raw]:
    made = Raw(backend, world)
    boolean = "0" if backend.kind == "sqlite" else "FALSE"
    made.run(user_row(id="'u1'", name="'ana'").replace("FALSE", boolean))
    made.run("INSERT INTO memberships VALUES ('p1', 'u1', 'viewer')")
    made.run("INSERT INTO project_groups VALUES ('p1', 'g')")
    yield made
    made.close()


@pytest.mark.parametrize("constraint", sorted(PLANTED))
def test_the_database_refuses_a_planted_bad_row(raw: Raw, constraint: str) -> None:
    statement = PLANTED[constraint]
    if raw.kind == "sqlite":
        statement = statement.replace("FALSE", "0")
    with pytest.raises((sqlite3.IntegrityError, DatabaseError)):
        raw.run(statement)


def test_sqlite_refuses_a_value_of_the_wrong_type(raw: Raw) -> None:
    if raw.kind != "sqlite":
        pytest.skip("PostgreSQL types every column")
    with pytest.raises(sqlite3.IntegrityError):
        raw.run(user_row(id="'u5'", name="'eve'", admin="0", version="'one'"))


def failing_migrations(backend: Backend) -> tuple[tuple[str, ...], ...]:
    current = MIGRATIONS if backend.kind == "sqlite" else migrations_for(str(backend.schema))
    return (*current, ("CREATE TABLE half_done (x INTEGER)", "SELECT * FROM no_such_table"))


def store_with(backend: Backend, migrations: tuple[tuple[str, ...], ...]) -> AnyStore:
    """Another store on the same database, with these migrations; closed with the test's."""
    template = backend.store()
    made: AnyStore = (
        SqliteStore(template.path, migrations)
        if isinstance(template, SqliteStore)
        else PostgresStore(template.settings, migrations)
    )
    backend.opened.append(made)
    return made


def test_a_migration_that_fails_halfway_leaves_the_version_unchanged(
    backend: Backend, world: World
) -> None:
    broken = store_with(backend, failing_migrations(backend))
    with pytest.raises((sqlite3.OperationalError, DatabaseError)):
        migrate(broken)
    with world.store.transaction(read_only=True) as transaction:
        assert world.store.stored_version(transaction) == 1
        present = transaction.rows(
            "SELECT count(*) FROM sqlite_schema WHERE name = 'half_done'"
            if backend.kind == "sqlite"
            else "SELECT count(*) FROM pg_tables WHERE tablename = 'half_done'"
        )
        assert present == [(0,)]


def test_r4_a_store_newer_than_the_code_is_refused_not_migrated_down(
    backend: Backend, world: World
) -> None:
    current = MIGRATIONS if backend.kind == "sqlite" else migrations_for(str(backend.schema))
    newer = store_with(backend, (*current, ("CREATE TABLE later (x INTEGER)",)))
    assert migrate(newer) == 2
    with pytest.raises(AccessError) as refused:
        migrate(store_with(backend, current))
    assert refused.value.code == ACCESS_STORE_NEWER
    with world.store.transaction(read_only=True) as transaction:
        assert world.store.stored_version(transaction) == 2


def test_migrating_twice_changes_nothing(backend: Backend, world: World) -> None:
    assert migrate(world.store) == 1
    assert migrate(world.store) == 1


def test_the_sqlite_file_is_owner_only(backend: Backend, world: World) -> None:
    if backend.kind != "sqlite":
        pytest.skip("a SQLite property")
    assert isinstance(world.store, SqliteStore)
    assert stat.S_IMODE(world.store.path.stat().st_mode) == 0o600


def test_a_postgres_schema_owned_by_another_role_is_refused() -> None:
    connection, password = postgres_connection()
    admin = superuser(connection, password)
    role, schema = f"other_{uuid.uuid4().hex[:8]}", f"t_{uuid.uuid4().hex[:16]}"
    admin.run(f"CREATE ROLE {role}")
    admin.run(f"CREATE SCHEMA {schema} AUTHORIZATION {role}")
    try:
        store = PostgresStore(read_settings(connection, password, schema=schema))
        with pytest.raises(AccessError) as refused:
            migrate(store)
        assert refused.value.code == ACCESS_STORE_UNSAFE
        store.close()
    finally:
        admin.run(f"DROP SCHEMA {schema} CASCADE")
        admin.run(f"DROP ROLE {role}")
        admin.close()


def test_the_postgres_schema_is_owned_by_the_store_and_closed_to_public(
    backend: Backend, world: World
) -> None:
    if backend.kind != "postgres":
        pytest.skip("a PostgreSQL property")
    connection, password = postgres_connection()
    admin: pg8000.native.Connection = superuser(connection, password)
    try:
        rows = admin.run(
            "SELECT pg_get_userbyid(nspowner), has_schema_privilege('public', nspname, 'USAGE') "
            "FROM pg_namespace WHERE nspname = :schema",
            schema=backend.schema,
        )
        assert rows == [[connection.split("user=")[1].split()[0], False]]
    finally:
        admin.close()


def test_two_first_migrations_at_once_both_succeed(backend: Backend) -> None:
    """The second migrator waits for the first and then sees its version: neither fails."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        for _ in range(10):
            store = backend.store()
            start = threading.Barrier(2)

            def migrator(store: AnyStore = store, start: threading.Barrier = start) -> int:
                start.wait(timeout=30)
                return migrate(store)

            futures = [pool.submit(migrator) for _ in range(2)]
            assert [future.result(timeout=60) for future in futures] == [1, 1]
            store.close()
            fresh(backend, store)


def fresh(backend: Backend, store: AnyStore) -> None:
    """Remove the store, so the next round migrates an empty one again."""
    if isinstance(store, SqliteStore):
        for suffix in ("", "-wal", "-shm"):
            Path(f"{store.path}{suffix}").unlink(missing_ok=True)
        return
    admin = superuser(*postgres_connection())
    try:
        admin.run(f"DROP SCHEMA {backend.schema} CASCADE")
    finally:
        admin.close()


def test_a_sqlite_file_others_can_read_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "access.sqlite"
    path.touch(mode=0o644)
    path.chmod(0o644)
    with pytest.raises(AccessError) as refused:
        migrate(SqliteStore(path))
    assert refused.value.code == ACCESS_STORE_UNSAFE


def test_a_sqlite_file_that_is_a_link_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.sqlite"
    target.touch(mode=0o600)
    (tmp_path / "access.sqlite").symlink_to(target)
    with pytest.raises(AccessError) as refused:
        migrate(SqliteStore(tmp_path / "access.sqlite"))
    assert refused.value.code == ACCESS_STORE_UNSAFE
