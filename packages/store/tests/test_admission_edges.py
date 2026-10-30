"""#77: admission, time limits and cancellation at their edges.

Every case reaches its state with events (or by firing the guard directly, as its timer does),
never with sleeps, and runs `ROUNDS` times to expose races.
"""

from __future__ import annotations

import datetime as dt
import functools
import threading
import time
from collections.abc import Callable, Iterator
from types import SimpleNamespace

import pytest
from orvex.contracts.store.v1 import (
    LocalLakeLocation,
    PostgresLakeLocation,
    ReadRequest,
    StatementRequest,
)
from orvex.store import lake as lake_module
from orvex.store.errors import StoreError
from orvex.store.lake import Guard, Lake, Limits, ReadHandle, open_lake

ROUNDS = 50
SLOW = "SELECT sum(hash(a.range + b.range)) AS n FROM range(200000000) AS a, range(1000) AS b"
# Many small batches, the first one at once.
LONG = "SELECT range AS n FROM range(100000000)"
WAIT = 10.0  # the most any event may take; reaching it fails the test

type Location = LocalLakeLocation | PostgresLakeLocation


def open_with(location: Location, max_concurrent: int, timeout_ms: int = 600_000) -> Lake:
    return open_lake(location, Limits("1GB", 2, max_concurrent, 0, timeout_ms))


def request(
    sql: str, lake: Lake, timeout_ms: int | None = None, snapshot: int | None = None
) -> ReadRequest:
    return ReadRequest(
        kind="read",
        sql=sql,
        parameters=[],
        snapshot=lake.current_snapshot().id if snapshot is None else snapshot,
        batch_size=1024,
        timeout_ms=timeout_ms,
    )


def statement(sql: str, timeout_ms: int | None = None) -> StatementRequest:
    return StatementRequest(kind="statement", sql=sql, parameters=[], timeout_ms=timeout_ms)


def code_of(action: Callable[[], object]) -> str:
    with pytest.raises(StoreError) as raised:
        action()
    return raised.value.code


def one(lake: Lake) -> list[dict[str, object]]:
    """A read that must be admitted at once (no queue): `STORE_BUSY` fails the test."""
    rows: list[dict[str, object]] = lake.read(request("SELECT 1 AS x", lake)).read_all().to_pylist()
    return rows


def guard_of(handle: ReadHandle) -> Guard:
    return handle._guard


def at_limit(guard: Guard) -> None:
    """What the guard's timer does when the limit passes."""
    guard._expire()


def fire(guard: Guard, stop: str) -> threading.Thread:
    """The limit (as the guard's timer would) or a cancel, from another thread."""
    return in_thread(functools.partial(at_limit, guard) if stop == "limit" else guard.cancel)


def in_thread(action: Callable[[], None]) -> threading.Thread:
    thread = threading.Thread(target=action, daemon=True)
    thread.start()
    return thread


@pytest.fixture
def single(location: Location) -> Iterator[Lake]:
    """One slot, no queue: a slot held by mistake shows as `STORE_BUSY`."""
    opened = open_with(location, max_concurrent=1)
    yield opened
    opened.close()


# --- 1. a failed read frees its slot before its error is seen ---------------------------------


@pytest.mark.parametrize("stop", ["limit", "cancel", "cancel in the same thread"])
def test_a_failed_read_frees_its_slot_before_its_error_is_seen(single: Lake, stop: str) -> None:
    expected = "STORE_TIMEOUT" if stop == "limit" else "STORE_CANCELLED"
    for _ in range(ROUNDS):
        handle = single.start_read(request(SLOW, single))
        if stop == "cancel in the same thread":
            handle.cancel()
            fired = None
        else:
            # Fired from another thread, racing with the first read below.
            fired = fire(guard_of(handle), stop)
        assert code_of(handle.reader.read_all) == expected
        assert one(single) == [{"x": 1}]
        if fired is not None:
            fired.join(WAIT)


def test_a_read_that_times_out_in_setup_frees_its_slot_first(
    single: Lake, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = lake_module.exact_select
    guards: list[Guard] = []
    original_init = Guard.__init__

    def keep(self: Guard, *args: object) -> None:
        original_init(self, *args)  # type: ignore[arg-type]
        guards.append(self)

    def expires_in_setup(*args: object) -> str | None:
        at_limit(guards[-1])  # the limit passes while the read is set up
        return original(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(Guard, "__init__", keep)
    monkeypatch.setattr(lake_module, "exact_select", expires_in_setup)
    for _ in range(ROUNDS):
        assert code_of(lambda: single.start_read(request("SELECT 2 AS y", single))) == (
            "STORE_TIMEOUT"
        )
        monkeypatch.setattr(lake_module, "exact_select", original)
        assert one(single) == [{"x": 1}]
        monkeypatch.setattr(lake_module, "exact_select", expires_in_setup)


# --- 2a. nothing runs before admission ---------------------------------------------------------


def test_a_read_queries_the_lake_only_once_admitted(
    location: Location, monkeypatch: pytest.MonkeyPatch
) -> None:
    lake = open_with(location, max_concurrent=1)
    try:
        events: list[str] = []
        recording = threading.Event()
        maintaining = threading.Event()
        original_one = lake._one
        original_cursor = lake._cursor
        original_acquire = lake._admission.acquire

        def one_recorded(
            sql: str, parameters: dict[str, object], cursor: object = None
        ) -> tuple[object, ...] | None:
            if recording.is_set() and not maintaining.is_set():
                events.append("query")
            return original_one(sql, parameters, cursor)  # type: ignore[arg-type]

        def cursor_recorded() -> object:
            if recording.is_set() and not maintaining.is_set():
                events.append("query")
            return original_cursor()

        def acquire_after_maintenance(deadline: float) -> None:
            if recording.is_set() and not maintaining.is_set():
                # Maintenance expires the read's snapshot while the read waits for its slot.
                maintaining.set()
                try:
                    lake.maintain(
                        dt.datetime.now(dt.UTC) + dt.timedelta(days=1),
                        dt.datetime.now(dt.UTC) - dt.timedelta(hours=1),
                    )
                finally:
                    maintaining.clear()
                events.append("acquire")
            original_acquire(deadline)

        monkeypatch.setattr(lake, "_one", one_recorded)
        monkeypatch.setattr(lake, "_cursor", cursor_recorded)
        monkeypatch.setattr(lake._admission, "acquire", acquire_after_maintenance)
        with lake.transaction() as transaction:
            transaction.execute(statement("CREATE TABLE t (n BIGINT)"))
        for round_ in range(ROUNDS):
            pinned = lake.current_snapshot().id
            with lake.transaction() as transaction:
                transaction.execute(statement(f"INSERT INTO t VALUES ({round_})"))
            events.clear()
            recording.set()
            try:
                code = code_of(
                    functools.partial(
                        lake.start_read, request("SELECT n FROM t", lake, None, pinned)
                    )
                )
            finally:
                recording.clear()
            # The snapshot expired while the read waited (#68 names it STORE_SNAPSHOT_EXPIRED).
            assert code == "STORE_SNAPSHOT_EXPIRED"
            assert events[0] == "acquire", events
        assert one(lake) == [{"x": 1}]
    finally:
        lake.close()


# --- 2b. the commit runs inside the transaction's limit ----------------------------------------


def test_a_block_that_outlives_its_limit_rolls_back(
    single: Lake, monkeypatch: pytest.MonkeyPatch
) -> None:
    with single.transaction() as transaction:
        transaction.execute(statement("CREATE TABLE t (n BIGINT)"))
    real = time.monotonic
    for round_ in range(ROUNDS):
        before = single.current_snapshot().id

        def outlives(round_: int = round_) -> None:
            with single.transaction() as transaction:
                transaction.execute(statement(f"INSERT INTO t VALUES ({round_})"))
                # The block ends one hour later, with no statement running.
                later = SimpleNamespace(monotonic=lambda: real() + 3600.0)
                monkeypatch.setattr(lake_module, "time", later)

        try:
            assert code_of(outlives) == "STORE_TIMEOUT"
        finally:
            monkeypatch.setattr(lake_module, "time", time)
        assert single.current_snapshot().id == before
        rows = single.read(request("SELECT count(*) AS c FROM t", single)).read_all().to_pylist()
        assert rows == [{"c": 0}]


# --- 3. waiting writers hold no slot ----------------------------------------------------------


class _WatchedLock:
    """The lake's writer lock, telling when a thread starts waiting for it."""

    def __init__(self, lock: threading.Lock) -> None:
        self._lock = lock
        self.waiting = threading.Event()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if self._lock.locked():
            self.waiting.set()
        return self._lock.acquire(blocking, timeout)

    def release(self) -> None:
        self._lock.release()


def test_a_waiting_writer_does_not_block_reads(
    location: Location, monkeypatch: pytest.MonkeyPatch
) -> None:
    lake = open_with(location, max_concurrent=2)
    watched = _WatchedLock(lake._write_lock)
    monkeypatch.setattr(lake, "_write_lock", watched)
    try:
        for _ in range(ROUNDS):
            watched.waiting.clear()
            holding = threading.Event()
            done = threading.Event()
            outcome: list[str] = []

            def writer_a(holding: threading.Event = holding, done: threading.Event = done) -> None:
                with lake.transaction():
                    holding.set()
                    done.wait(WAIT)

            def writer_b(outcome: list[str] = outcome) -> None:
                try:
                    with lake.transaction():
                        pass
                    outcome.append("committed")
                except StoreError as error:
                    outcome.append(error.code)

            a = in_thread(writer_a)
            assert holding.wait(WAIT)
            b = in_thread(writer_b)
            assert watched.waiting.wait(WAIT)
            assert one(lake) == [{"x": 1}]  # admitted at once, never STORE_BUSY
            done.set()
            a.join(WAIT)
            b.join(WAIT)
            assert outcome == ["committed"]
    finally:
        lake.close()


# --- 4. a paused stream is closed at its limit -------------------------------------------------


@pytest.mark.parametrize("stop", ["limit", "cancel"])
def test_a_paused_stream_is_closed_at_its_limit(single: Lake, stop: str) -> None:
    expected = "STORE_TIMEOUT" if stop == "limit" else "STORE_CANCELLED"
    for _ in range(ROUNDS):
        handle = single.start_read(request(LONG, single))
        first = handle.reader.read_next_batch()
        assert first.num_rows > 0
        # The consumer holds the batch; the limit (or a cancel) comes from another thread.
        guard = guard_of(handle)
        started = time.monotonic()
        fired = fire(guard, stop)
        fired.join(WAIT)
        # Firing closes the paused read and frees its slot before it returns.
        assert time.monotonic() - started < 0.2
        assert one(single) == [{"x": 1}]
        # The guard's repeated interrupts have stopped: no thread is left.
        assert guard._stopped.is_set()
        repeat = guard._repeat
        assert repeat is None or not repeat.is_alive()
        assert code_of(handle.reader.read_next_batch) == expected


def test_a_stream_still_streams_when_nothing_fires(single: Lake) -> None:
    handle = single.start_read(request("SELECT range AS n FROM range(5000)", single))
    total = 0
    for batch in handle.reader:
        total += batch.num_rows
    assert total == 5000
    assert one(single) == [{"x": 1}]


# --- 5. a caught interruption is still the transaction's outcome -------------------------------


@pytest.mark.parametrize("stop", ["cancel", "limit"])
def test_a_caught_interruption_decides_the_transaction(
    single: Lake, monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    expected = "STORE_TIMEOUT" if stop == "limit" else "STORE_CANCELLED"
    with single.transaction() as transaction:
        transaction.execute(statement("CREATE TABLE t (n BIGINT)"))
    original_init = Guard.__init__
    started: list[threading.Event] = []
    guards: list[Guard] = []

    def announced(self: Guard, *args: object) -> None:
        original_init(self, *args)  # type: ignore[arg-type]
        guards.append(self)
        if started:
            started[-1].set()

    monkeypatch.setattr(Guard, "__init__", announced)
    for round_ in range(ROUNDS):
        before = single.current_snapshot().id
        caught: list[str] = []

        def caught_inside(round_: int = round_, caught: list[str] = caught) -> None:
            with single.transaction() as transaction:
                transaction.execute(statement(f"INSERT INTO t VALUES ({round_})"))
                running = threading.Event()
                started.append(running)

                def stop_it() -> None:
                    assert running.wait(WAIT)
                    if stop == "cancel":
                        transaction.cancel()
                    else:
                        at_limit(guards[-1])  # the slow statement's guard

                stopper = in_thread(stop_it)
                try:
                    transaction.execute(statement(f"INSERT INTO t {SLOW}"))
                except StoreError as error:
                    caught.append(error.code)  # caught: the block goes on and ends normally
                stopper.join(WAIT)

        assert code_of(caught_inside) == expected
        assert caught == [expected]
        assert single.current_snapshot().id == before
        rows = single.read(request("SELECT count(*) AS c FROM t", single)).read_all().to_pylist()
        assert rows == [{"c": 0}]


# --- 6. cancel between statements ------------------------------------------------------------


def test_a_cancel_between_statements_rolls_back(single: Lake) -> None:
    with single.transaction() as transaction:
        transaction.execute(statement("CREATE TABLE t (n BIGINT)"))
    for round_ in range(ROUNDS):
        before = single.current_snapshot().id

        def cancelled_between(round_: int = round_) -> None:
            with single.transaction() as transaction:
                transaction.execute(statement(f"INSERT INTO t VALUES ({round_})"))
                # Cancelled from another thread while no statement runs; the block then ends.
                in_thread(transaction.cancel).join(WAIT)

        assert code_of(cancelled_between) == "STORE_CANCELLED"
        assert single.current_snapshot().id == before
