"""A write transaction of the managed lake: its statements, Arrow loads and source landings, and
the catalog checks and token around its commit.
"""

from __future__ import annotations

import contextlib
import math
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence

import duckdb
import pyarrow as pa
from orvex.contracts.store.v1 import CommitResult, Parameter, Snapshot, StatementRequest
from orvex.store import source_worker
from orvex.store.catalog_values import _single_value
from orvex.store.ctrl_c import _on_ctrl_c, _user_interrupt
from orvex.store.errors import StoreError
from orvex.store.lake_names import LAKE, METADATA
from orvex.store.limits import Guard
from orvex.store.parameters import _bound, _typed
from orvex.store.sql_guard import WRITE_STATEMENTS, _created_kind_allowed, _single_statement
from orvex.store.sql_parser import _plain
from orvex.store.sql_text import identifier as _identifier

# A DuckDB type name as `load_arrow` accepts it: a name, optionally with numeric arguments.
_ARROW_LOAD_TYPE = re.compile(r"[A-Z][A-Z0-9_]*(?: [A-Z][A-Z0-9_]*)*(?:\([0-9]+(?:,[0-9]+)?\))?")


def _tag(cursor: duckdb.DuckDBPyConnection, token: str) -> None:
    """Mark the transaction's snapshot with `token` (DuckLake's commit information): no user
    data and no secret, only a random id. SQL from callers cannot set it
    (`DUCKLAKE_TABLE_MACROS`)."""
    cursor.execute(
        "CALL ducklake_set_commit_message($lake, '', 'transaction', extra_info => $token)",
        {"lake": LAKE, "token": token},
    )


def _commit(cursor: duckdb.DuckDBPyConnection) -> None:
    """Commit the write transaction (a function of its own, so tests can stand in a slow one)."""
    cursor.commit()


def _unpinnable_objects(cursor: duckdb.DuckDBPyConnection) -> int:
    """Views in the lake and user macros anywhere, read inside the open transaction.

    Both can read lake tables from inside a function call or a view body, where a read's
    snapshot rewrite cannot reach them, so they are not allowed to exist.
    """
    result = cursor.execute(
        "SELECT (SELECT count(*) FROM duckdb_views() "
        "        WHERE lower(database_name) = $lake AND NOT internal) "
        "     + (SELECT count(*) FROM duckdb_functions() "
        "        WHERE NOT internal AND function_type IN ('macro', 'table_macro'))",
        {"lake": LAKE},
    )
    row = result.fetchone()
    result.fetchone()  # reaches the end of the one-row result, so the cursor is free again
    return int(str(row[0])) if row else 0


def _outside_inventory(cursor: duckdb.DuckDBPyConnection) -> str:
    """Every user object outside the lake: tables, views, sequences, types, macros, secrets."""
    return _single_value(
        cursor,
        "SELECT string_agg(item, '|' ORDER BY item) FROM ("
        " SELECT 'table:' || database_name || '.' || schema_name || '.' || table_name AS item"
        " FROM duckdb_tables() WHERE lower(database_name) NOT IN ($lake, $metadata)"
        " UNION ALL SELECT 'view:' || database_name || '.' || schema_name || '.' || view_name"
        " FROM duckdb_views() WHERE NOT internal AND lower(database_name) NOT IN ($lake, $metadata)"
        " UNION ALL SELECT 'sequence:' || database_name || '.' || schema_name || '.'"
        " || sequence_name"
        " FROM duckdb_sequences() WHERE lower(database_name) NOT IN ($lake, $metadata)"
        " UNION ALL SELECT 'type:' || database_name || '.' || schema_name || '.' || type_name"
        " FROM duckdb_types() WHERE NOT internal AND lower(database_name) NOT IN ($lake, $metadata)"
        " UNION ALL SELECT 'secret:' || name FROM duckdb_secrets()"
        ")",
        {"lake": LAKE, "metadata": METADATA},
    )


class Transaction:
    """A write transaction; `result` is set when the block commits."""

    def __init__(
        self,
        cursor: duckdb.DuckDBPyConnection,
        table_functions: frozenset[str],
        timeout_ms: Callable[[int | None], int],
        deadline: float = math.inf,
        land: Callable[..., source_worker.SourceStream] | None = None,
    ) -> None:
        self._cursor = cursor
        # The transaction's own limit: no statement of it runs past it (#160).
        self._deadline = deadline
        self._table_functions = table_functions
        self._start_source = land
        self._timeout_ms = timeout_ms
        self._guard: Guard | None = None
        self._lock = threading.Lock()
        self.cancelled = False
        # The first statement interrupted by its time limit or a cancel: the transaction's
        # outcome, even if the block catches the error (#77).
        self.interrupted: StoreError | None = None
        self.result: CommitResult | None = None
        # The snapshot the transaction reads (#255): set when it begins.
        self.started: Snapshot | None = None

    def _interrupted(self, error: StoreError) -> StoreError:
        if error.code in {"STORE_TIMEOUT", "STORE_CANCELLED"} and self.interrupted is None:
            self.interrupted = error
        return error

    def _statement_deadline(self, timeout_ms: int | None) -> float:
        """A statement's deadline: its own limit, capped by the transaction's (#160)."""
        return min(time.monotonic() + self._timeout_ms(timeout_ms) / 1000, self._deadline)

    def bounded[T](self, work: Callable[[], T]) -> T:
        """`work`, the store's own queries on this transaction's cursor, under the transaction's
        limit and its cancel, like a statement (#160)."""
        guard = Guard(self._cursor, self._deadline)
        with self._lock:
            if self.cancelled:
                guard.stop()
                raise StoreError("STORE_CANCELLED", "The transaction was cancelled.", {})
            self._guard = guard
        try:
            if guard.reason is not None:
                raise guard.error()
            return work()
        except duckdb.Error as error:
            raise self._interrupted(guard.error(error)) from error
        finally:
            guard.stop()
            self._guard = None

    def cancel(self) -> None:
        """Cancel from any thread: a running statement is interrupted, and the block rolls back."""
        with self._lock:
            self.cancelled = True
            guard = self._guard
        if guard is not None:
            guard.cancel()

    def value(self, request: StatementRequest) -> str | None:
        """The first column of the first row of one SELECT run inside this transaction, as text.

        For small checks between statements (counts, existence): it sees the transaction's
        uncommitted changes. `None` when there is no row or the value is NULL.
        """
        sql = _plain(request.sql)
        statement = _single_statement(sql, self._table_functions)
        if statement.type != duckdb.StatementType.SELECT:
            raise StoreError(
                "STORE_STATEMENT_INVALID",
                "A transaction value must come from one SELECT.",
                {"statement": statement.type.name},
            )
        parameters = _bound(statement, request.parameters)
        deadline = self._statement_deadline(request.timeout_ms)
        guard = Guard(self._cursor, deadline)
        with self._lock:
            if self.cancelled:
                guard.stop()
                raise StoreError("STORE_CANCELLED", "The transaction was cancelled.", {})
            self._guard = guard
        typed = _typed(sql, request.parameters)
        try:
            result = self._cursor.execute(
                f"SELECT CAST(#1 AS VARCHAR) FROM ({typed}) LIMIT 1", parameters
            )
            row = result.fetchone()
            result.fetchone()  # reaches the end of the result, so the cursor is free again
        except duckdb.Error as error:
            raise self._interrupted(guard.error(error)) from error
        except RuntimeError as error:
            if _user_interrupt(error):
                raise KeyboardInterrupt from error
            raise
        finally:
            guard.stop()
            self._guard = None
        return None if not row or row[0] is None else str(row[0])

    def execute(self, request: StatementRequest) -> None:
        sql = _plain(request.sql)
        statement = _single_statement(sql, self._table_functions)
        if statement.type not in WRITE_STATEMENTS or not _created_kind_allowed(sql, statement):
            raise StoreError(
                "STORE_STATEMENT_INVALID",
                "This statement kind is not allowed in a transaction.",
                {"statement": statement.type.name},
            )
        parameters = _bound(statement, request.parameters)
        deadline = self._statement_deadline(request.timeout_ms)
        guard = Guard(self._cursor, deadline)
        # Checked and published together, so a cancel either sees this guard or is seen here.
        with self._lock:
            if self.cancelled:
                guard.stop()
                raise StoreError("STORE_CANCELLED", "The transaction was cancelled.", {})
            self._guard = guard
        typed = _typed(sql, request.parameters)
        try:
            self._cursor.sql(typed, params=parameters)
        except duckdb.Error as error:
            raise self._interrupted(guard.error(error)) from error
        except RuntimeError as error:
            if _user_interrupt(error):
                raise KeyboardInterrupt from error
            raise
        finally:
            guard.stop()
            self._guard = None

    def load_arrow(
        self,
        schema: str,
        table: str,
        reader: pa.RecordBatchReader,
        columns: Sequence[tuple[str, str]],
        on_interrupt: Callable[[], None] | None = None,
        timeout_ms: int | None = None,
    ) -> int:
        """Create `schema.table` in the lake, inside this transaction, from an Arrow stream: each
        column `(name, DuckDB type)` is the stream's field of that name cast to that type, in
        order. The stream is read once, batch by batch, by DuckDB (no row passes through Python),
        and the number of rows is returned.

        `on_interrupt` is called from another thread when the statement times out or is
        cancelled, and on Ctrl-C when the load runs in the main thread (to close the stream's
        source, so a remote query stops at once even while it sends nothing). Ctrl-C then raises
        `KeyboardInterrupt`. An exception raised by the stream itself propagates unchanged; the
        caller decides what it means.
        """
        names = [name for name, _ in columns]
        if len({name.lower() for name in names}) != len(names):
            # DuckDB resolves names without case: `a` and `A` would read the same field.
            raise StoreError(
                "STORE_STATEMENT_INVALID",
                "Column names must differ in more than letter case.",
                {"columns": ", ".join(names)},
            )
        if not columns or reader.schema.names != names:
            raise StoreError(
                "STORE_STATEMENT_INVALID",
                "The Arrow stream's fields must be exactly the declared columns, in order.",
                {"fields": ", ".join(reader.schema.names), "columns": ", ".join(names)},
            )
        for _, kind in columns:
            if _ARROW_LOAD_TYPE.fullmatch(kind) is None:
                raise StoreError(
                    "STORE_STATEMENT_INVALID", "Not a DuckDB type name.", {"type": kind}
                )
        failure: list[BaseException] = []

        def batches() -> Iterator[pa.RecordBatch]:
            try:
                yield from reader
            except BaseException as error:
                failure.append(error)
                raise

        stream = pa.RecordBatchReader.from_batches(reader.schema, batches())
        view = f"__arrow_{uuid.uuid4().hex}"
        target = f"{_identifier(schema)}.{_identifier(table)}"
        selected = ", ".join(
            f"CAST({_identifier(name)} AS {kind}) AS {_identifier(name)}" for name, kind in columns
        )
        deadline = self._statement_deadline(timeout_ms)
        guard = Guard(self._cursor, deadline)
        guard.on_fire = on_interrupt
        with self._lock:
            if self.cancelled:
                guard.stop()
                raise StoreError("STORE_CANCELLED", "The transaction was cancelled.", {})
            self._guard = guard
        pressed: list[bool] = []
        restore = _on_ctrl_c(on_interrupt, pressed)
        try:
            self._cursor.sql(f"CREATE SCHEMA IF NOT EXISTS {_identifier(schema)}")
            self._cursor.register(view, stream)
            try:
                self._cursor.execute(f"CREATE TABLE {target} AS SELECT {selected} FROM {view}")
            finally:
                self._cursor.unregister(view)
            if guard.reason is not None:
                # Interrupted, even if the statement ended: what it read is not the whole stream.
                raise self._interrupted(guard.error())
            counted = self._cursor.execute(f"SELECT count(*) FROM {target}").fetchone()
            if pressed:
                raise KeyboardInterrupt
        except duckdb.Error as error:
            if pressed:
                raise KeyboardInterrupt from None
            if failure and guard.reason is None:
                raise failure[0] from None
            raise self._interrupted(guard.error(error)) from error
        except StoreError:
            raise
        except Exception as error:
            # The stream's own error: unchanged, unless the statement was interrupted, which is
            # then the reason (closing the source on interrupt makes the stream fail).
            if pressed:
                raise KeyboardInterrupt from None
            if guard.reason is not None:
                raise self._interrupted(guard.error()) from None
            if isinstance(error, RuntimeError) and _user_interrupt(error):
                raise KeyboardInterrupt from error
            raise
        finally:
            restore()
            guard.stop()
            self._guard = None
        if pressed:  # pressed while the load was finishing: never lost
            raise KeyboardInterrupt
        return int(counted[0]) if counted else 0

    def land(
        self,
        source: str,
        sql: str,
        parameters: Sequence[Parameter],
        schema: str,
        table: str,
        timeout_ms: int | None = None,
    ) -> int:
        """Replace `schema.table`, inside this transaction, with what `sql` returns on the
        database source `source`, read in its worker (#372) as one Arrow stream, batch by batch
        (no row passes through Python). Types are checked at both ends: the stream's and then
        the landed table's must be the source's own, an ENUM landing as VARCHAR, or nothing is
        kept (`UNSUPPORTED_SOURCE_TYPE`). No type or column text from the source reaches the
        SQL written here. Returns the number of rows."""
        if self._start_source is None:
            raise StoreError(
                "STORE_SOURCE_INVALID", f"{source} is not a database source.", {"source": source}
            )
        deadline = self._statement_deadline(timeout_ms)
        remaining = max(1, int((deadline - time.monotonic()) * 1000))
        stream = self._start_source(source, sql, parameters, remaining)
        try:
            return self._landed(stream, source, schema, table, deadline)
        finally:
            stream.close()  # in every outcome, even one raised before the guard is set

    def _landed(
        self,
        stream: source_worker.SourceStream,
        source: str,
        schema: str,
        table: str,
        deadline: float,
    ) -> int:
        view = f"__source_{uuid.uuid4().hex}"
        target = f"{_identifier(schema)}.{_identifier(table)}"
        guard = Guard(self._cursor, deadline)
        guard.on_fire = stream.close
        with self._lock:
            if self.cancelled:
                guard.stop()
                raise StoreError("STORE_CANCELLED", "The transaction was cancelled.", {})
            self._guard = guard
        pressed: list[bool] = []
        restore = _on_ctrl_c(stream.close, pressed)
        try:
            # Under the guard: a cancel or the time limit ends the worker even while it binds.
            stream.open()
            expected = [(name, source_worker.landed_type(kind)) for name, kind in stream.types]
            if [name for name, _ in expected] != stream.schema.names:
                raise StoreError(
                    "STORE_SOURCE_FAILED",
                    "The process reading the source sent another shape; nothing was kept.",
                    {"source": source},
                )
            self._cursor.register(view, stream.reader())
            try:
                arrived = self._cursor.sql(f"SELECT * FROM {view}").types
                _same_types(source, expected, [str(kind) for kind in arrived])
                for sql in (
                    f"CREATE SCHEMA IF NOT EXISTS {_identifier(schema)}",
                    f"DROP TABLE IF EXISTS {target}",
                    f"CREATE TABLE {target} AS SELECT * FROM {view}",
                ):
                    self._cursor.sql(sql)  # run at once, leaving no result open
            finally:
                with contextlib.suppress(duckdb.Error):  # an aborted transaction rolls it back
                    self._cursor.unregister(view)
            if guard.reason is not None:
                raise self._interrupted(guard.error())
            landed = self._cursor.sql(f"SELECT * FROM {target}").types
            _same_types(source, expected, [str(kind) for kind in landed])
            result = self._cursor.execute(f"SELECT count(*) FROM {target}")
            counted = result.fetchone()
            result.fetchone()  # reaches the end of the one-row result, so the cursor is free
            if pressed:
                raise KeyboardInterrupt
        except duckdb.Error as error:
            if pressed:
                raise KeyboardInterrupt from None
            if guard.reason is not None:
                raise self._interrupted(guard.error(error)) from error
            if stream.failed is not None:  # the worker's stream ended early: its own reason
                raise stream.failed from None
            raise StoreError(
                "STORE_QUERY_FAILED",
                "The lake could not store the source's rows; nothing was kept.",
                {"source": source, "duckdb": str(error)},
            ) from None
        except StoreError as error:
            if guard.reason is not None and error.code in {"STORE_SOURCE_FAILED", "STORE_TIMEOUT"}:
                raise self._interrupted(guard.error()) from None
            raise
        finally:
            restore()
            guard.stop()
            self._guard = None
            stream.close()
        if pressed:
            raise KeyboardInterrupt
        return int(counted[0]) if counted else 0


def _same_types(source: str, expected: Sequence[tuple[str, str]], found: Sequence[str]) -> None:
    """`UNSUPPORTED_SOURCE_TYPE` naming the first column whose type is not the source's own."""
    for (name, kind), arrived in zip(expected, found, strict=True):
        if kind != arrived:
            raise StoreError(
                "UNSUPPORTED_SOURCE_TYPE",
                f"Column {source}.{name} would land as {arrived}, not {kind} (law 11); nothing "
                "was kept.",
                {"column": f"{source}.{name}"},
            )
