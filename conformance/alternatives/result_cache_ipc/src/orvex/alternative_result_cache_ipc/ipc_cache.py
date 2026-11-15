"""The alternative result cache (#486 G1): every guarantee of the result-cache contract, built
differently from the semantic module's cache.

- Each entry is kept as the Arrow IPC stream of its table, and decoded on every hit, so a hit's
  exactness goes through a real serialization.
- Recency is a counter: each put or hit gives the entry a new tick, pushed on a min-heap. The
  heap may hold stale ticks; eviction skips a tick that is no longer its entry's, and the heap
  is rebuilt once stale ticks outnumber the live ones.
- One lock guards every entry, the heap and the byte total.
"""

from __future__ import annotations

import heapq
import threading
from collections.abc import Callable
from dataclasses import dataclass

import pyarrow as pa
from orvex.contracts.envelope import CallEnvelope
from orvex.contracts.semantic.interface import (
    LARGEST_SHARE,
    Stored,
    Use,
    Withheld,
    entry_bytes,
)


@dataclass(frozen=True, slots=True)
class _Entry:
    ipc: bytes
    withheld: tuple[Withheld, ...]
    estimated: dict[str, int] | None
    born: float
    weight: int
    tick: int


def _encode(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as stream:
        stream.write_table(table)
    return sink.getvalue().to_pybytes()


def _decode(ipc: bytes) -> pa.Table:
    return pa.ipc.open_stream(pa.py_buffer(ipc)).read_all()


class IpcResultCache:
    """A result cache bounded to `max_bytes`, its entries aged on `clock`."""

    def __init__(self, max_bytes: int, clock: Callable[[], float]) -> None:
        if max_bytes < 1:
            raise ValueError("the bound is at least one byte")
        self._limit = max_bytes
        self._clock = clock
        self._held: dict[str, _Entry] = {}
        self._order: list[tuple[int, str]] = []
        self._counter = 0
        self._total = 0
        self._guard = threading.Lock()

    def get(self, envelope: CallEnvelope, use: Use) -> Stored | None:
        key = use.key
        if key is None:
            return None
        with self._guard:
            entry = self._held.get(key)
            if entry is None:
                return None
            if use.ttl is not None and self._clock() - entry.born >= use.ttl:
                self._forget(key)
                return None
            self._held[key] = self._touch(key, entry)
            ipc, withheld, estimated = entry.ipc, entry.withheld, entry.estimated
        return Stored(_decode(ipc), withheld, None if estimated is None else dict(estimated))

    def put(self, envelope: CallEnvelope, use: Use, stored: Stored) -> None:
        key = use.key
        weight = entry_bytes(stored)
        if key is None or weight * LARGEST_SHARE > self._limit - self._limit % LARGEST_SHARE:
            return
        ipc = _encode(stored.table)
        estimated = None if stored.estimated is None else dict(stored.estimated)
        with self._guard:
            self._forget(key)
            fresh = _Entry(ipc, tuple(stored.withheld), estimated, self._clock(), weight, 0)
            self._held[key] = self._touch(key, fresh)
            self._total += weight
            while self._total > self._limit:
                self._evict_oldest()

    def size_bytes(self, envelope: CallEnvelope) -> int:
        with self._guard:
            return self._total

    def entries(self, envelope: CallEnvelope) -> int:
        with self._guard:
            return len(self._held)

    def _touch(self, key: str, entry: _Entry) -> _Entry:
        self._counter += 1
        heapq.heappush(self._order, (self._counter, key))
        if len(self._order) > 2 * len(self._held) + 64:
            live = {k: e.tick for k, e in self._held.items()}
            live[key] = self._counter
            self._order = [(tick, k) for k, tick in live.items()]
            heapq.heapify(self._order)
        return _Entry(
            entry.ipc, entry.withheld, entry.estimated, entry.born, entry.weight, self._counter
        )

    def _evict_oldest(self) -> None:
        while self._order:
            tick, key = heapq.heappop(self._order)
            entry = self._held.get(key)
            if entry is not None and entry.tick == tick:
                self._forget(key)
                return

    def _forget(self, key: str) -> None:
        entry = self._held.pop(key, None)
        if entry is not None:
            self._total -= entry.weight
