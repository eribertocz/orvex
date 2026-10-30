"""#160: the store's own catalog queries run under the request's limit, a statement's limit is
capped by its transaction's, and a read that needs no catalog to be refused is refused before
it is admitted."""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable, Iterator

import duckdb
import pytest
from orvex.contracts.store.v1 import (
    LocalLakeLocation,
    PostgresLakeLocation,
    ReadRequest,
    StatementRequest,
)
from orvex.store import lake as lake_module
from orvex.store.errors import StoreError
from orvex.store.lake import Lake, Limits, Transaction, open_lake

type Location = LocalLakeLocation | PostgresLakeLocation
LIMIT_MS = 1000
MARGIN = 2.0  # seconds past the limit an interruption may take
SLOW = "SELECT sum(hash(a.range + b.range)) AS n FROM range(200000000) AS a, range(1000) AS b"


def request(sql: str, lake: Lake) -> ReadRequest:
    return ReadRequest(
        kind="read",
        sql=sql,
        parameters=[],
        snapshot=lake.current_snapshot().id,
        batch_size=1024,
        timeout_ms=None,
    )


def statement(sql: str) -> StatementRequest:
    return StatementRequest(kind="statement", sql=sql, parameters=[], timeout_ms=None)


def code_of(action: Callable[[], object]) -> str:
    with pytest.raises(StoreError) as raised:
        action()
    return raised.value.code


def open_with(location: Location, timeout_ms: int = LIMIT_MS) -> Lake:
    return open_lake(location, Limits("1GB", 2, 1, 0, timeout_ms))


@pytest.fixture
def lake(location: Location) -> Iterator[Lake]:
    opened = open_with(location)
    with opened.transaction() as transaction:
        transaction.execute(statement("CREATE TABLE t (n BIGINT)"))
    yield opened
    opened.close()


def timed(action: Callable[[], object]) -> tuple[str, float]:
    started = time.monotonic()
    code = code_of(action)
    return code, time.monotonic() - started


def slow_on(cursor: duckdb.DuckDBPyConnection) -> int:
    cursor.execute(SLOW).fetchall()
    return 0


def test_a_statement_stops_at_its_transactions_limit(lake: Lake) -> None:
    before = lake.current_snapshot().id

    def block() -> None:
        with lake.transaction() as transaction:
            code = code_of(
                lambda: transaction.execute(
                    StatementRequest(
                        kind="statement",
                        sql=f"CREATE TABLE x AS {SLOW}",
                        parameters=[],
                        timeout_ms=60_000,
                    )
                )
            )
            assert code == "STORE_TIMEOUT"

    code, elapsed = timed(block)
    assert code == "STORE_TIMEOUT"
    assert elapsed < LIMIT_MS / 1000 + MARGIN, elapsed
    assert lake.current_snapshot().id == before


def test_the_checks_before_commit_stop_at_the_limit(
    lake: Lake, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = lake.current_snapshot().id
    monkeypatch.setattr(lake_module, "_unpinnable_objects", slow_on)

    def block() -> None:
        with lake.transaction() as transaction:
            transaction.execute(statement("INSERT INTO t VALUES (1)"))

    code, elapsed = timed(block)
    assert code == "STORE_TIMEOUT"
    assert elapsed < LIMIT_MS / 1000 + MARGIN, elapsed
    assert lake.current_snapshot().id == before
    monkeypatch.undo()
    # The writer lock and the slot are free: the next transaction commits.
    with lake.transaction() as transaction:
        transaction.execute(statement("INSERT INTO t VALUES (2)"))
    assert lake.read(request("SELECT n FROM t", lake)).read_all().to_pylist() == [{"n": 2}]


def test_a_cancel_during_the_checks_before_commit_rolls_back(
    location: Location, monkeypatch: pytest.MonkeyPatch
) -> None:
    lake = open_with(location, timeout_ms=600_000)  # only the cancel can stop it
    try:
        with lake.transaction() as transaction:
            transaction.execute(statement("CREATE TABLE t (n BIGINT)"))
        before = lake.current_snapshot().id
        checking = threading.Event()
        held: list[Transaction] = []

        def slow_after_signal(cursor: duckdb.DuckDBPyConnection) -> int:
            checking.set()
            return slow_on(cursor)

        monkeypatch.setattr(lake_module, "_unpinnable_objects", slow_after_signal)

        def cancel() -> None:
            if checking.wait(10):
                held[0].cancel()

        canceller = threading.Thread(target=cancel, daemon=True)
        canceller.start()

        def block() -> None:
            with lake.transaction() as transaction:
                held.append(transaction)
                transaction.execute(statement("INSERT INTO t VALUES (1)"))

        try:
            assert code_of(block) == "STORE_CANCELLED"
        finally:
            canceller.join(10)
        assert lake.current_snapshot().id == before
    finally:
        lake.close()


def test_a_reads_setup_stops_at_its_limit(lake: Lake, monkeypatch: pytest.MonkeyPatch) -> None:
    original = lake._tables_at

    def slow_tables(snapshot: int, cursor: duckdb.DuckDBPyConnection | None = None) -> object:
        assert cursor is not None  # on the read's own cursor, under its guard
        slow_on(cursor)
        return original(snapshot, cursor)

    monkeypatch.setattr(lake, "_tables_at", slow_tables)
    code, elapsed = timed(lambda: lake.read(request("SELECT n FROM t", lake)))
    assert code == "STORE_TIMEOUT"
    assert elapsed < LIMIT_MS / 1000 + MARGIN, elapsed
    monkeypatch.undo()
    # The slot is free (capacity 1, no queue: a held slot would be STORE_BUSY).
    assert lake.read(request("SELECT 1 AS x", lake)).read_all().to_pylist() == [{"x": 1}]


def test_a_commit_that_starts_in_time_is_never_reported_as_failed(
    location: Location, monkeypatch: pytest.MonkeyPatch
) -> None:
    lake = open_with(location, timeout_ms=3000)
    try:
        before = lake.current_snapshot().id
        real = lake_module._commit

        def slow_commit(cursor: duckdb.DuckDBPyConnection) -> None:
            time.sleep(3.5)  # the transaction's limit passes while it commits
            real(cursor)

        monkeypatch.setattr(lake_module, "_commit", slow_commit)
        with lake.transaction() as transaction:
            transaction.execute(statement("CREATE TABLE c AS SELECT 1 AS n"))
        assert transaction.result is not None
        assert transaction.result.snapshot is not None
        assert transaction.result.snapshot.id > before
        assert lake.current_snapshot().id == transaction.result.snapshot.id
    finally:
        lake.close()


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT * FROM (SUMMARIZE t)", "STORE_STATEMENT_INVALID"),
        ("SELECT * FROM read_csv('x.csv')", "STORE_STATEMENT_INVALID"),
        ("SELECT * FROM glob('*')", "STORE_STATEMENT_INVALID"),
        ("SELECT getenv('HOME')", "STORE_STATEMENT_INVALID"),
        ("SELEC 1", "STORE_QUERY_FAILED"),
        ("SELECT n FROM t", "STORE_BUSY"),
    ],
)
def test_on_a_full_lake_an_invalid_read_gets_its_own_error(
    lake: Lake, sql: str, expected: str
) -> None:
    holder = lake.start_read(request("SELECT range AS n FROM range(100000000)", lake))
    try:
        found = code_of(lambda: lake.read(request(sql, lake)))
    finally:
        holder.cancel()
        with contextlib.suppress(StoreError):
            holder.reader.read_all()
    assert found == expected
