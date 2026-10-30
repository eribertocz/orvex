"""#77, Reviewer pass 1 on #159: a read's guard may fire at any point of the reading thread.

The read runs `Lake.start_read` unchanged on a lake whose catalog plumbing (snapshot check,
pinning, cursor) is plain DuckDB, so no DuckDB extension is needed. The guard fires right after
the reading thread's n-th look at `reason`, for every n up to past the second batch: each check
point of the read is hit, deterministically.
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from typing import ClassVar, cast

import duckdb
import pytest
from orvex.contracts.store.v1 import ReadRequest
from orvex.store import lake as lake_module
from orvex.store.errors import StoreError
from orvex.store.lake import Admission, Guard, Lake, ReadHandle

LONG = "SELECT range AS n FROM range(100000000)"
LOOKS = range(1, 9)


class _FiresOnLook(Guard):
    """Fires (limit or cancel) right after the reading thread's `fire_on`-th look at `reason`."""

    reader: threading.Thread | None = None
    fire_on = 0
    fire_with = "limit"
    fired: ClassVar[list[threading.Thread]] = []

    def __init__(self, *args: object) -> None:
        self._reason: str | None = None
        self._looks = 0
        super().__init__(*args)  # type: ignore[arg-type]

    @property
    def reason(self) -> str | None:
        value = self._reason
        if threading.current_thread() is _FiresOnLook.reader and value is None:
            self._looks += 1
            if self._looks == _FiresOnLook.fire_on:
                # From another thread, as the timer or a caller does. When the reading thread
                # holds the read's lock here, the guard waits for it, exactly as a real timer
                # would; otherwise it has fired before the read goes on.
                fire = self._expire if _FiresOnLook.fire_with == "limit" else self.cancel
                thread = threading.Thread(target=fire)
                _FiresOnLook.fired.append(thread)
                thread.start()
                thread.join(timeout=0.2)
        return value

    @reason.setter
    def reason(self, value: str | None) -> None:
        self._reason = value


def _unpinned(tree: dict[str, object], snapshot: int, cursor: object) -> str:
    """The read's SQL back from its checked tree, not pinned (no lake here)."""
    row = duckdb.execute("SELECT json_deserialize_sql($tree)", {"tree": json.dumps(tree)})
    return str(row.fetchone()[0])  # type: ignore[index]


def _lake(connection: duckdb.DuckDBPyConnection) -> Lake:
    lake = Lake.__new__(Lake)
    lake._admission = Admission(1, 0)
    lake._limits = SimpleNamespace(default_timeout_ms=600_000)  # type: ignore[assignment]
    lake._table_functions = frozenset()
    lake._snapshot = lambda snapshot_id, cursor=None: object()  # type: ignore[method-assign,assignment,return-value]
    lake._pinned = _unpinned  # type: ignore[method-assign]
    lake._cursor = connection.cursor  # type: ignore[method-assign]
    return lake


def _slot_is_free(lake: Lake) -> bool:
    try:
        lake._admission.acquire(time.monotonic())
    except StoreError:
        return False
    lake._admission.release()
    return True


@pytest.mark.parametrize("stop", ["limit", "cancel"])
@pytest.mark.parametrize("look", LOOKS)
def test_a_guard_firing_at_any_point_frees_the_slot_before_the_consumer_waits(
    monkeypatch: pytest.MonkeyPatch, look: int, stop: str
) -> None:
    expected = "STORE_TIMEOUT" if stop == "limit" else "STORE_CANCELLED"
    monkeypatch.setattr(lake_module, "Guard", _FiresOnLook)
    monkeypatch.setattr(_FiresOnLook, "reader", threading.current_thread())
    monkeypatch.setattr(_FiresOnLook, "fire_on", look)
    monkeypatch.setattr(_FiresOnLook, "fire_with", stop)
    monkeypatch.setattr(_FiresOnLook, "fired", [])
    connection = duckdb.connect()
    lake = _lake(connection)
    request = ReadRequest(
        kind="read", sql=LONG, parameters=[], snapshot=1, batch_size=1024, timeout_ms=None
    )
    code = None
    handle: ReadHandle | None = None
    try:
        handle = lake.start_read(request)
        for _ in range(3):
            handle.reader.read_next_batch()
            for thread in _FiresOnLook.fired:
                thread.join(5)
            guard = cast(_FiresOnLook, handle._guard)
            if guard._reason is not None:
                # Fired while this batch was produced or handed over: the consumer holds it,
                # and the read is already closed.
                assert _slot_is_free(lake)
                assert guard._stopped.is_set()
                assert guard._repeat is None or not guard._repeat.is_alive()
    except StoreError as error:
        code = error.code
    if code is None:
        assert handle is not None
        with pytest.raises(StoreError) as raised:
            handle.reader.read_next_batch()
        code = raised.value.code
    assert code == expected
    assert _slot_is_free(lake)
    connection.close()
