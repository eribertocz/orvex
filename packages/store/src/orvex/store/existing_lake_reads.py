"""Reads of an existing DuckLake at one snapshot (P2-30 PR 3, #399; ADR-0027 §3, §5, §7).

`ExistingLake` attaches the lake and keeps it isolated; this module is how anything is asked
of the attached instance afterwards. Every call takes the instance's one connection in turn
(`ExistingLake.busy`), on a cursor of its own, under a deadline at which DuckDB is interrupted
(`STORE_TIMEOUT`); the instance's `memory_limit` and `threads` bound the rest (law 7).

- `read`: one statement of the semantic adapter, checked by the statement guard
  (`existing_lake_guard`), at the snapshot it names. In the same read transaction the catalog
  must still list that snapshot with the commit time it was verified with; a catalog restored
  or replaced since gives `STORE_SOURCE_INVALID` (`reason: snapshot_changed`) and no row. The
  result has the managed lake's Arrow form (zones named `UTC`, DuckDB types in the metadata,
  `UHUGEINT` as exact text), so both routes give the same values and types.
- `table_columns` and `views`: the lake's tables at a snapshot, as the semantic model is compiled
  against them, and its views, which no dataset may read.
- `latest` and `verify`: `ExistingLake`'s, taken in turn with the reads.

The result of a read is held in memory before it is returned: the semantic engine reads every
batch before it answers anyway (never a partial result).
"""

from __future__ import annotations

import datetime as dt
import json
import time
from collections.abc import Callable
from typing import Any

import duckdb
import pyarrow as pa
from orvex.contracts.store.v1 import ExistingLakeAttachment, ReadRequest, Snapshot, TablesResult
from orvex.store.arrow_exact import _utc_schema, _with_duckdb_types, exact_select
from orvex.store.catalog_tables import one_row, tables_at, tables_with_columns
from orvex.store.errors import StoreError
from orvex.store.existing_lake import _EPOCH, LAKE, ExistingLake
from orvex.store.existing_lake_guard import guarded
from orvex.store.limits import Guard
from orvex.store.nested_types import refuse_nested_uhugeint
from orvex.store.sql_text import literal as _quote

SNAPSHOT_CHANGED = "snapshot_changed"


class LakeReads:
    """What may be asked of one attached existing lake, each call in turn and under a deadline
    (`timeout_ms`, or a read's own)."""

    def __init__(self, lake: ExistingLake, *, timeout_ms: int) -> None:
        if timeout_ms < 1:
            raise StoreError("STORE_LIMITS_REQUIRED", "The lake's reads need a time limit.", {})
        self._lake = lake
        self._timeout_ms = timeout_ms

    @property
    def name(self) -> str:
        return self._lake.name

    def read(self, request: ReadRequest, committed_at: dt.datetime) -> pa.RecordBatchReader:
        """`request`'s statement at `request.snapshot`, which must still have been committed at
        `committed_at`."""
        timeout = request.timeout_ms if request.timeout_ms is not None else self._timeout_ms

        def work(cursor: duckdb.DuckDBPyConnection) -> pa.Table:
            cursor.execute("BEGIN TRANSACTION")
            self._confirm(cursor, request.snapshot, committed_at)
            tables = tables_at(cursor, self._lake._metadata, request.snapshot)
            tree, values = guarded(
                request.sql,
                request.parameters,
                tables=tables,
                snapshot=request.snapshot,
                table_functions=self._lake.table_functions,
            )
            found = one_row(
                cursor, "SELECT json_deserialize_sql($tree)", {"tree": json.dumps(tree)}
            )
            sql = str(found[0]) if found else ""
            relation = cursor.sql(sql, params=values) if values else cursor.sql(sql)
            refuse_nested_uhugeint(list(relation.columns), relation.types)
            types = [str(kind) for kind in relation.types]
            exact = exact_select(sql, list(relation.columns), types)
            if exact is not None:
                relation = cursor.sql(exact, params=values) if values else cursor.sql(exact)
            table = relation.to_arrow_table()
            cursor.execute("COMMIT")
            schema = _with_duckdb_types(_utc_schema(table.schema), types)
            # Cast to the zones' names, then the schema with each column's DuckDB type.
            return pa.Table.from_arrays(table.cast(schema).columns, schema=schema)

        table = self._run(work, timeout)
        return pa.RecordBatchReader.from_batches(
            table.schema, table.to_batches(max_chunksize=request.batch_size)
        )

    def table_columns(self, snapshot: Snapshot) -> TablesResult:
        """The lake's tables alive at `snapshot` (still committed at its time), with columns."""

        def work(cursor: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
            cursor.execute("BEGIN TRANSACTION")
            self._confirm(cursor, snapshot.id, snapshot.committed_at)
            found = tables_with_columns(cursor, self._lake._metadata, LAKE, snapshot.id)
            cursor.execute("COMMIT")
            return found

        tables = self._run(work, self._timeout_ms)
        return TablesResult.model_validate(
            {"kind": "tables_result", "snapshot": snapshot.id, "tables": tables}
        )

    def views(self, snapshot: Snapshot) -> frozenset[str]:
        """`schema.view` of every view of the lake alive at `snapshot`, lower-cased."""
        meta = self._lake._metadata

        def work(cursor: duckdb.DuckDBPyConnection) -> frozenset[str]:
            rows = cursor.execute(
                f"SELECT lower(s.schema_name) || '.' || lower(v.view_name) "
                f"FROM {meta}.ducklake_view AS v JOIN {meta}.ducklake_schema AS s "
                "USING (schema_id) WHERE v.begin_snapshot <= $n "
                "AND (v.end_snapshot IS NULL OR v.end_snapshot > $n)",
                {"n": snapshot.id},
            ).fetchall()
            return frozenset(str(row[0]) for row in rows)

        return self._run(work, self._timeout_ms)

    def committed_at(self, snapshot: int) -> dt.datetime | None:
        """When the catalog says `snapshot` was committed, or None when it lists no such id."""

        def work(cursor: duckdb.DuckDBPyConnection) -> dt.datetime | None:
            found = one_row(
                cursor,
                f"SELECT epoch_us(snapshot_time) FROM ducklake_snapshots({_quote(LAKE)}) "
                "WHERE snapshot_id = $id",
                {"id": snapshot},
            )
            return None if found is None else _EPOCH + dt.timedelta(microseconds=int(str(found[0])))

        return self._run(work, self._timeout_ms)

    def latest(self) -> Snapshot:
        return self._locked(self._lake.latest_snapshot)

    def verify(self, snapshot: Snapshot, current: ExistingLakeAttachment) -> Snapshot:
        return self._locked(lambda: self._lake.verify(snapshot, current))

    # --- in turn, under a deadline -------------------------------------------------------------

    def _locked[T](self, work: Callable[[], T]) -> T:
        deadline = time.monotonic() + self._timeout_ms / 1000
        if not self._lake.busy.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise StoreError("STORE_TIMEOUT", "The lake stayed busy past the time limit.", {})
        try:
            result: T = work()
            return result
        finally:
            self._lake.busy.release()

    def _run[T](self, work: Callable[[duckdb.DuckDBPyConnection], T], timeout_ms: int) -> T:
        deadline = time.monotonic() + timeout_ms / 1000
        if not self._lake.busy.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise StoreError("STORE_TIMEOUT", "The statement exceeded its time limit.", {})
        try:
            cursor = self._lake._connection.cursor()
            guard = Guard(cursor, deadline)
            try:
                if guard.reason is not None:
                    raise guard.error()
                result: T = work(cursor)
                if guard.reason is not None:
                    raise guard.error()
                return result
            except duckdb.Error as error:
                raise guard.error(error) from error
            finally:
                guard.stop()
                cursor.close()
        finally:
            self._lake.busy.release()

    def _confirm(
        self, cursor: duckdb.DuckDBPyConnection, snapshot: int, committed_at: dt.datetime
    ) -> None:
        """The catalog, in this transaction, still lists `snapshot` with its commit time."""
        since_epoch = (committed_at - _EPOCH) // dt.timedelta(microseconds=1)
        found = one_row(
            cursor,
            f"SELECT epoch_us(snapshot_time) FROM ducklake_snapshots({_quote(LAKE)}) "
            "WHERE snapshot_id = $id",
            {"id": snapshot},
        )
        if found is None or int(str(found[0])) != since_epoch:
            raise StoreError(
                "STORE_SOURCE_INVALID",
                f"Connection {self._lake.name}: the catalog no longer lists the snapshot read "
                "with its commit time; it was restored or replaced.",
                {"connection": self._lake.name, "reason": SNAPSHOT_CHANGED},
            )
