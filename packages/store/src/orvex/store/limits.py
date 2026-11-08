"""The lake's resource limits and how they are enforced: the limits themselves, admission slots,
and the deadline and cancel that interrupt a DuckDB statement.
"""

from __future__ import annotations

import collections
import contextlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import duckdb
import pyarrow as pa
from orvex.store.errors import StoreError

MAX_TIMEOUT_MS = 3_600_000


@dataclass(frozen=True, slots=True)
class Limits:
    """Resource limits of a lake; every field is required (law 7).

    `max_concurrent` statements (reads in progress and open transactions) run at once; up to
    `queue_size` more wait in arrival order; `default_timeout_ms` applies when a request sets no
    `timeout_ms`.
    """

    memory_limit: str | None
    threads: int | None
    max_concurrent: int | None = None
    queue_size: int | None = None
    default_timeout_ms: int | None = None


def _require_limits(limits: Limits) -> tuple[str, int, int, int]:
    """The memory limit, thread count, concurrency limit and queue size, once every limit is set
    and within its range (law 7); `STORE_LIMITS_REQUIRED` otherwise."""
    if (
        not limits.memory_limit
        or not limits.threads
        or limits.max_concurrent is None
        or limits.max_concurrent < 1
        or limits.queue_size is None
        or limits.queue_size < 0
        or limits.default_timeout_ms is None
        or not 1 <= limits.default_timeout_ms <= MAX_TIMEOUT_MS
    ):
        raise StoreError(
            "STORE_LIMITS_REQUIRED",
            "The lake needs a memory limit, a thread count, a concurrency limit, a queue "
            "size and a default timeout.",
            {},
        )
    return str(limits.memory_limit), int(limits.threads), limits.max_concurrent, limits.queue_size


class Admission:
    """At most `capacity` holders at once, a bounded first-come first-served queue behind them."""

    def __init__(self, capacity: int, queue_size: int) -> None:
        self._capacity = capacity
        self._queue_size = queue_size
        self._running = 0
        self._waiting: collections.deque[object] = collections.deque()
        self._condition = threading.Condition()

    def acquire(self, deadline: float) -> None:
        """Wait for a slot until `deadline` (time.monotonic); STORE_BUSY or STORE_TIMEOUT."""
        with self._condition:
            if self._running < self._capacity and not self._waiting:
                self._running += 1
                return
            if len(self._waiting) >= self._queue_size:
                raise StoreError(
                    "STORE_BUSY",
                    "The lake is at capacity and its queue is full; try again shortly.",
                    {"running": str(self._running), "queued": str(len(self._waiting))},
                )
            ticket = object()
            self._waiting.append(ticket)
            try:
                while not (self._waiting[0] is ticket and self._running < self._capacity):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise StoreError(
                            "STORE_TIMEOUT",
                            "The request timed out while waiting for a free slot.",
                            {},
                        )
                    self._condition.wait(remaining)
                self._running += 1
            finally:
                self._waiting.remove(ticket)
                self._condition.notify_all()

    def release(self) -> None:
        with self._condition:
            self._running -= 1
            self._condition.notify_all()


INTERRUPT_REPEAT_SECONDS = 0.05


class Guard:
    """Interrupts a DuckDB cursor at a deadline or on request, and remembers why."""

    def __init__(self, cursor: duckdb.DuckDBPyConnection, deadline: float) -> None:
        self._cursor = cursor
        self.reason: str | None = None
        self.deadline = deadline
        # Called after an interruption, for an owner that must also release what is idle.
        self.on_fire: Callable[[], None] | None = None
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._repeat: threading.Thread | None = None
        self._timer = threading.Timer(max(0.0, deadline - time.monotonic()), self._expire)
        self._timer.daemon = True
        self._timer.start()

    def _expire(self) -> None:
        self._interrupt("STORE_TIMEOUT")

    def cancel(self) -> None:
        self._interrupt("STORE_CANCELLED")

    def _interrupt(self, reason: str) -> None:
        with self._lock:
            if self.reason is not None:
                return
            self.reason = reason
        if self.on_fire is not None:
            self.on_fire()
        # An interrupt sent while the cursor is between two pieces of work can be lost, so it is
        # repeated until the owner stops the guard.
        with self._lock:
            if self._stopped.is_set():
                return
            self._repeat = threading.Thread(target=self._keep_interrupting, daemon=True)
            self._repeat.start()

    def _keep_interrupting(self) -> None:
        while True:
            # A statement that already ended has a closed cursor: there is nothing to stop.
            with contextlib.suppress(duckdb.Error):
                self._cursor.interrupt()
            if self._stopped.wait(INTERRUPT_REPEAT_SECONDS):
                return

    def stop(self) -> None:
        """No interrupt reaches the cursor once this returns."""
        with self._lock:
            self._stopped.set()
            repeat = self._repeat
        self._timer.cancel()
        if repeat is not None:
            repeat.join()

    def error(self, cause: Exception | None = None) -> StoreError:
        """The store error for an interruption (or `cause` when it was not interrupted)."""
        if self.reason == "STORE_TIMEOUT" or (
            self.reason is None and time.monotonic() >= self.deadline
        ):
            return StoreError("STORE_TIMEOUT", "The statement exceeded its time limit.", {})
        if self.reason == "STORE_CANCELLED":
            return StoreError("STORE_CANCELLED", "The statement was cancelled.", {})
        return StoreError(
            "STORE_QUERY_FAILED", "DuckDB could not run the statement.", {"duckdb": str(cause)}
        )


@dataclass(slots=True)
class ReadHandle:
    """A running read: its Arrow stream and a way to cancel it from another thread."""

    reader: pa.RecordBatchReader
    _guard: Guard

    def cancel(self) -> None:
        self._guard.cancel()


def _bounded[T](cursor: duckdb.DuckDBPyConnection, deadline: float, work: Callable[[], T]) -> T:
    """`work`, a few queries on `cursor`, interrupted at `deadline` (`STORE_TIMEOUT`)."""
    guard = Guard(cursor, deadline)
    try:
        if guard.reason is not None:
            raise guard.error()
        return work()
    except duckdb.Error as error:
        raise guard.error(error) from error
    finally:
        guard.stop()
