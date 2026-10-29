"""P1-07: SQLite and PostgreSQL sources attached read-only, with exact types only."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest
from orvex.contracts.store.v1 import (
    LocalLakeLocation,
    PostgresLakeLocation,
    PostgresSourceAttachment,
    ReadRequest,
    SqliteSourceAttachment,
    StatementRequest,
)
from orvex.store.errors import StoreError
from orvex.store.lake import Lake, Limits, open_lake

LIMITS = Limits(
    memory_limit="1GB", threads=2, max_concurrent=8, queue_size=64, default_timeout_ms=600_000
)
REPOSITORY = Path(__file__).resolve().parents[3]
PASSWORD_ENV = "STORE_TEST_POSTGRES_PASSWORD"

type Location = LocalLakeLocation | PostgresLakeLocation


def write(lake: Lake, *sqls: str) -> None:
    with lake.transaction() as transaction:
        for sql in sqls:
            transaction.execute(StatementRequest(kind="statement", sql=sql, parameters=[]))


def read(lake: Lake, sql: str) -> list[dict[str, object]]:
    request = ReadRequest(
        kind="read", sql=sql, parameters=[], snapshot=lake.current_snapshot().id, batch_size=65536
    )
    rows: list[dict[str, object]] = lake.read(request).read_all().to_pylist()
    return rows


def code_of(action: object) -> str:
    assert callable(action)
    with pytest.raises(StoreError) as raised:
        action()
    return raised.value.code


# --- SQLite -----------------------------------------------------------------------------------


@pytest.fixture
def sqlite_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("sqlite") / "field.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE consumers (id INTEGER, name TEXT, ratio REAL, photo BLOB, note TEXT);
        INSERT INTO consumers VALUES (1, 'Ana', 0.5, x'00ff', NULL);
        INSERT INTO consumers VALUES (9007199254740993, 'Bea', -1.25, NULL, 'x');
        CREATE TABLE charges (id INTEGER, amount DECIMAL(10,2), fee NUMBER(12,2));
        INSERT INTO charges VALUES (1, 12.34, 0.10);
        """
    )
    connection.commit()
    connection.close()
    return path


def sqlite_source(path: Path, name: str = "field") -> SqliteSourceAttachment:
    return SqliteSourceAttachment(
        kind="source_attachment", name=name, type="sqlite", path=str(path)
    )


@pytest.fixture
def sqlite_lake(location: Location, sqlite_file: Path) -> Iterator[Lake]:
    opened = open_lake(location, LIMITS, [sqlite_source(sqlite_file)])
    yield opened
    opened.close()


def test_sqlite_table_is_copied_unchanged(sqlite_lake: Lake) -> None:
    write(sqlite_lake, "CREATE TABLE consumers AS SELECT * FROM field.consumers")
    rows = read(
        sqlite_lake,
        "SELECT CAST(id AS VARCHAR) AS id, name, ratio, photo, note FROM consumers ORDER BY id",
    )
    assert rows == [
        {"id": "1", "name": "Ana", "ratio": 0.5, "photo": b"\x00\xff", "note": None},
        {"id": "9007199254740993", "name": "Bea", "ratio": -1.25, "photo": None, "note": "x"},
    ]


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO field.consumers VALUES (2, 'Cid', 1.0, NULL, NULL)",
        "UPDATE field.consumers SET name = 'Z'",
        "DELETE FROM field.consumers",
        "CREATE TABLE field.extra AS SELECT 1 AS x",
    ],
)
def test_sqlite_source_is_read_only(sqlite_lake: Lake, sqlite_file: Path, sql: str) -> None:
    before = hashlib.sha256(sqlite_file.read_bytes()).hexdigest()
    assert code_of(lambda: write(sqlite_lake, sql)) == "STORE_QUERY_FAILED"
    assert hashlib.sha256(sqlite_file.read_bytes()).hexdigest() == before


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE x AS SELECT * FROM sqlite_query('{path}', 'SELECT 1')",
        "CREATE TABLE x AS SELECT * FROM sqlite_scan('{path}', 'consumers')",
        "CREATE TABLE x AS SELECT * FROM postgres_query('field', 'SELECT 1')",
        "CREATE TABLE x AS SELECT * FROM postgres_execute('field', 'SELECT 1')",
        "ATTACH '{path}' AS again (TYPE sqlite)",
        "DETACH field",
    ],
)
def test_source_functions_and_attach_are_refused(
    sqlite_lake: Lake, sqlite_file: Path, sql: str
) -> None:
    statement = sql.format(path=sqlite_file)
    assert code_of(lambda: write(sqlite_lake, statement)) == "STORE_STATEMENT_INVALID"


def test_reads_cannot_reference_sqlite(sqlite_lake: Lake) -> None:
    assert code_of(lambda: read(sqlite_lake, "SELECT * FROM field.consumers")) == (
        "STORE_STATEMENT_INVALID"
    )


def test_sqlite_decimal_column_is_refused_only_when_read(sqlite_lake: Lake) -> None:
    with pytest.raises(StoreError) as raised:
        write(sqlite_lake, "CREATE TABLE c AS SELECT id, amount FROM field.charges")
    assert raised.value.code == "UNSUPPORTED_SOURCE_TYPE"
    assert raised.value.details["column"] == "field.charges.amount"
    with pytest.raises(StoreError) as number:
        write(sqlite_lake, "CREATE TABLE f AS SELECT fee FROM field.charges")
    assert number.value.details["column"] == "field.charges.fee"
    write(sqlite_lake, "CREATE TABLE ids AS SELECT id FROM field.charges")
    assert read(sqlite_lake, "SELECT count(*) AS n FROM ids") == [{"n": 1}]


@pytest.mark.parametrize(
    "definition",
    [
        "CREATE VIEW billed AS SELECT id, amount AS total FROM charges",
        "CREATE VIEW wipe AS SELECT * FROM postgres_execute('production', 'DELETE FROM readings',"
        " use_transaction = false)",
    ],
    ids=["view", "view-calling-a-function"],
)
def test_sqlite_file_with_views_is_refused(
    location: Location, sqlite_file: Path, definition: str
) -> None:
    connection = sqlite3.connect(sqlite_file)
    connection.execute(definition)
    connection.commit()
    connection.close()
    with pytest.raises(StoreError) as raised:
        open_lake(location, LIMITS, [sqlite_source(sqlite_file)])
    assert raised.value.code == "STORE_SOURCE_INVALID"
    assert raised.value.details["source"] == "field"


def test_sqlite_trigger_is_harmless(location: Location, sqlite_file: Path) -> None:
    connection = sqlite3.connect(sqlite_file)
    connection.execute("CREATE TRIGGER audit AFTER INSERT ON charges BEGIN SELECT 1; END")
    connection.commit()
    connection.close()
    lake = open_lake(location, LIMITS, [sqlite_source(sqlite_file)])
    try:
        write(lake, "CREATE TABLE ids AS SELECT id FROM field.charges")
    finally:
        lake.close()


def test_sqlite_changes_after_opening_do_not_reach_the_lake(
    location: Location, sqlite_file: Path
) -> None:
    """The lake reads a private copy taken at open: a view or an inexact column added to the
    original afterwards cannot be used."""
    lake = open_lake(location, LIMITS, [sqlite_source(sqlite_file)])
    try:
        connection = sqlite3.connect(sqlite_file)
        connection.executescript(
            """
            DROP TABLE consumers;
            CREATE VIEW consumers AS SELECT * FROM charges;
            CREATE TABLE late (amount DECIMAL(10,2));
            """
        )
        connection.commit()
        connection.close()
        write(lake, "CREATE TABLE c AS SELECT name FROM field.consumers")
        assert read(lake, "SELECT count(*) AS n FROM c") == [{"n": 2}]
        assert code_of(lambda: write(lake, "CREATE TABLE l AS SELECT * FROM field.late")) == (
            "STORE_QUERY_FAILED"
        )
    finally:
        lake.close()


def test_missing_sqlite_file_is_not_created(location: Location, tmp_path: Path) -> None:
    missing = tmp_path / "absent" / "field.sqlite"
    with pytest.raises(StoreError) as raised:
        open_lake(location, LIMITS, [sqlite_source(missing)])
    assert raised.value.code == "SOURCE_NOT_FOUND"
    assert raised.value.details["source"] == "field"
    assert not missing.exists()


# --- PostgreSQL --------------------------------------------------------------------------------


def postgres_settings() -> dict[str, str]:
    connection = os.environ.get("STORE_TEST_POSTGRES")
    if not connection or not os.environ.get(PASSWORD_ENV):
        if os.environ.get("CI"):
            pytest.fail("STORE_TEST_POSTGRES and STORE_TEST_POSTGRES_PASSWORD are required in CI")
        pytest.skip("no PostgreSQL configured for this run")
    return dict(item.split("=", 1) for item in connection.split())


def postgres_source(
    schema: str | None, name: str = "production", password_env: str = PASSWORD_ENV
) -> PostgresSourceAttachment:
    settings = postgres_settings()
    return PostgresSourceAttachment.model_validate(
        {
            "kind": "source_attachment",
            "name": name,
            "type": "postgres",
            "host": settings["host"],
            "port": int(settings.get("port", "5432")),
            "database": settings["dbname"],
            "user": settings["user"],
            "password_env": password_env,
            **({"schema": schema} if schema else {}),
        }
    )


@pytest.fixture
def admin() -> Iterator[duckdb.DuckDBPyConnection]:
    """A separate, writable connection that prepares the source database (as a DBA would)."""
    settings = postgres_settings()
    connection = duckdb.connect()
    connection.install_extension("postgres")
    connection.load_extension("postgres")
    password = os.environ[PASSWORD_ENV].replace("'", "''")
    connection.sql(f"CREATE SECRET (TYPE postgres, PASSWORD '{password}')")
    target = " ".join(f"{key}={value}" for key, value in settings.items())
    connection.sql(f"ATTACH '{target}' AS pg (TYPE postgres)")
    yield connection
    connection.close()


@pytest.fixture
def schema(admin: duckdb.DuckDBPyConnection) -> Iterator[str]:
    name = f"src_{uuid.uuid4().hex[:12]}"
    execute(admin, f"CREATE SCHEMA {name}")
    execute(
        admin,
        f"CREATE TABLE {name}.readings (reading_id bigint, kwh numeric(18,4), "
        "read_at timestamptz, read_on date, status text, loose numeric, cash money, "
        "rounded numeric(5,-2))",
    )
    execute(
        admin,
        f"CREATE MATERIALIZED VIEW {name}.monthly AS "
        f"SELECT reading_id, sum(kwh) AS total FROM {name}.readings GROUP BY reading_id",
    )
    execute(
        admin,
        f"INSERT INTO {name}.readings VALUES "
        "(1, 12.3456, '2024-01-31 23:59:59.123456-04', '2024-01-31', 'valid', 1.5, 2.50, 1200), "
        "(9007199254740993, -0.0001, NULL, NULL, NULL, NULL, NULL, NULL)",
    )
    yield name
    execute(admin, f"DROP SCHEMA {name} CASCADE")


def execute(admin: duckdb.DuckDBPyConnection, sql: str) -> None:
    """Run SQL on the source database itself (setup only; never through the store)."""
    quoted = sql.replace("'", "''")
    admin.sql(f"CALL postgres_execute('pg', '{quoted}')")


def test_postgres_tables_arrive_exact(location: Location, schema: str) -> None:
    for declared, name in [
        (schema, "production.readings"),
        (None, f"production.{schema}.readings"),
    ]:
        lake = open_lake(location, LIMITS, [postgres_source(declared)])
        try:
            write(
                lake,
                "CREATE OR REPLACE TABLE r AS "
                f"SELECT reading_id, kwh, read_at, read_on, status FROM {name}",
            )
            rows = read(
                lake,
                "SELECT CAST(reading_id AS VARCHAR) AS id, CAST(kwh AS VARCHAR) AS kwh, "
                "typeof(kwh) AS kwh_type, typeof(read_at) AS at_type, "
                "CAST(epoch_us(read_at) AS VARCHAR) AS at_us, CAST(read_on AS VARCHAR) AS d, "
                "status FROM r ORDER BY reading_id",
            )
        finally:
            lake.close()
        assert rows == [
            {
                "id": "1",
                "kwh": "12.3456",
                "kwh_type": "DECIMAL(18,4)",
                "at_type": "TIMESTAMP WITH TIME ZONE",
                "at_us": "1706759999123456",
                "d": "2024-01-31",
                "status": "valid",
            },
            {
                "id": "9007199254740993",
                "kwh": "-0.0001",
                "kwh_type": "DECIMAL(18,4)",
                "at_type": "TIMESTAMP WITH TIME ZONE",
                "at_us": None,
                "d": None,
                "status": None,
            },
        ]


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO production.readings (reading_id) VALUES (3)",
        "UPDATE production.readings SET status = 'x'",
        "DELETE FROM production.readings",
        "CREATE TABLE production.extra AS SELECT 1 AS x",
    ],
)
def test_postgres_source_is_read_only(
    location: Location, schema: str, admin: duckdb.DuckDBPyConnection, sql: str
) -> None:
    lake = open_lake(location, LIMITS, [postgres_source(schema)])
    try:
        # The test user owns the schema and could write: only the store's settings refuse it.
        assert code_of(lambda: write(lake, sql)) == "STORE_QUERY_FAILED"
    finally:
        lake.close()
    count = admin.sql(f"SELECT count(*) FROM pg.{schema}.readings").fetchall()
    statuses = admin.sql(f"SELECT count(*) FROM pg.{schema}.readings WHERE status = 'x'")
    assert count == [(2,)]
    assert statuses.fetchall() == [(0,)]


def test_reads_cannot_reference_postgres(location: Location, schema: str) -> None:
    lake = open_lake(location, LIMITS, [postgres_source(schema)])
    try:
        assert code_of(lambda: read(lake, "SELECT * FROM production.readings")) == (
            "STORE_STATEMENT_INVALID"
        )
    finally:
        lake.close()


@pytest.mark.parametrize("column", ["loose", "cash", "rounded"])
@pytest.mark.parametrize("declared", [True, False], ids=["schema", "no-schema"])
def test_inexact_postgres_columns_are_refused_only_when_read(
    location: Location, schema: str, column: str, declared: bool
) -> None:
    lake = open_lake(location, LIMITS, [postgres_source(schema if declared else None)])
    table = "production.readings" if declared else f"production.{schema}.readings"
    label = (
        f"production.readings.{column}" if declared else f"production.{schema}.readings.{column}"
    )
    try:
        with pytest.raises(StoreError) as raised:
            write(lake, f"CREATE TABLE x AS SELECT reading_id, {column} FROM {table}")
        assert raised.value.code == "UNSUPPORTED_SOURCE_TYPE"
        assert raised.value.details["column"] == label
        with pytest.raises(StoreError) as filtered:
            write(
                lake,
                f"CREATE TABLE x AS SELECT reading_id FROM {table} WHERE {column} IS NOT NULL",
            )
        assert filtered.value.code == "UNSUPPORTED_SOURCE_TYPE"
        write(lake, f"CREATE TABLE ok AS SELECT reading_id, kwh FROM {table}")
        assert read(lake, "SELECT count(*) AS n FROM ok") == [{"n": 2}]
    finally:
        lake.close()


def test_materialized_view_sum_is_refused(location: Location, schema: str) -> None:
    """sum() of numeric(18,4) is numeric without precision: DuckDB would read it as DOUBLE."""
    lake = open_lake(location, LIMITS, [postgres_source(schema)])
    try:
        with pytest.raises(StoreError) as raised:
            write(lake, "CREATE TABLE m AS SELECT * FROM production.monthly")
        assert raised.value.code == "UNSUPPORTED_SOURCE_TYPE"
        assert raised.value.details["column"] == "production.monthly.total"
    finally:
        lake.close()


def test_unreachable_host_fails_within_the_connect_timeout(location: Location) -> None:
    postgres_settings()
    # TEST-NET-1, another machine: verified TLS is required (#122), and the connection times out
    # before any TLS is negotiated.
    unreachable = postgres_source(None).model_copy(
        update={"host": "192.0.2.1", "sslmode": "verify-full"}
    )
    started = time.monotonic()
    with pytest.raises(StoreError) as raised:
        open_lake(location, LIMITS, [unreachable])
    assert raised.value.code == "STORE_CONNECTION_FAILED"
    assert time.monotonic() - started < 30


def test_postgres_credentials_never_appear(
    location: Location, schema: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrong = "wrong-password-very-recognizable"
    monkeypatch.setenv("SOURCE_WRONG_PASSWORD", wrong)
    with pytest.raises(StoreError) as raised:
        open_lake(location, LIMITS, [postgres_source(schema, password_env="SOURCE_WRONG_PASSWORD")])
    assert raised.value.code == "STORE_CONNECTION_FAILED"
    assert raised.value.details["source"] == "production"
    assert wrong not in str(raised.value)
    assert wrong not in repr(raised.value.details)
    assert raised.value.__cause__ is None
    monkeypatch.delenv("SOURCE_MISSING_PASSWORD", raising=False)
    with pytest.raises(StoreError) as missing:
        open_lake(
            location, LIMITS, [postgres_source(schema, password_env="SOURCE_MISSING_PASSWORD")]
        )
    assert missing.value.code == "STORE_SECRET_MISSING"
    assert missing.value.details["variable"] == "SOURCE_MISSING_PASSWORD"


def test_synthetic_dataset_through_a_postgres_source(
    location: Location, schema: str, admin: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    out = tmp_path / "small"
    subprocess.run(
        [sys.executable, "-m", "tools.synthetic.generate", "--size", "small", "--out", str(out)],
        cwd=REPOSITORY,
        check=True,
        capture_output=True,
    )
    tables = ["consumers", "meters", "readings", "invoices", "payments", "credit_notes"]
    for table in tables:
        parquet = out / f"{table}.parquet"
        admin.sql(
            f"CREATE TABLE pg.{schema}.syn_{table} AS SELECT * FROM read_parquet('{parquet}')"
        )
    lake = open_lake(location, LIMITS, [postgres_source(schema)])
    try:
        write(lake, *[f"CREATE TABLE {t} AS SELECT * FROM production.syn_{t}" for t in tables])
        counts = {t: read(lake, f"SELECT count(*) AS n FROM {t}")[0]["n"] for t in tables}
    finally:
        lake.close()
    assert counts == {
        "consumers": 10_000,
        "meters": 10_300,
        "readings": 240_720,
        "invoices": 240_000,
        "payments": 236_400,
        "credit_notes": 100,
    }
