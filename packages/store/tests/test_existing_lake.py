"""An existing DuckLake attached read-only and isolated (P2-30 PR 1, #399; ADR-0027 §3).

SQLite catalogs run everywhere. PostgreSQL catalogs need `STORE_TEST_POSTGRES` and
`STORE_TEST_POSTGRES_PASSWORD` (a superuser that creates a database and roles per test); they
are required in CI and skipped elsewhere when missing.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest
from orvex.contracts.store.v1 import (
    ExistingLakeAttachment,
    PostgresLakeLocation,
    ProtectedFile,
    ProtectedLocation,
    RolesInUseItem,
    SqliteLakeLocation,
)
from orvex.store import existing_lake
from orvex.store.errors import StoreError
from orvex.store.existing_lake import ExistingLake

MEMORY = "512MB"
PASSWORD_ENV = "STORE_TEST_POSTGRES_PASSWORD"

# The owner's writer, a process of its own as another system's would be.
OWNER = """
import sys, duckdb
catalog, data, step = sys.argv[1], sys.argv[2], sys.argv[3]
c = duckdb.connect()
for e in ("ducklake", "sqlite"):
    c.install_extension(e)
    c.load_extension(e)
options = f"DATA_PATH '{data}', DATA_INLINING_ROW_LIMIT 100"
c.execute(f"ATTACH 'ducklake:sqlite:{catalog}' AS l ({options})")
if step == "create":
    c.execute("CREATE TABLE l.readings (reading_id BIGINT, kwh DECIMAL(18, 4))")
else:
    c.execute(f"INSERT INTO l.readings VALUES ({int(step)}, 1.5), ({int(step) + 1}, 2.25)")
"""


def owner_writes(catalog: Path, data: Path, step: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", OWNER, str(catalog), data.as_posix() + "/", step],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def make_sqlite_lake(tmp_path: Path, *, wal: bool = True) -> tuple[Path, Path]:
    catalog, data = tmp_path / "owner" / "catalog.sqlite", tmp_path / "owner" / "data"
    data.mkdir(parents=True)
    assert owner_writes(catalog, data, "create").returncode == 0
    if wal:
        connection = sqlite3.connect(catalog)
        assert connection.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
        connection.close()
    assert owner_writes(catalog, data, "10").returncode == 0
    return catalog, data


def sqlite_attachment(
    catalog: Path, data: Path, protected: list[str] | None = None, **location: object
) -> ExistingLakeAttachment:
    return ExistingLakeAttachment(
        kind="existing_lake_attachment",
        name="billing",
        location=SqliteLakeLocation(
            kind="lake_location",
            catalog="sqlite",
            path=str(catalog),
            data_path=data.as_posix() + "/",
            **location,  # type: ignore[arg-type]
        ),
        roles_in_use=[],
        protected_files=[ProtectedFile(path) for path in protected or []],
        protected_locations=[ProtectedLocation(path) for path in protected or []],
    )


def refused(attachment: ExistingLakeAttachment) -> StoreError:
    with pytest.raises(StoreError) as caught:
        ExistingLake(attachment, memory_limit=MEMORY, threads=1).close()
    return caught.value


@pytest.fixture
def sqlite_lake(tmp_path: Path) -> tuple[Path, Path]:
    return make_sqlite_lake(tmp_path)


# --- SQLite: accepted, and new snapshots under the sandbox -----------------------------------


def test_a_wal_catalog_shows_new_snapshots_with_inlined_rows_under_the_locked_sandbox(
    sqlite_lake: tuple[Path, Path],
) -> None:
    # The measurement ADR-0027 §3 asks for: inlined rows never pass through the file sandbox,
    # yet each commit of the owner is seen, and the owner's commits still succeed.
    catalog, data = sqlite_lake
    with ExistingLake(sqlite_attachment(catalog, data), memory_limit=MEMORY, threads=1) as lake:
        connection = lake._connection
        first = lake.latest_snapshot()
        assert connection.execute("SELECT count(*) FROM lake.readings").fetchone() == (2,)
        for step in ("20", "30"):
            written = owner_writes(catalog, data, step)
            assert written.returncode == 0, written.stderr[-400:]
        assert lake.latest_snapshot().id == first.id + 2
        assert connection.execute(
            "SELECT list(reading_id ORDER BY reading_id) FROM lake.readings"
        ).fetchone() == ([10, 11, 20, 21, 30, 31],)
        # Every row is inlined in the catalog: no data file was written.
        assert not list(data.rglob("*.parquet"))


def test_a_rollback_journal_reader_blocks_the_owner_so_such_a_catalog_is_refused(
    tmp_path: Path,
) -> None:
    catalog, data = make_sqlite_lake(tmp_path, wal=False)
    # Pinned DuckDB 2.0.0 behavior: after one read, the reader's lock fails the owner's commit.
    reader = duckdb.connect()
    for extension in ("ducklake", "sqlite"):
        reader.install_extension(extension)
        reader.load_extension(extension)
    reader.execute(f"ATTACH 'ducklake:sqlite:{catalog}' AS l (READ_ONLY)")
    reader.execute("SELECT count(*) FROM l.readings").fetchone()
    blocked = owner_writes(catalog, data, "20")
    reader.close()
    assert blocked.returncode != 0
    assert "locked" in blocked.stderr
    error = refused(sqlite_attachment(catalog, data))
    assert error.code == "STORE_SOURCE_INVALID"
    assert "WAL" in error.message


def test_sqlite_opens_its_catalog_outside_duckdbs_allow_list(
    sqlite_lake: tuple[Path, Path], tmp_path: Path
) -> None:
    # Pinned DuckDB 2.0.0 behavior, why the descriptor checks guard a SQLite catalog: with the
    # catalog left out of the allow-list, a locked instance still reads it.
    catalog, data = sqlite_lake
    reader = duckdb.connect()
    for extension in ("ducklake", "sqlite"):
        reader.install_extension(extension)
        reader.load_extension(extension)
    reader.execute(f"ATTACH 'ducklake:sqlite:{catalog}' AS l (READ_ONLY)")
    reader.execute(f"SET allowed_directories = ['{data}/']")
    reader.execute("SET enable_external_access = false")
    reader.execute("SET lock_configuration = true")
    assert owner_writes(catalog, data, "20").returncode == 0
    assert reader.execute("SELECT count(*) FROM l.readings").fetchone() == (4,)
    reader.close()


# --- SQLite: links ---------------------------------------------------------------------------


def test_a_catalog_that_is_a_symbolic_link_is_refused(
    sqlite_lake: tuple[Path, Path], tmp_path: Path
) -> None:
    catalog, data = sqlite_lake
    link = tmp_path / "link.sqlite"
    link.symlink_to(catalog)
    error = refused(sqlite_attachment(link, data))
    assert error.code == "STORE_SOURCE_INVALID"
    assert "symbolic link" in error.message


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_a_companion_file_that_is_a_symbolic_link_is_refused(
    sqlite_lake: tuple[Path, Path], tmp_path: Path, suffix: str
) -> None:
    catalog, data = sqlite_lake
    companion = Path(f"{catalog}{suffix}")
    companion.unlink(missing_ok=True)
    target = tmp_path / "elsewhere"
    target.write_bytes(b"")
    companion.symlink_to(target)
    assert refused(sqlite_attachment(catalog, data)).code == "STORE_SOURCE_INVALID"


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
@pytest.mark.parametrize("protected_suffix", ["", "-wal", ".wal"])
def test_a_catalog_file_that_is_a_hard_link_to_a_protected_file_is_refused(
    sqlite_lake: tuple[Path, Path], tmp_path: Path, suffix: str, protected_suffix: str
) -> None:
    catalog, data = sqlite_lake
    protected = tmp_path / "managed" / "catalog.ducklake"
    protected.parent.mkdir()
    victim = Path(f"{protected}{protected_suffix}")
    if suffix:
        victim.write_bytes(b"protected")
        companion = Path(f"{catalog}{suffix}")
        companion.unlink(missing_ok=True)
        os.link(victim, companion)
    else:
        # The catalog itself is the protected file under another name.
        victim.unlink(missing_ok=True)
        os.link(catalog, victim)
    protected.touch(exist_ok=True)
    error = refused(sqlite_attachment(catalog, data, protected=[str(protected)]))
    assert error.code == "STORE_SOURCE_INVALID"
    assert "hard link" in error.message


def test_a_catalog_whose_companion_is_a_link_to_itself_is_refused(
    sqlite_lake: tuple[Path, Path],
) -> None:
    catalog, data = sqlite_lake
    journal = Path(f"{catalog}-journal")
    journal.unlink(missing_ok=True)
    os.link(catalog, journal)
    assert refused(sqlite_attachment(catalog, data)).code == "STORE_SOURCE_INVALID"


def test_an_unrelated_protected_file_does_not_refuse_the_catalog(
    sqlite_lake: tuple[Path, Path], other_device: Path
) -> None:
    # Unrelated: another file, on another file system (PR 2: a file system that holds a
    # protected file is refused, `test_existing_lake_files`).
    catalog, data = sqlite_lake
    other = other_device / "managed.sqlite"
    other.write_bytes(b"x")
    ExistingLake(
        sqlite_attachment(catalog, data, protected=[str(other)]), memory_limit=MEMORY, threads=1
    ).close()


# --- versions, data path, owner settings -----------------------------------------------------


@pytest.mark.parametrize("version", ["1.0", "0.3", "9.9"])
def test_a_catalog_version_that_needs_migration_or_is_untested_is_refused(
    sqlite_lake: tuple[Path, Path], version: str
) -> None:
    catalog, data = sqlite_lake
    connection = sqlite3.connect(catalog)
    connection.execute("UPDATE ducklake_metadata SET value = ? WHERE key = 'version'", (version,))
    connection.commit()
    connection.close()
    error = refused(sqlite_attachment(catalog, data))
    assert error.code == "ENGINE_VERSION_UNTESTED"
    assert error.details["version"] == version


def test_a_data_path_other_than_the_catalogs_own_is_refused(
    sqlite_lake: tuple[Path, Path], tmp_path: Path
) -> None:
    catalog, _ = sqlite_lake
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert refused(sqlite_attachment(catalog, elsewhere)).code == "STORE_SOURCE_INVALID"


def test_encryption_is_the_owners_setting(sqlite_lake: tuple[Path, Path]) -> None:
    catalog, data = sqlite_lake
    error = refused(sqlite_attachment(catalog, data, encrypted=True))
    assert error.code == "STORE_LOCATION_INVALID"


def test_a_file_that_is_not_a_sqlite_database_is_refused(tmp_path: Path) -> None:
    catalog, data = tmp_path / "catalog.sqlite", tmp_path / "data"
    catalog.write_text("not a database")
    data.mkdir()
    assert refused(sqlite_attachment(catalog, data)).code == "STORE_SOURCE_INVALID"


# --- the sandbox and the lock ----------------------------------------------------------------

SECURITY_SETTINGS = [
    "SET enable_external_access = true",
    "SET allowed_directories = ['/']",
    "SET allowed_paths = ['/etc/passwd']",
    "SET allowed_configs = ['enable_external_access']",
    "SET lock_configuration = false",
    "RESET lock_configuration",
    "SET autoload_known_extensions = true",
    "SET autoinstall_known_extensions = true",
    "SET allow_community_extensions = true",
    "SET allow_unsigned_extensions = true",
    "SET allow_persistent_secrets = true",
    "SET temp_directory = '/tmp'",
    "SET disabled_filesystems = ''",
    "SET python_enable_replacements = true",
    "SET TimeZone = 'America/La_Paz'",
    "SET GLOBAL TimeZone = 'America/La_Paz'",
    "SET memory_limit = '64GB'",
    "SET threads = 64",
]


def test_after_the_lock_no_security_setting_changes(sqlite_lake: tuple[Path, Path]) -> None:
    catalog, data = sqlite_lake
    with ExistingLake(sqlite_attachment(catalog, data), memory_limit=MEMORY, threads=1) as lake:
        for statement in SECURITY_SETTINGS:
            for connection in (lake._connection, lake._connection.cursor()):
                with pytest.raises(duckdb.Error, match="configuration"):
                    connection.execute(statement)


def test_the_instance_is_locked_with_the_lake_alone_in_utc(sqlite_lake: tuple[Path, Path]) -> None:
    catalog, data = sqlite_lake
    with ExistingLake(sqlite_attachment(catalog, data), memory_limit=MEMORY, threads=1) as lake:
        cursor = lake._connection.cursor()
        settings = dict(
            cursor.execute(
                "SELECT name, value FROM duckdb_settings() WHERE name IN ('TimeZone', "
                "'enable_external_access', 'lock_configuration', 'allowed_configs', "
                "'python_enable_replacements')"
            ).fetchall()
        )
        assert settings == {
            "TimeZone": "UTC",
            "enable_external_access": "false",
            "lock_configuration": "true",
            "allowed_configs": "[]",
            "python_enable_replacements": "false",
        }
        databases = {row[0] for row in cursor.execute("FROM duckdb_databases()").fetchall()}
        assert databases == {"lake", "memory", "system", "temp"}
        assert cursor.execute("SELECT current_database()").fetchone() == ("memory",)
        assert cursor.execute("SELECT count(*) FROM duckdb_secrets()").fetchone() == (0,)


def test_reading_a_file_outside_the_allow_list_fails(
    sqlite_lake: tuple[Path, Path], tmp_path: Path
) -> None:
    catalog, data = sqlite_lake
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.csv").write_text("x\n42\n")
    (data / "inside.csv").write_text("x\n7\n")
    (data / "link_in.csv").symlink_to(data / "inside.csv")
    (data / "link_out.csv").symlink_to(outside / "secret.csv")
    (data / "folder_out").symlink_to(outside)
    with ExistingLake(sqlite_attachment(catalog, data), memory_limit=MEMORY, threads=1) as lake:
        connection = lake._connection
        read = "SELECT * FROM read_csv({})"
        assert connection.execute(read.format(f"'{data}/inside.csv'")).fetchall() == [(7,)]
        # DuckDB 2.0.0 resolves a symbolic link before it checks the allow-list: a link that
        # stays inside is read, a link out of it is refused (measured for #399).
        assert connection.execute(read.format(f"'{data}/link_in.csv'")).fetchall() == [(7,)]
        for path in (
            outside / "secret.csv",
            data / "link_out.csv",
            data / "folder_out" / "secret.csv",
            Path(f"{data}/../../outside/secret.csv"),
            catalog.parent / "elsewhere.csv",
        ):
            with pytest.raises(duckdb.PermissionException):
                connection.execute(read.format(f"'{path}'")).fetchall()


# --- PostgreSQL catalogs ---------------------------------------------------------------------


def postgres_settings() -> dict[str, str]:
    connection = os.environ.get("STORE_TEST_POSTGRES")
    if not connection or not os.environ.get(PASSWORD_ENV):
        if os.environ.get("CI"):
            pytest.fail("STORE_TEST_POSTGRES and STORE_TEST_POSTGRES_PASSWORD are required in CI")
        pytest.skip("no PostgreSQL configured for this run")
    return dict(pair.split("=", 1) for pair in connection.split())


class Catalog:
    """A database of its own with a DuckLake catalog in schema `cat`, written by an owner role,
    and a reader role that may read only that schema, as a careful DBA sets them up."""

    def __init__(self, tmp_path: Path) -> None:
        self.settings = postgres_settings()
        suffix = uuid.uuid4().hex[:12]
        self.database, self.owner, self.reader = f"xl_{suffix}", f"own_{suffix}", f"rd_{suffix}"
        self.password = os.environ[PASSWORD_ENV]
        self.data = tmp_path / "data"
        self.data.mkdir()
        self.admin = self._connect(self.settings["dbname"], self.settings["user"], "admin")
        self.execute_on("admin", f"CREATE DATABASE {self.database}")
        quoted = self.password.replace("'", "''")
        for role in (self.owner, self.reader):
            self.execute_on("admin", f"CREATE ROLE {role} LOGIN PASSWORD '{quoted}'")
        self.admin.execute(
            f"ATTACH '{self._target(self.database, self.settings['user'])}' AS db (TYPE postgres)"
        )
        self.execute(
            "REVOKE ALL ON SCHEMA public FROM PUBLIC",
            f"CREATE SCHEMA cat AUTHORIZATION {self.owner}",
            f"GRANT USAGE ON SCHEMA cat TO {self.reader}",
            "CREATE SCHEMA other",
            "CREATE TABLE other.secrets (value text)",
            "INSERT INTO other.secrets VALUES ('do not read')",
        )
        writer = duckdb.connect()
        for extension in ("ducklake", "postgres"):
            writer.install_extension(extension)
            writer.load_extension(extension)
        writer.execute(f"CREATE TEMPORARY SECRET (TYPE postgres, PASSWORD '{quoted}')")
        writer.execute(
            f"ATTACH 'ducklake:postgres:{self._target(self.database, self.owner)}' AS l "
            f"(DATA_PATH '{self.data.as_posix()}/', METADATA_SCHEMA 'cat')"
        )
        writer.execute("CREATE TABLE l.readings (reading_id BIGINT, kwh DECIMAL(18, 4))")
        writer.execute("INSERT INTO l.readings VALUES (1, 1.5), (2, 2.25)")
        writer.close()
        self.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA cat TO {self.reader}")

    def _target(self, database: str, user: str) -> str:
        pairs = {**self.settings, "dbname": database, "user": user}
        return " ".join(f"{key}={value}" for key, value in pairs.items())

    def _connect(self, database: str, user: str, name: str) -> duckdb.DuckDBPyConnection:
        connection = duckdb.connect()
        connection.install_extension("postgres")
        connection.load_extension("postgres")
        quoted = os.environ[PASSWORD_ENV].replace("'", "''")
        connection.execute(f"CREATE SECRET (TYPE postgres, PASSWORD '{quoted}')")
        connection.execute(f"ATTACH '{self._target(database, user)}' AS {name} (TYPE postgres)")
        return connection

    def execute_on(self, database: str, *statements: str) -> None:
        for sql in statements:
            quoted = sql.replace("'", "''")
            self.admin.execute(
                f"CALL postgres_execute('{database}', '{quoted}', use_transaction := false)"
            )

    def execute(self, *statements: str) -> None:
        self.execute_on("db", *statements)

    def attachment(self, roles_in_use: tuple[str, ...] = ()) -> ExistingLakeAttachment:
        return ExistingLakeAttachment(
            kind="existing_lake_attachment",
            name="billing",
            location=PostgresLakeLocation(
                kind="lake_location",
                catalog="postgres",
                connection=self._target(self.database, self.reader),
                password_env=PASSWORD_ENV,
                metadata_schema="cat",
                data_path=self.data.as_posix() + "/",
            ),
            roles_in_use=[RolesInUseItem(role) for role in roles_in_use],
            protected_files=[],
            protected_locations=[],
        )

    def close(self) -> None:
        self.admin.execute("DETACH db")
        self.execute_on("admin", f"DROP DATABASE {self.database} WITH (FORCE)")
        for role in (self.owner, self.reader):
            self.execute_on("admin", f"DROP ROLE {role}")
        self.admin.close()


@pytest.fixture
def catalog(tmp_path: Path) -> Iterator[Catalog]:
    made = Catalog(tmp_path)
    yield made
    made.close()


def test_a_minimal_role_attaches_the_lake_in_read_only_sessions(catalog: Catalog) -> None:
    with ExistingLake(catalog.attachment(), memory_limit=MEMORY, threads=1) as lake:
        connection = lake._connection
        assert connection.execute("SELECT sum(kwh) FROM lake.readings").fetchone()[0] == 3.75  # type: ignore[index]
        assert lake.latest_snapshot().id >= 1
        # DuckLake's own catalog session, not only the check's, is read-only.
        assert connection.execute(
            "SELECT * FROM postgres_query('__ducklake_metadata_lake', "
            "'SELECT current_setting(''transaction_read_only'')')"
        ).fetchone() == ("on",)


def test_a_writable_catalog_session_is_not_read_only(
    catalog: Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(existing_lake, "READ_ONLY_SESSION", "")
    assert refused(catalog.attachment()).code == "SOURCE_NOT_READ_ONLY"


def test_a_role_in_use_elsewhere_is_refused(catalog: Catalog) -> None:
    # `roles_in_use` lists the roles of every other connection of the instance (catalogs and
    # sources) and of every managed lake's catalog: one rule covers both cases.
    error = refused(catalog.attachment(roles_in_use=("someone", catalog.reader)))
    assert error.code == "STORE_SOURCE_INVALID"
    assert error.details["kind"] == "role in use"


@pytest.mark.parametrize(
    ("grants", "kind"),
    [
        (
            [
                "GRANT USAGE ON SCHEMA other TO {reader}",
                "GRANT SELECT ON other.secrets TO {reader}",
            ],
            "schema",
        ),
        (["GRANT SELECT ON other.secrets TO {reader}"], "table"),
        (["GRANT SELECT (value) ON other.secrets TO {reader}"], "table"),
        (["GRANT SELECT ON other.secrets TO PUBLIC"], "table"),
        (["GRANT USAGE ON SCHEMA other TO PUBLIC"], "schema"),
        (["CREATE VIEW other.v AS SELECT 1 AS x", "GRANT SELECT ON other.v TO PUBLIC"], "view"),
        (["CREATE SEQUENCE other.s", "GRANT USAGE ON SEQUENCE other.s TO {reader}"], "sequence"),
        (["CREATE FUNCTION other.f() RETURNS int LANGUAGE sql AS 'SELECT 1'"], "function"),
        (["GRANT pg_read_all_data TO {reader}"], "role pg_read_all_data"),
        (["GRANT INSERT ON cat.ducklake_snapshot TO {reader}"], "write in the catalog schema"),
        (
            ["GRANT UPDATE (snapshot_id) ON cat.ducklake_snapshot TO {reader}"],
            "write in the catalog schema",
        ),
        (["GRANT TRUNCATE ON cat.ducklake_metadata TO {reader}"], "write in the catalog schema"),
        (["GRANT CREATE ON SCHEMA cat TO {reader}"], "write in the catalog schema"),
        (["GRANT pg_read_server_files TO {reader}"], "role pg_read_server_files"),
        (
            [
                "CREATE ROLE {reader}_grp NOINHERIT",
                "GRANT SELECT ON other.secrets TO {reader}_grp",
                "GRANT {reader}_grp TO {reader}",
            ],
            "table",
        ),
        # PR 2 (Reviewer pass 1 on #405, finding 5): large objects, and the cluster's other
        # databases while an extension can open a connection to them.
        (
            ["SELECT lo_create(424242)", "ALTER LARGE OBJECT 424242 OWNER TO {reader}"],
            "large object",
        ),
        (
            ["SELECT lo_create(424243)", "GRANT SELECT ON LARGE OBJECT 424243 TO PUBLIC"],
            "large object",
        ),
        (["ALTER DATABASE {database} SET lo_compat_privileges = on"], "large object"),
        (["CREATE EXTENSION dblink SCHEMA cat"], "database"),
        (["CREATE EXTENSION postgres_fdw SCHEMA cat"], "database"),
        (["GRANT CREATE ON DATABASE {database} TO {reader}"], "write in the catalog database"),
        (["GRANT pg_monitor TO {reader}"], "role pg_monitor"),
        (["GRANT pg_read_all_stats TO {reader}"], "role pg_read_all_stats"),
        (["GRANT pg_read_all_settings TO {reader}"], "role pg_read_all_settings"),
    ],
)
def test_a_role_that_can_use_anything_beyond_the_catalog_schema_is_refused(
    catalog: Catalog, grants: list[str], kind: str
) -> None:
    catalog.execute(
        *(grant.format(reader=catalog.reader, database=catalog.database) for grant in grants)
    )
    try:
        error = refused(catalog.attachment())
    finally:
        if "{reader}_grp" in " ".join(grants):
            catalog.execute(f"REVOKE {catalog.reader}_grp FROM {catalog.reader}")
            catalog.execute(f"REVOKE ALL ON other.secrets FROM {catalog.reader}_grp")
            catalog.execute_on("admin", f"DROP ROLE {catalog.reader}_grp")
    assert error.code == "STORE_SOURCE_INVALID"
    assert error.details["kind"] == kind
    # The object kind is named, never an object of another schema.
    assert "other" not in error.message
    assert "secrets" not in error.message


def test_an_operator_or_function_of_the_owner_cannot_stand_in_for_postgresqls(
    catalog: Catalog,
) -> None:
    # The database's owner puts its own `<>` and `current_setting` ahead of pg_catalog's.
    catalog.execute(
        f"GRANT USAGE ON SCHEMA other TO {catalog.reader}",
        f"GRANT SELECT ON other.secrets TO {catalog.reader}",
        "CREATE FUNCTION cat.ne(name, name) RETURNS boolean LANGUAGE sql IMMUTABLE "
        "AS 'SELECT false'",
        "CREATE OPERATOR cat.<> (LEFTARG = name, RIGHTARG = name, FUNCTION = cat.ne)",
        "CREATE FUNCTION cat.current_setting(text) RETURNS text LANGUAGE sql "
        "AS 'SELECT ''on''::text'",
        f"ALTER DATABASE {catalog.database} SET search_path = cat, pg_catalog",
    )
    error = refused(catalog.attachment())
    assert error.code == "STORE_SOURCE_INVALID"
    assert error.details["kind"] in {"schema", "table"}


def test_a_hijacked_current_setting_cannot_fake_a_read_only_session(
    catalog: Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Without the store's session options, the database's search path reaches the owner's
    # `current_setting`, which answers `on`: only the qualified call tells the truth.
    monkeypatch.setattr(existing_lake, "READ_ONLY_SESSION", "")
    catalog.execute(
        "CREATE FUNCTION cat.current_setting(text) RETURNS text LANGUAGE sql "
        "AS 'SELECT ''on''::text'",
        f"ALTER DATABASE {catalog.database} SET search_path = cat, pg_catalog",
    )
    assert refused(catalog.attachment()).code == "SOURCE_NOT_READ_ONLY"


def test_a_role_setting_that_switches_the_role_is_refused(catalog: Catalog) -> None:
    # The login role holds pg_read_all_data, but its setting makes each session start as a
    # clean role; code running in the session could reset the role and read everything.
    other = f"{catalog.reader}_y"
    catalog.execute(
        f"CREATE ROLE {other} NOLOGIN",
        f"GRANT USAGE ON SCHEMA cat TO {other}",
        f"GRANT SELECT ON ALL TABLES IN SCHEMA cat TO {other}",
        f"GRANT {other} TO {catalog.reader}",
        f"GRANT pg_read_all_data TO {catalog.reader}",
        f"ALTER ROLE {catalog.reader} SET role = '{other}'",
    )
    try:
        error = refused(catalog.attachment())
    finally:
        catalog.execute(f"ALTER ROLE {catalog.reader} RESET role")
        catalog.execute_on("admin", f"DROP OWNED BY {other} CASCADE")
        catalog.execute(f"DROP OWNED BY {other} CASCADE")
        catalog.execute_on("admin", f"DROP ROLE {other}")
    assert error.code == "STORE_SOURCE_INVALID"
    assert error.details["kind"] == "role setting"


def test_privileges_are_those_of_the_login_role() -> None:
    # Membership is read from `session_user`, never `current_user`, and nothing in the SQL is
    # left for the search path to resolve.
    sql = existing_lake.privileges_sql("cat")
    assert "pg_has_role(session_user" in sql
    assert "current_user" not in sql
    for bare in (" <> ", " = ", " || ", " IN (", " !~ ", "CASE c.relkind WHEN"):
        assert bare not in sql


def test_an_invoker_view_reaching_another_schema_is_refused_before_the_attach(
    catalog: Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The catalog's owner swaps a catalog table for a view over a SECURITY INVOKER function
    # that reads another schema, with this role's privileges.
    catalog.execute(
        "ALTER TABLE cat.ducklake_metadata RENAME TO ducklake_metadata_real",
        "CREATE FUNCTION cat.leak() RETURNS TABLE (key varchar, value varchar, scope varchar, "
        "scope_id bigint) LANGUAGE sql SECURITY INVOKER AS "
        "'SELECT key, value, scope, scope_id FROM cat.ducklake_metadata_real UNION ALL "
        "SELECT ''leak'', value, NULL, NULL FROM other.secrets'",
        "CREATE VIEW cat.ducklake_metadata AS SELECT * FROM cat.leak()",
        f"ALTER FUNCTION cat.leak() OWNER TO {catalog.owner}",
        f"ALTER VIEW cat.ducklake_metadata OWNER TO {catalog.owner}",
        f"GRANT SELECT ON cat.ducklake_metadata TO {catalog.reader}",
    )
    attached: list[str] = []
    original = ExistingLake._attach

    def spy(self: ExistingLake, target: str, options: list[str]) -> None:
        attached.append(target)
        original(self, target, options)

    monkeypatch.setattr(ExistingLake, "_attach", spy)
    # Without a privilege on the other schema, the view exposes no row: it fails.
    error = refused(catalog.attachment())
    assert error.code == "STORE_SOURCE_INVALID"
    assert "do not read" not in str(error.details)
    assert attached == []
    # With one, the role check refuses the connection before DuckLake reads the catalog.
    catalog.execute(
        f"GRANT USAGE ON SCHEMA other TO {catalog.reader}",
        f"GRANT SELECT ON other.secrets TO {catalog.reader}",
    )
    error = refused(catalog.attachment())
    assert error.code == "STORE_SOURCE_INVALID"
    assert error.details["kind"] in {"schema", "table"}
    assert attached == []


def test_a_postgres_catalog_version_that_needs_migration_is_refused(catalog: Catalog) -> None:
    catalog.execute("UPDATE cat.ducklake_metadata SET value = '1.0' WHERE key = 'version'")
    assert refused(catalog.attachment()).code == "ENGINE_VERSION_UNTESTED"


def test_a_catalog_connection_cannot_set_its_own_options(catalog: Catalog) -> None:
    attachment = catalog.attachment()
    location = attachment.location
    assert isinstance(location, PostgresLakeLocation)
    changed = location.model_copy(
        update={"connection": f"{location.connection} options='-c search_path=other'"}
    )
    error = refused(attachment.model_copy(update={"location": changed}))
    assert error.code == "STORE_LOCATION_INVALID"


def test_a_postgres_lake_snapshot_is_verified_and_a_file_outside_is_not_served(
    catalog: Catalog, tmp_path: Path
) -> None:
    # The snapshot's tables are read from the catalog schema itself (PR 2).
    outside = tmp_path / "outside.parquet"
    duckdb.connect().execute(f"COPY (SELECT 1::BIGINT AS reading_id) TO '{outside}'")
    with ExistingLake(catalog.attachment(), memory_limit=MEMORY, threads=1) as lake:
        snapshot = lake.latest_snapshot()
        assert lake.verify(snapshot, lake.attachment) == snapshot
        catalog.execute(
            "INSERT INTO cat.ducklake_data_file (data_file_id, table_id, begin_snapshot, path, "
            "path_is_relative, file_format, record_count, file_size_bytes, footer_size, "
            f"row_id_start) SELECT 999, table_id, 0, '{outside}', false, 'parquet', 1, 1, 1, 0 "
            "FROM cat.ducklake_table"
        )
        with pytest.raises(StoreError, match="outside its data location"):
            lake.verify(snapshot, lake.attachment)
