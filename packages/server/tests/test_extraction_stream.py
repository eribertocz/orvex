"""P1-17 part 2 (#98): ClickHouse rows streamed into DuckDB exactly, without Python rows.

These tests need a ClickHouse server:
`TRANSFORM_TEST_CLICKHOUSE` (its HTTP URL) with an administrator in
`TRANSFORM_TEST_CLICKHOUSE_USER` / `_PASSWORD` (the CI service). They create a database and a
user with SELECT only, and drop them afterwards. The stream goes into a plain DuckDB
transaction through the store's `Transaction.load_arrow`, as a run does inside the lake.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import clickhouse_connect
import duckdb
import pytest
from clickhouse_connect.driver.client import Client
from orvex.contracts.project.v1 import ClickhouseConnection
from orvex.store.errors import StoreError
from orvex.store.lake import Transaction
from orvex.transform import extraction
from orvex.transform.clickhouse import (
    Column,
    Source,
    SourceError,
    load_columns,
    since_type,
)
from orvex.transform.incremental import Check
from orvex.transform.models import Model, discover_models

SERVER = os.environ.get("TRANSFORM_TEST_CLICKHOUSE")
live = pytest.mark.skipif(SERVER is None, reason="no ClickHouse server for tests")
READER_PASSWORD = "reader-" + uuid.uuid4().hex


# --- a live ClickHouse ----------------------------------------------------------------------


def admin() -> Client:
    assert SERVER is not None
    url = urlsplit(SERVER)
    return clickhouse_connect.get_client(
        host=url.hostname,
        port=url.port,
        username=os.environ.get("TRANSFORM_TEST_CLICKHOUSE_USER", "default"),
        password=os.environ.get("TRANSFORM_TEST_CLICKHOUSE_PASSWORD", ""),
        secure=url.scheme == "https",
    )


@pytest.fixture(scope="module")
def reader() -> Iterator[dict[str, str]]:
    """A database and a user that may only SELECT from it."""
    suffix = uuid.uuid4().hex[:12]
    database, user = f"test_{suffix}", f"reader_{suffix}"
    client = admin()
    for statement in [
        f"CREATE DATABASE {database}",
        f"CREATE USER {user} IDENTIFIED WITH sha256_password BY '{READER_PASSWORD}'",
        f"GRANT SELECT ON {database}.* TO {user}",
        # The functions extraction queries use need no grant beyond SELECT.
        f"GRANT SELECT ON system.numbers TO {user}",
    ]:
        client.command(statement)
    try:
        yield {"database": database, "user": user}
    finally:
        client.command(f"DROP USER IF EXISTS {user}")
        client.command(f"DROP DATABASE IF EXISTS {database}")
        client.close()


def source(reader: dict[str, str]) -> Source:
    assert SERVER is not None
    url = urlsplit(SERVER)
    connection = ClickhouseConnection.model_validate(
        {
            "type": "clickhouse",
            "host": url.hostname,
            "port": url.port,
            "database": reader["database"],
            "user": reader["user"],
            "password": "${env:UNUSED}",
            "read_only": True,
            "secure": url.scheme == "https",
        }
    )
    return Source("analytics_ch", connection, READER_PASSWORD)


def transaction(seconds: float = 60) -> tuple[duckdb.DuckDBPyConnection, Transaction]:
    connection = duckdb.connect()
    connection.execute("SET TimeZone = 'UTC'")
    connection.execute("ATTACH ':memory:' AS lake")
    connection.execute("USE lake")
    cursor = connection.cursor()
    cursor.begin()
    return cursor, Transaction(cursor, frozenset(), lambda _: int(seconds * 1000))


def model(tmp_path: Path, sql: str, sidecar: str) -> Model:
    folder = tmp_path / "models" / "raw"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "events.sql").write_text(sql, encoding="utf-8")
    (folder / "events.yaml").write_text(sidecar, encoding="utf-8")
    connection = ClickhouseConnection.model_validate(
        {
            "type": "clickhouse",
            "host": "h",
            "port": 8123,
            "database": "d",
            "user": "u",
            "password": "${env:UNUSED}",
            "read_only": True,
        }
    )
    found = discover_models(tmp_path, {"analytics_ch": connection})
    assert found.problems == ()
    return found.models[0]


def extract(
    reader: dict[str, str],
    sql: str,
    cursor_transaction: tuple[duckdb.DuckDBPyConnection, Transaction],
) -> tuple[duckdb.DuckDBPyConnection, int, tuple[Column, ...]]:
    """Describe, stream and load `sql` into `_extract.t`; the cursor, rows and columns."""
    cursor, handle = cursor_transaction
    client = source(reader)
    try:
        columns = client.describe(sql, None).columns
        stream = client.stream(sql, columns, None, 60)
        try:
            rows = handle.load_arrow(
                "_extract", "t", stream.reader, load_columns(columns), on_interrupt=stream.close
            )
        finally:
            stream.close()
    finally:
        client.close()
    return cursor, rows, columns


BOUNDARIES = """SELECT
    toUInt64('18446744073709551615') AS u64,
    toInt128('-170141183460469231731687303715884105728') AS i128_min,
    toInt128('170141183460469231731687303715884105727') AS i128_max,
    toUInt128('340282366920938463463374607431768211455') AS u128_max,
    toDecimal128('9999999999999999999999999999.9999999999', 10) AS d38,
    toDateTime64('2024-02-29 23:59:59.999999', 6, 'America/La_Paz') AS ts,
    toDateTime('2024-02-29 23:59:59', 'UTC') AS seconds,
    toDate32('1900-01-01') AS d32,
    toDate('2024-01-02') AS d,
    CAST(NULL AS Nullable(Int32)) AS n_int,
    CAST(NULL AS Nullable(String)) AS n_text,
    CAST(NULL AS Nullable(Decimal(18, 4))) AS n_decimal,
    CAST(NULL AS Nullable(DateTime64(3, 'UTC'))) AS n_time,
    '' AS empty,
    repeat('é', 524288) AS mebibyte,
    CAST('b' AS Enum8('a' = 1, 'b' = 2)) AS e,
    toFixedString('ab', 2) AS fixed,
    toBool(true) AS flag,
    toUUID('00000000-0000-0000-0000-000000000001') AS id
"""


@live
def test_boundary_values_arrive_exact(reader: dict[str, str]) -> None:
    cursor, rows, _ = extract(reader, BOUNDARIES, transaction())
    assert rows == 1
    found = cursor.execute(
        "SELECT CAST(u64 AS VARCHAR), CAST(i128_min AS VARCHAR), CAST(i128_max AS VARCHAR), "
        "CAST(u128_max AS VARCHAR), CAST(d38 AS VARCHAR), "
        "strftime(timezone('UTC', ts), '%Y-%m-%d %H:%M:%S.%f'), "
        "strftime(timezone('UTC', seconds), '%Y-%m-%d %H:%M:%S'), "
        "CAST(d32 AS VARCHAR), CAST(d AS VARCHAR), "
        "n_int IS NULL, n_text IS NULL, n_decimal IS NULL, n_time IS NULL, "
        "empty, length(mebibyte), octet_length(encode(mebibyte)), e, fixed, flag, "
        "CAST(id AS VARCHAR) FROM _extract.t"
    ).fetchall()
    assert found == [
        (
            "18446744073709551615",
            "-170141183460469231731687303715884105728",
            "170141183460469231731687303715884105727",
            "340282366920938463463374607431768211455",
            "9999999999999999999999999999.9999999999",
            "2024-03-01 03:59:59.999999",  # La Paz is UTC-4: the same instant
            "2024-02-29 23:59:59",
            "1900-01-01",
            "2024-01-02",
            True,
            True,
            True,
            True,
            "",
            524288,
            1048576,
            "b",
            "ab",
            True,
            "00000000-0000-0000-0000-000000000001",
        )
    ]
    types = {
        str(name): str(kind) for name, kind, *_ in cursor.execute("DESCRIBE _extract.t").fetchall()
    }
    assert types["i128_min"] == "HUGEINT"
    assert types["u128_max"] == "UHUGEINT"
    assert types["d38"] == "DECIMAL(38,10)"
    assert types["ts"] == "TIMESTAMP WITH TIME ZONE"
    assert types["e"] == "VARCHAR"


@live
def test_text_that_is_not_utf8_is_refused_with_its_position(
    reader: dict[str, str], tmp_path: Path
) -> None:
    sql = "SELECT number AS n, if(number = 2, unhex('C328'), 'ok') AS label FROM numbers(4)"
    cursor, rows, columns = extract(reader, sql, transaction())
    assert rows == 4
    sidecar = "version: 1\nconnection: analytics_ch\n"
    planned = extraction.steps(model(tmp_path, sql, sidecar), columns, [], None, "", False, None)
    (check,) = [step for step in planned if isinstance(step, Check)]
    assert check.code == "UNSUPPORTED_SOURCE_VALUE"
    value = cursor.execute(check.sql.replace('"_extract"."raw__events"', "_extract.t")).fetchone()
    assert value is not None
    assert value[0] == "label (row 3 of the stream)"
    # The bad value itself never reached DuckDB.
    assert cursor.execute("SELECT count(*) FROM _extract.t WHERE label IS NULL").fetchone() == (1,)


@live
def test_a_million_rows_stream_in_batches_with_bounded_memory(reader: dict[str, str]) -> None:
    sql = (
        "SELECT number AS id, toDecimal64(number / 100, 2) AS amount, "
        "concat('meter-', toString(number % 1000)) AS meter FROM numbers(1000000)"
    )
    cursor, handle = transaction()
    client = source(reader)
    peak = [0]
    sampling = threading.Event()

    def rss() -> int:
        with open("/proc/self/statm", encoding="ascii") as statm:
            return int(statm.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")

    def sample() -> None:
        while not sampling.is_set():
            peak[0] = max(peak[0], rss())
            time.sleep(0.02)

    base = rss()
    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    try:
        columns = client.describe(sql, None).columns
        stream = client.stream(sql, columns, None, 120)
        batches = [0]
        original = stream.reader

        def counted() -> Iterator[object]:
            for batch in original:
                batches[0] += 1
                yield batch

        import pyarrow as pa

        reader_counted = pa.RecordBatchReader.from_batches(original.schema, counted())
        rows = handle.load_arrow("_extract", "t", reader_counted, load_columns(columns))
        stream.close()
    finally:
        sampling.set()
        sampler.join()
        client.close()
    assert rows == 1_000_000
    assert stream.rows == 1_000_000
    assert batches[0] > 1
    assert peak[0] - base < 512 * 1024 * 1024
    # The same totals ClickHouse computes itself: every value arrived unchanged.
    totals = admin()
    try:
        expected = totals.query(
            f"SELECT toString(sum(id)), toString(sum(amount)) FROM ({sql})"
        ).result_rows[0]
    finally:
        totals.close()
    assert cursor.execute(
        "SELECT CAST(sum(id) AS VARCHAR), CAST(sum(amount) AS VARCHAR) FROM _extract.t"
    ).fetchone() == tuple(expected)


def processes(sql_mark: str) -> int:
    client = admin()
    try:
        return int(
            client.command(  # type: ignore[arg-type]  # a one-value SELECT returns its value
                f"SELECT count() FROM system.processes WHERE query LIKE '%{sql_mark}%' "
                "AND query NOT LIKE '%system.processes%'"
            )
        )
    finally:
        client.close()


@live
def test_a_cancelled_load_stops_the_clickhouse_query(reader: dict[str, str]) -> None:
    mark = f"cancel_{uuid.uuid4().hex[:8]}"
    sql = f"SELECT number AS {mark} FROM numbers(100000000000)"  # far longer than the test
    cursor, handle = transaction()
    client = source(reader)
    columns = client.describe(sql, None).columns
    stream = client.stream(sql, columns, None, 120)
    result: dict[str, object] = {}

    def load() -> None:
        try:
            handle.load_arrow(
                "_extract", "t", stream.reader, load_columns(columns), on_interrupt=stream.close
            )
            result["outcome"] = "loaded"
        except StoreError as error:
            result["outcome"] = error.code

    worker = threading.Thread(target=load)
    worker.start()
    deadline = time.monotonic() + 30
    while stream.rows == 0 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert processes(mark) == 1  # running while rows arrive
    handle.cancel()
    worker.join(30)
    client.close()
    assert result["outcome"] == "STORE_CANCELLED"
    deadline = time.monotonic() + 5
    while processes(mark) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert processes(mark) == 0
    cursor.rollback()


@live
def test_a_clickhouse_error_mid_stream_fails_the_load(reader: dict[str, str]) -> None:
    sql = "SELECT number AS n, throwIf(number = 300000, 'stop here') AS boom FROM numbers(1000000)"
    cursor, handle = transaction()
    client = source(reader)
    columns = client.describe(sql, None).columns
    stream = client.stream(sql, columns, None, 120)
    try:
        with pytest.raises(SourceError) as failed:
            handle.load_arrow("_extract", "t", stream.reader, load_columns(columns))
    finally:
        stream.close()
        client.close()
    assert failed.value.code == "SOURCE_QUERY_FAILED"
    assert "stop here" in failed.value.message
    assert "(ClickHouse code 395)" in failed.value.message
    assert READER_PASSWORD not in failed.value.message
    cursor.rollback()


@live
def test_queries_are_read_only_with_since_bound_on_the_server(reader: dict[str, str]) -> None:
    mark = f"since_{uuid.uuid4().hex[:8]}"
    sql = (
        f"SELECT number AS {mark}, toDateTime64('2024-01-01 00:00:00', 6, 'UTC') + number AS t "
        "FROM numbers(10) WHERE t > {since:DateTime64(6, 'UTC')}"
    )
    since = "2024-01-01 00:00:03.000000"
    declared = since_type("DateTime64(6, 'UTC')")
    _, handle = transaction()
    client = source(reader)
    try:
        columns = client.describe(sql, declared).columns
        stream = client.stream(sql, columns, since, 60)
        rows = handle.load_arrow("_extract", "t", stream.reader, load_columns(columns))
        stream.close()
    finally:
        client.close()
    assert rows == 6  # 4 to 9: the row exactly at `since` is not re-read with `>`
    logs = admin()
    try:
        logs.command("SYSTEM FLUSH LOGS")
        found = logs.query(
            "SELECT Settings['readonly'], query FROM system.query_log "
            f"WHERE type = 'QueryFinish' AND query LIKE '%{mark}%rowNumberInAllBlocks%' "
            "AND query NOT LIKE '%system.query_log%'"
        ).result_rows
    finally:
        logs.close()
    assert found
    for readonly, query in found:
        assert readonly == "1"
        # Bound by the server: the log shows ClickHouse's own substitution of the parameter
        # (`_CAST(…, 'DateTime64(6, \'UTC\')')`), never the value written into the SQL text.
        assert "_CAST('" in query
        assert since not in query


@live
def test_a_changed_source_is_refused_before_reading(reader: dict[str, str]) -> None:
    client = source(reader)
    try:
        columns = client.describe("SELECT toInt32(1) AS a", None).columns
        with pytest.raises(SourceError) as changed:
            client.check_columns("SELECT toInt64(1) AS a", None, columns)
    finally:
        client.close()
    assert changed.value.code == "SCHEMA_CHANGED"
