"""A cancel for one running question (#445; Owner precision 2): the managed-lake reads it
started are interrupted, and a read it would start next is refused (`STORE_CANCELLED`).

The cancel is thread-safe and takes effect once: a read registered after it is cancelled at
once. Reads of other engines (an existing lake, ClickHouse) are not interrupted: their own
time limit (the role's `max_run_ms`) ends them, and their answer is discarded. So the question
says, once its engine is known, whether a cancel interrupts it (`known`): only then may its
slot be freed at the cancel; otherwise the slot is held until the execution really ends.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import pyarrow as pa
from orvex.store.errors import StoreError
from orvex.store.lake import Lake
from orvex.store.limits import ReadHandle


class Cancel:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._reads: list[ReadHandle] = []
        self._interruptible: bool | None = None  # unknown until the question's engine is

    @property
    def interruptible(self) -> bool:
        """Whether a cancel now stops the execution: its engine reads only the managed lake."""
        with self._lock:
            return self._interruptible is True

    def known(self, interruptible: bool) -> None:
        """The question's engine is known; raise `STORE_CANCELLED` when already cancelled, so
        an execution cancelled while it waited or planned stops before it reads."""
        with self._lock:
            self._interruptible = interruptible
            cancelled = self._cancelled
        if cancelled:
            raise StoreError("STORE_CANCELLED", "The question was cancelled.", {})

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            reads, self._reads = self._reads, []
        for handle in reads:
            handle.cancel()

    def reading(self, lake: Lake) -> Callable[[Any], pa.RecordBatchReader]:
        """`lake.read`, each read interruptible by this cancel."""

        def read(request: Any) -> pa.RecordBatchReader:  # noqa: ANN401 - a `ReadRequest`
            if self.cancelled:
                raise StoreError("STORE_CANCELLED", "The question was cancelled.", {})
            handle = lake.start_read(request)
            with self._lock:
                late = self._cancelled
                if not late:
                    self._reads.append(handle)
            if late:
                handle.cancel()
            return handle.reader

        return read
