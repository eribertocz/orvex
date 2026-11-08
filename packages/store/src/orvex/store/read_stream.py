"""A lake read's Arrow stream, once its cursor is admitted, guarded and its SQL pinned: who owns
the cursor at each phase of the read, and its admission slot released exactly once (#77, #159).
"""

from __future__ import annotations

import threading
import weakref
from collections.abc import Callable, Iterator

import duckdb
import pyarrow as pa
from orvex.store import nested_types
from orvex.store.arrow_exact import _utc_schema, _with_duckdb_types, exact_select
from orvex.store.errors import StoreError
from orvex.store.limits import Guard, ReadHandle


def stream_read(
    cursor: duckdb.DuckDBPyConnection,
    guard: Guard,
    sql: str,
    parameters: dict[str, str | None],
    batch_size: int,
    release: Callable[[], None],
) -> ReadHandle:
    """The read of `sql` on `cursor` (pinned, under `guard`), bound now and computed batch by
    batch as its stream is read; `release` frees its admission slot, once, when it ends."""
    # Who owns the cursor: the calling thread while it sets the read up ("setup"), nobody
    # while the stream waits for its first read ("idle") or while the consumer holds a batch
    # ("paused"), the reading thread while DuckDB computes a batch ("computing"). A read
    # nobody owns is closed by the guard when it fires (or by collection of a dropped
    # reader); the owner closes it otherwise. The slot is released exactly once, and before
    # anyone can see the read's error (#77).
    state = threading.Lock()
    phase = "setup"
    released = threading.Event()

    def close() -> None:
        """Called once, by whoever moved the read to "finished"."""
        try:
            guard.stop()
            cursor.close()
        finally:
            release()
            released.set()

    def finish() -> None:
        nonlocal phase
        with state:
            already = phase == "finished"
            phase = "finished"
        if already:
            # Closed by another thread: its error is seen only once the slot is free.
            released.wait()
            return
        close()

    def release_if_idle() -> None:
        nonlocal phase
        # Checked and claimed in one step, so the reading thread cannot take the cursor back
        # in between (#159, Reviewer pass 1).
        with state:
            unowned = phase in {"idle", "paused"}
            if unowned:
                phase = "finished"
        if unowned:
            close()

    guard.on_fire = release_if_idle

    try:
        # Binding is quick and returns before any row is computed; the work runs on the first
        # read of the stream, where a timeout or a cancel can still interrupt it.
        relation = cursor.sql(sql, params=parameters)
        names = list(relation.columns)
        nested_types.refuse_nested_uhugeint(names, relation.types)
        types = [str(kind) for kind in relation.types]
        exact = exact_select(sql, names, types)
        if exact is not None:
            relation = cursor.sql(exact, params=parameters)
        empty = relation.limit(0).to_arrow_table()
        schema = _with_duckdb_types(_utc_schema(empty.schema), types)
    except duckdb.Error as error:
        finish()
        if isinstance(error, duckdb.InterruptException) or guard.reason:
            raise guard.error(error) from error
        raise StoreError(
            "STORE_QUERY_FAILED", "DuckDB could not run the read.", {"duckdb": str(error)}
        ) from error
    except BaseException:
        finish()
        raise
    if guard.reason is not None:
        # The limit expired or a cancel came while the read was being set up.
        finish()
        raise guard.error()
    with state:
        phase = "idle"
        fired = guard.reason is not None
    if fired:
        # It fired between the check above and now, and found the read still being set up.
        release_if_idle()

    def take() -> None:
        """The reading thread takes the cursor back; a read closed meanwhile raises its
        error, once its slot is free."""
        nonlocal phase
        with state:
            closed = phase == "finished"
            if not closed:
                phase = "computing"
        if closed:
            finish()  # waits until the slot is released
            raise guard.error()

    def batches() -> Iterator[pa.RecordBatch]:
        # The cursor and the admission slot live as long as the stream.
        nonlocal phase
        take()
        try:
            if guard.reason is not None:
                raise guard.error()
            stream = relation.to_arrow_reader(batch_size)
            while True:
                try:
                    batch = stream.read_next_batch()
                except StopIteration:
                    return
                # Checked and paused in one step: a guard that fires before it is seen here,
                # one that fires after finds the read paused and closes it (#159, Reviewer
                # pass 1).
                with state:
                    fired = guard.reason is not None
                    if not fired and phase == "computing":
                        phase = "paused"
                if fired:
                    raise guard.error()
                # While the consumer holds this batch, the guard may close the read.
                yield batch if batch.schema == schema else batch.cast(schema)
                take()
                if guard.reason is not None:
                    raise guard.error()
        except (OSError, pa.ArrowException, duckdb.Error) as error:
            if guard.reason is not None or isinstance(error, duckdb.InterruptException):
                raise guard.error(error) from error
            raise StoreError(
                "STORE_QUERY_FAILED",
                "DuckDB failed while streaming the read.",
                {"duckdb": str(error)},
            ) from error
        finally:
            finish()

    reader = pa.RecordBatchReader.from_batches(schema, batches())
    # A stream dropped before its first batch never runs its generator, so its slot is
    # released when the reader is collected (a started stream releases it when closed).
    weakref.finalize(reader, release_if_idle)
    return ReadHandle(reader=reader, _guard=guard)
