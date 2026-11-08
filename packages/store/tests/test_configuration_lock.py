"""#246: the managed lake's connection is locked once it is open. No setting changes afterwards,
through the store's API or on the connection and its cursors, and DuckLake's maintenance and
every write path still run."""

from __future__ import annotations

import datetime as dt

import duckdb
import pyarrow as pa
import pytest
from orvex.contracts.store.v1 import ReadRequest, StatementRequest
from orvex.store.errors import StoreError
from orvex.store.lake import Lake

EVERY_CATALOG = True

SETTINGS = [
    "SET TimeZone = 'America/La_Paz'",
    "SET GLOBAL TimeZone = 'America/La_Paz'",
    "RESET TimeZone",
    "SET python_enable_replacements = true",
    "SET enable_external_access = true",
    "SET allowed_directories = ['/']",
    "SET allowed_paths = ['/etc/passwd']",
    "SET autoload_known_extensions = true",
    "SET autoinstall_known_extensions = true",
    "SET allow_persistent_secrets = true",
    "SET temp_directory = '/tmp'",
    "SET disabled_filesystems = ''",
    "SET memory_limit = '64GB'",
    "SET threads = 64",
    "SET lock_configuration = false",
    "RESET lock_configuration",
    "PRAGMA threads = 64",
]


def statement(sql: str) -> StatementRequest:
    return StatementRequest(kind="statement", sql=sql, parameters=[])


def request(lake: Lake, sql: str) -> ReadRequest:
    snapshot = lake.current_snapshot().id
    return ReadRequest(kind="read", sql=sql, parameters=[], snapshot=snapshot, batch_size=1024)


def code_of(work: object) -> str:
    with pytest.raises(StoreError) as raised:
        work()  # type: ignore[operator]
    return raised.value.code


@pytest.mark.parametrize("sql", SETTINGS)
def test_no_setting_changes_on_the_connection_or_a_cursor(lake: Lake, sql: str) -> None:
    for connection in (lake._connection, lake._cursor()):
        with pytest.raises(duckdb.Error, match="configuration has been locked"):
            connection.execute(sql)


@pytest.mark.parametrize("sql", SETTINGS)
def test_no_setting_is_accepted_through_the_store(lake: Lake, sql: str) -> None:
    assert code_of(lambda: lake.read(request(lake, sql))) == "STORE_STATEMENT_INVALID"
    assert code_of(lambda: lake.describe(sql)) == "STORE_STATEMENT_INVALID"
    with lake.transaction() as transaction:
        assert code_of(lambda: transaction.execute(statement(sql))) == "STORE_STATEMENT_INVALID"
        assert code_of(lambda: transaction.value(statement(sql))) == "STORE_STATEMENT_INVALID"


def test_every_cursor_reads_in_utc_without_python_objects(lake: Lake) -> None:
    cursor = lake._cursor()
    settings = dict(
        cursor.execute(
            "SELECT name, value FROM duckdb_settings() WHERE name IN ('TimeZone', "
            "'python_enable_replacements', 'enable_external_access', 'lock_configuration')"
        ).fetchall()
    )
    assert settings == {
        "TimeZone": "UTC",
        "python_enable_replacements": "false",
        "enable_external_access": "false",
        "lock_configuration": "true",
    }
    t = pa.table({"x": [1]})  # a Python object a replacement scan would read
    with pytest.raises(duckdb.CatalogException):
        cursor.execute("SELECT * FROM t")
    assert t.num_rows == 1
    cursor.close()
    with lake.transaction() as transaction:
        transaction.execute(
            statement("CREATE TABLE z AS SELECT TIMESTAMPTZ '2026-01-01 00:00Z' AS m")
        )
    read = lake.read(request(lake, "SELECT m FROM z")).read_all()
    assert read.schema.field("m").type == pa.timestamp("us", tz="UTC")
    assert read.column("m")[0].as_py() == dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


def test_every_write_path_and_maintenance_run_on_the_locked_lake(lake: Lake) -> None:
    with lake.transaction() as transaction:
        transaction.execute(statement("CREATE TABLE t AS SELECT range AS n FROM range(10)"))
    for step in range(3):
        with lake.transaction() as transaction:
            transaction.execute(statement(f"INSERT INTO t VALUES ({100 + step})"))
            transaction.execute(statement(f"DELETE FROM t WHERE n = {step}"))
            transaction.execute(statement(f"UPDATE t SET n = n + 1000 WHERE n = {9 - step}"))
            assert transaction.value(statement("SELECT count(*) FROM t")) == "10"
    with lake.transaction() as transaction:
        stream = pa.RecordBatchReader.from_batches(
            pa.schema([("a", pa.int64())]), [pa.record_batch([pa.array([1, 2, 3])], ["a"])]
        )
        assert transaction.load_arrow("main", "loaded", stream, [("a", "BIGINT")]) == 3
    report = lake.maintain(
        dt.datetime.now(dt.UTC) + dt.timedelta(days=1),
        dt.datetime.now(dt.UTC) - dt.timedelta(hours=1),
    )
    assert report.snapshots_expired > 0
    rows = lake.read(request(lake, "SELECT count(*) AS c, sum(n) AS s FROM t")).read_all()
    assert rows.to_pylist() == [{"c": 10, "s": sum([3, 4, 5, 6, 100, 101, 102, 1007, 1008, 1009])}]
    loaded = lake.read(request(lake, "SELECT sum(a) AS s FROM loaded")).read_all()
    assert loaded.to_pylist() == [{"s": 6}]
