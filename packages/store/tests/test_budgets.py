"""P1-04: time limits, cancellation and admission control."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import pytest
from orvex.contracts.store.v1 import (
    LocalLakeLocation,
    PostgresLakeLocation,
    ReadRequest,
    StatementRequest,
)
from orvex.store.errors import StoreError
from orvex.store.lake import Admission, Lake, Limits, open_lake

SLOW = "SELECT sum(hash(a.range + b.range)) AS n FROM range(200000000) AS a, range(1000) AS b"


def limits(max_concurrent: int = 8, queue_size: int = 64, timeout_ms: int = 600_000) -> Limits:
    return Limits("1GB", 2, max_concurrent, queue_size, timeout_ms)


@pytest.fixture
def lake(location: LocalLakeLocation | PostgresLakeLocation) -> Iterator[Lake]:
    opened = open_lake(location, limits(max_concurrent=2, queue_size=1))
    yield opened
    opened.close()


def request(sql: str, lake: Lake, timeout_ms: int | None = None) -> ReadRequest:
    return ReadRequest(
        kind="read",
        sql=sql,
        parameters=[],
        snapshot=lake.current_snapshot().id,
        batch_size=1024,
        timeout_ms=timeout_ms,
    )


def code_of(action: object) -> str:
    assert callable(action)
    with pytest.raises(StoreError) as raised:
        action()
    return raised.value.code


def test_read_timeout_interrupts_and_the_lake_stays_usable(lake: Lake) -> None:
    started = time.monotonic()
    assert code_of(lambda: lake.read(request(SLOW, lake, 200)).read_all()) == "STORE_TIMEOUT"
    assert time.monotonic() - started < 1.0
    assert lake.read(request("SELECT 1 AS x", lake)).read_all().to_pylist() == [{"x": 1}]


def test_transaction_timeout_rolls_back(lake: Lake) -> None:
    with lake.transaction() as transaction:
        transaction.execute(
            StatementRequest(kind="statement", sql="CREATE TABLE t (n BIGINT)", parameters=[])
        )
    before = lake.current_snapshot().id

    def slow_insert() -> None:
        with lake.transaction() as slow:
            slow.execute(
                StatementRequest(kind="statement", sql="INSERT INTO t VALUES (1)", parameters=[])
            )
            slow.execute(
                StatementRequest(
                    kind="statement", sql=f"INSERT INTO t {SLOW}", parameters=[], timeout_ms=200
                )
            )

    assert code_of(slow_insert) == "STORE_TIMEOUT"
    assert lake.current_snapshot().id == before
    assert lake.read(request("SELECT count(*) AS n FROM t", lake)).read_all().to_pylist() == [
        {"n": 0}
    ]


def test_cancel_read_from_another_thread(lake: Lake) -> None:
    handle = lake.start_read(request(SLOW, lake))
    threading.Timer(0.2, handle.cancel).start()
    started = time.monotonic()
    assert code_of(handle.reader.read_all) == "STORE_CANCELLED"
    assert time.monotonic() - started < 1.2  # the cancel comes at 0.2 s


def test_cancel_transaction_rolls_back(lake: Lake) -> None:
    before = lake.current_snapshot().id

    def cancelled() -> None:
        with lake.transaction() as transaction:
            transaction.execute(
                StatementRequest(
                    kind="statement", sql="CREATE TABLE c AS SELECT 1 AS x", parameters=[]
                )
            )
            threading.Timer(0.2, transaction.cancel).start()
            transaction.execute(
                StatementRequest(kind="statement", sql=f"CREATE TABLE d AS {SLOW}", parameters=[])
            )

    assert code_of(cancelled) == "STORE_CANCELLED"
    assert lake.current_snapshot().id == before


def test_admission_runs_two_queues_one_and_refuses_the_fourth(lake: Lake) -> None:
    first = lake.start_read(request(SLOW, lake))
    second = lake.start_read(request(SLOW, lake))
    third: dict[str, object] = {}

    def queued() -> None:
        third["rows"] = lake.read(request("SELECT 3 AS x", lake)).read_all().to_pylist()

    waiter = threading.Thread(target=queued)
    waiter.start()
    time.sleep(0.3)
    assert "rows" not in third
    assert code_of(lambda: lake.read(request("SELECT 4 AS x", lake))) == "STORE_BUSY"
    first.cancel()
    assert code_of(first.reader.read_all) == "STORE_CANCELLED"
    waiter.join(5)
    assert third["rows"] == [{"x": 3}]
    second.cancel()
    assert code_of(second.reader.read_all) == "STORE_CANCELLED"


def test_queued_request_times_out_without_starting(lake: Lake) -> None:
    first = lake.start_read(request(SLOW, lake))
    second = lake.start_read(request(SLOW, lake))
    try:
        started = time.monotonic()
        assert code_of(lambda: lake.read(request("SELECT 1 AS x", lake, 300))) == "STORE_TIMEOUT"
        assert time.monotonic() - started < 1.5
    finally:
        first.cancel()
        second.cancel()
        for handle in (first, second):
            with pytest.raises(StoreError):
                handle.reader.read_all()


def test_admission_is_first_come_first_served() -> None:
    admission = Admission(capacity=1, queue_size=5)
    admission.acquire(time.monotonic() + 5)
    order: list[int] = []

    def waiter(number: int) -> None:
        admission.acquire(time.monotonic() + 10)
        order.append(number)
        admission.release()

    threads = []
    for number in range(5):
        thread = threading.Thread(target=waiter, args=(number,))
        thread.start()
        threads.append(thread)
        time.sleep(0.05)
    admission.release()
    for thread in threads:
        thread.join(5)
    assert order == [0, 1, 2, 3, 4]


def test_admission_refuses_when_queue_full_and_times_out_in_queue() -> None:
    admission = Admission(capacity=1, queue_size=0)
    admission.acquire(time.monotonic() + 5)
    assert code_of(lambda: admission.acquire(time.monotonic() + 5)) == "STORE_BUSY"
    waiting = Admission(capacity=1, queue_size=1)
    waiting.acquire(time.monotonic() + 5)
    assert code_of(lambda: waiting.acquire(time.monotonic() + 0.1)) == "STORE_TIMEOUT"


def test_concurrent_mixed_load(location: LocalLakeLocation | PostgresLakeLocation) -> None:
    lake = open_lake(location, limits(max_concurrent=4, queue_size=64))
    try:
        with lake.transaction() as setup:
            setup.execute(
                StatementRequest(kind="statement", sql="CREATE TABLE m (w INTEGER)", parameters=[])
            )
        snapshots: list[int] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def work(number: int) -> None:
            try:
                if number % 2:
                    with lake.transaction() as transaction:
                        transaction.execute(
                            StatementRequest(
                                kind="statement",
                                sql=f"INSERT INTO m VALUES ({number})",
                                parameters=[],
                            )
                        )
                    assert transaction.result is not None
                    assert transaction.result.snapshot is not None
                    with lock:
                        snapshots.append(transaction.result.snapshot.id)
                else:
                    lake.read(request("SELECT count(*) AS n FROM m", lake)).read_all()
            except Exception as error:  # noqa: BLE001 - collected and asserted below
                with lock:
                    errors.append(error)

        threads = [threading.Thread(target=work, args=(number,)) for number in range(50)]
        started = time.monotonic()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        assert time.monotonic() - started < 60
        assert errors == []
        assert len(snapshots) == 25
        assert len(set(snapshots)) == 25
    finally:
        lake.close()
