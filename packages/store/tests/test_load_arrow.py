"""#98: `Transaction.load_arrow`, an Arrow stream into a table of the write transaction.

A plain DuckDB transaction stands in for the lake's: the method only uses the transaction's
cursor (the lake's own transactions are exercised by the extraction runs in `server`).
"""

from __future__ import annotations

import os
import signal
import threading
import time
from collections.abc import Iterator

import duckdb
import pyarrow as pa
import pytest
from orvex.contracts.store.v1 import StatementRequest
from orvex.store.errors import StoreError
from orvex.store.lake import Transaction

SCHEMA = pa.schema([("amount", pa.string()), ("n", pa.int64())])
COLUMNS = [("amount", "HUGEINT"), ("n", "BIGINT")]


def transaction(seconds: float = 30) -> tuple[duckdb.DuckDBPyConnection, Transaction]:
    connection = duckdb.connect()
    connection.execute("ATTACH ':memory:' AS lake")
    connection.execute("USE lake")
    cursor = connection.cursor()
    cursor.begin()
    return cursor, Transaction(cursor, frozenset(), lambda _: int(seconds * 1000))


def batches(count: int, pause: float = 0) -> Iterator[pa.RecordBatch]:
    for index in range(count):
        if pause:
            time.sleep(pause)
        yield pa.record_batch(
            [pa.array([str(10**30 + index)]), pa.array([index], pa.int64())], schema=SCHEMA
        )


def reader(count: int, pause: float = 0) -> pa.RecordBatchReader:
    return pa.RecordBatchReader.from_batches(SCHEMA, batches(count, pause))


def test_rows_are_cast_exactly_and_counted() -> None:
    cursor, handle = transaction()
    assert handle.load_arrow("_extract", "t", reader(3), COLUMNS) == 3
    rows = cursor.execute("SELECT CAST(amount AS VARCHAR), n FROM _extract.t ORDER BY n").fetchall()
    assert rows == [
        ("1000000000000000000000000000000", 0),
        ("1000000000000000000000000000001", 1),
        ("1000000000000000000000000000002", 2),
    ]
    assert cursor.execute("DESCRIBE _extract.t").fetchall()[0][1] == "HUGEINT"


def test_fields_must_match_the_columns_in_order() -> None:
    _, handle = transaction()
    with pytest.raises(StoreError) as refused:
        handle.load_arrow("_extract", "t", reader(1), [("n", "BIGINT"), ("amount", "HUGEINT")])
    assert refused.value.code == "STORE_STATEMENT_INVALID"


@pytest.mark.parametrize(
    "kind", ["HUGEINT); DROP TABLE t; --", "decimal(18,4)", "DECIMAL(18,4", "", "VARCHAR -- x"]
)
def test_type_names_are_checked_before_any_sql(kind: str) -> None:
    _, handle = transaction()
    with pytest.raises(StoreError) as refused:
        handle.load_arrow("_extract", "t", reader(1), [("amount", kind), ("n", "BIGINT")])
    assert refused.value.code == "STORE_STATEMENT_INVALID"


def test_a_failing_stream_raises_its_own_error() -> None:
    class Broken(Exception):
        pass

    def broken() -> Iterator[pa.RecordBatch]:
        yield from batches(1)
        raise Broken("the source stopped")

    _, handle = transaction()
    with pytest.raises(Broken):
        handle.load_arrow(
            "_extract", "t", pa.RecordBatchReader.from_batches(SCHEMA, broken()), COLUMNS
        )


def test_a_time_limit_interrupts_and_calls_back() -> None:
    _, handle = transaction(seconds=0.5)
    called: list[str] = []
    started = time.monotonic()
    with pytest.raises(StoreError) as stopped:
        handle.load_arrow(
            "_extract",
            "t",
            reader(100, pause=0.1),
            COLUMNS,
            on_interrupt=lambda: called.append("x"),
        )
    assert stopped.value.code == "STORE_TIMEOUT"
    assert called == ["x"]
    assert time.monotonic() - started < 5


def test_names_differing_only_in_case_are_refused() -> None:
    # DuckDB resolves `a` and `A` to the same field, so one value would be read twice.
    schema = pa.schema([("a", pa.int64()), ("A", pa.int64())])
    stream = pa.RecordBatchReader.from_batches(
        schema, [pa.record_batch([pa.array([1]), pa.array([2])], schema=schema)]
    )
    _, handle = transaction()
    with pytest.raises(StoreError) as refused:
        handle.load_arrow("_extract", "t", stream, [("a", "BIGINT"), ("A", "BIGINT")])
    assert refused.value.code == "STORE_STATEMENT_INVALID"


def ctrl_c_after(seconds: float) -> threading.Timer:
    timer = threading.Timer(seconds, lambda: os.kill(os.getpid(), signal.SIGINT))
    timer.start()
    return timer


def test_ctrl_c_during_a_load_is_a_keyboard_interrupt() -> None:
    cursor, handle = transaction()
    called: list[str] = []
    ctrl_c_after(0.5)
    with pytest.raises(KeyboardInterrupt):
        handle.load_arrow(
            "_extract",
            "t",
            reader(100, pause=0.1),
            COLUMNS,
            on_interrupt=lambda: called.append("x"),
        )
    assert called == ["x"]  # the source is closed at once, as on a time limit
    cursor.rollback()
    assert cursor.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE table_name = 't'"
    ).fetchone() == (0,)


def test_ctrl_c_is_a_keyboard_interrupt() -> None:
    # DuckDB answers Ctrl-C during a statement with `RuntimeError("Query interrupted")`; the
    # store gives it back as `KeyboardInterrupt`, which a run records before stopping.
    cursor, handle = transaction()
    cursor.execute("CREATE TABLE big AS SELECT * FROM range(200000) AS r(i)")
    started = time.monotonic()
    ctrl_c_after(0.5)
    with pytest.raises(KeyboardInterrupt):
        handle.execute(
            StatementRequest(
                kind="statement",
                sql="CREATE TABLE pairs AS SELECT count(*) AS n FROM big a, big b",
                parameters=[],
            )
        )
    assert time.monotonic() - started < 10
