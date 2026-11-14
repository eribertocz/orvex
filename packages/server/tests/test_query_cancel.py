"""#445 AC3b: a cancel interrupts the managed-lake reads its question started, refuses the
reads it would start next, and cancels at once a read that registers as it is cancelled."""

from __future__ import annotations

import contextlib
import threading
from typing import Any

import pytest
from orvex.server.query_cancel import Cancel
from orvex.store.errors import StoreError


class Handle:
    def __init__(self) -> None:
        self.cancels = 0
        self.reader = object()

    def cancel(self) -> None:
        self.cancels += 1


class Lake:
    def __init__(self) -> None:
        self.started: list[Handle] = []

    def start_read(self, request: Any) -> Handle:  # noqa: ANN401
        handle = Handle()
        self.started.append(handle)
        return handle


def test_a_cancel_interrupts_the_reads_started() -> None:
    lake, cancel = Lake(), Cancel()
    read = cancel.reading(lake)  # type: ignore[arg-type]
    assert read("one") is lake.started[0].reader
    read("two")
    cancel.cancel()
    assert [h.cancels for h in lake.started] == [1, 1]
    cancel.cancel()  # a second cancel interrupts nothing again
    assert [h.cancels for h in lake.started] == [1, 1]


def test_a_read_after_the_cancel_is_refused() -> None:
    lake, cancel = Lake(), Cancel()
    cancel.cancel()
    with pytest.raises(StoreError) as caught:
        cancel.reading(lake)("one")  # type: ignore[arg-type]
    assert caught.value.code == "STORE_CANCELLED"
    assert lake.started == []


def race() -> Lake:
    """Four reads and a cancel, released at once."""
    lake, cancel = Lake(), Cancel()
    read = cancel.reading(lake)  # type: ignore[arg-type]
    barrier = threading.Barrier(5)

    def reading() -> None:
        barrier.wait(5)
        with contextlib.suppress(StoreError):
            read("x")

    def cancelling() -> None:
        barrier.wait(5)
        cancel.cancel()

    threads = [threading.Thread(target=reading) for _ in range(4)]
    threads.append(threading.Thread(target=cancelling))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    return lake


def test_a_read_registering_while_cancelled_is_cancelled_once() -> None:
    """Reads and cancels race from many threads: every read started is cancelled exactly
    once, whichever came first."""
    for _ in range(50):
        assert all(h.cancels == 1 for h in race().started)


def test_a_question_learns_whether_a_cancel_interrupts_it() -> None:
    cancel = Cancel()
    assert not cancel.interruptible  # unknown: not interruptible
    cancel.known(False)
    assert not cancel.interruptible
    managed = Cancel()
    managed.known(True)
    assert managed.interruptible


def test_a_question_cancelled_before_its_engine_is_known_stops_there() -> None:
    cancel = Cancel()
    cancel.cancel()
    with pytest.raises(StoreError) as caught:
        cancel.known(True)
    assert caught.value.code == "STORE_CANCELLED"
