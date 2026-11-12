"""The managed lake's façade (contract `store` v1, P1-01): `open_lake` and `Lake`, which opens
one DuckLake and serves its reads, transactions, catalog questions and maintenance.

Writes run inside DuckDB in explicit transactions; each read is pinned to a snapshot by adding
`AT (VERSION => n)` to every lake table it references (rewritten with DuckDB's own parser) and is
read once, as an Arrow record-batch stream (ADR-0025). No row passes through Python. The work is
done by the store's modules of one purpose each; their public names that were importable from
here before #246 stay importable (the `import NAME as NAME` lines).
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
from orvex.contracts.store.v1 import (
    ChangesRequest,
    ChangesResult,
    CommitResult,
    FileSourceAttachment,
    LakeLocation,
    LocalLakeLocation,
    MaintenanceReport,
    Parameter,
    PostgresLakeLocation,
    PostgresSourceAttachment,
    ReadRequest,
    Snapshot,
    SourceAttachment,
    SqliteLakeLocation,
    SqliteSourceAttachment,
    StatisticsRequest,
    StatisticsResult,
    TablesRequest,
    TablesResult,
)
from orvex.store import source_worker, transaction
from orvex.store.arrow_exact import exact_select as exact_select
from orvex.store.catalog_reads import table_changes, table_statistics
from orvex.store.catalog_tables import tables_at, tables_with_columns
from orvex.store.catalog_values import _snapshot_of
from orvex.store.credentials import _source_password
from orvex.store.errors import StoreError, _source_not_found
from orvex.store.file_names import FOLDER_COST as FOLDER_COST
from orvex.store.file_names import PATTERN_BUDGET as PATTERN_BUDGET
from orvex.store.file_names import STEP_BUDGET as STEP_BUDGET
from orvex.store.isolation import EXTENSIONS as EXTENSIONS
from orvex.store.isolation import Isolation
from orvex.store.lake_names import LAKE as LAKE
from orvex.store.lake_names import METADATA as METADATA
from orvex.store.lake_names import METADATA_PREFIX as METADATA_PREFIX
from orvex.store.lake_names import RESERVED_SOURCE_NAMES as RESERVED_SOURCE_NAMES
from orvex.store.lake_names import _check_source_names
from orvex.store.limits import INTERRUPT_REPEAT_SECONDS as INTERRUPT_REPEAT_SECONDS
from orvex.store.limits import MAX_TIMEOUT_MS as MAX_TIMEOUT_MS
from orvex.store.limits import Admission as Admission
from orvex.store.limits import Guard as Guard
from orvex.store.limits import Limits as Limits
from orvex.store.limits import ReadHandle as ReadHandle
from orvex.store.limits import _bounded, _require_limits
from orvex.store.locations import (
    AnyLakeLocation,
    _data_path,
    _postgres_password,
    _storage_credentials,
)
from orvex.store.maintenance import maintained
from orvex.store.parameters import INTEGER_RANGES as INTEGER_RANGES
from orvex.store.parameters import _bound, _typed
from orvex.store.read_stream import stream_read
from orvex.store.read_tree import JsonTree as JsonTree
from orvex.store.read_tree import _read_tree, pin_tables
from orvex.store.source_exactness import Inexact as Inexact
from orvex.store.source_exactness import postgres_inexact_sql as postgres_inexact_sql
from orvex.store.source_worker import DatabaseSource
from orvex.store.sql_guard import DUCKLAKE_TABLE_MACROS as DUCKLAKE_TABLE_MACROS
from orvex.store.sql_guard import ENGINE_NAMES_ALLOWED as ENGINE_NAMES_ALLOWED
from orvex.store.sql_guard import READ_TABLE_FUNCTIONS as READ_TABLE_FUNCTIONS
from orvex.store.sql_guard import SHARED_HARMLESS_FUNCTIONS as SHARED_HARMLESS_FUNCTIONS
from orvex.store.sql_guard import TABLE_FUNCTIONS_SQL as TABLE_FUNCTIONS_SQL
from orvex.store.sql_guard import WRITE_STATEMENTS as WRITE_STATEMENTS
from orvex.store.sql_guard import _single_statement
from orvex.store.sql_parser import PARSER_SPACES as PARSER_SPACES
from orvex.store.sql_parser import _plain
from orvex.store.sql_text import literal as _quote
from orvex.store.sqlite_copy import sqlite_backup
from orvex.store.transaction import Transaction as Transaction

LAND_BATCH_SIZE = 65_536  # rows per Arrow batch from a source worker (#372)


class Lake:
    """One open lake. Reads may run concurrently; write transactions run one at a time."""

    def __init__(
        self,
        location: LakeLocation | AnyLakeLocation,
        limits: Limits,
        sources: Sequence[AnySource] = (),
    ) -> None:
        if isinstance(location, LakeLocation):
            location = location.root
        # Location checks come first: nothing is opened for a location that must be refused.
        password = (
            _postgres_password(location) if isinstance(location, PostgresLakeLocation) else ""
        )
        data_path = _data_path(location)
        storage = _storage_credentials(location.storage, data_path)
        memory, threads, capacity, queue_size = _require_limits(limits)
        attachments: list[FileSourceAttachment | SqliteSourceAttachment | PostgresSourceAttachment]
        attachments = [
            source.root if isinstance(source, SourceAttachment) else source for source in sources
        ]
        # Passwords are read and registered before anything is opened, so no error can show one.
        passwords = {
            source.name: _source_password(source)
            for source in attachments
            if isinstance(source, PostgresSourceAttachment)
        }
        _check_source_names([source.name for source in attachments])
        self._limits = limits
        self._admission = Admission(capacity, queue_size)
        # With database sources, the limits are split (#372): a worker gets its own share and
        # the lake keeps the rest, so the two together never exceed what was given.
        self._worker_memory, self._worker_threads = memory, threads
        self._worker_slot = threading.Semaphore(1)
        if any(source_worker.is_database(source) for source in attachments):
            try:
                memory, threads, self._worker_memory, self._worker_threads = (
                    source_worker.split_limits(memory, threads)
                )
            except (duckdb.Error, ValueError) as error:
                raise StoreError(
                    "STORE_LIMITS_REQUIRED",
                    "The memory limit or the thread count is not valid.",
                    {"duckdb": str(error)},
                ) from error
        try:
            self._connection = duckdb.connect(
                config={
                    "memory_limit": memory,
                    "threads": threads,
                    # Secrets saved on this machine are never loaded into a lake.
                    "allow_persistent_secrets": False,
                }
            )
        except (duckdb.Error, ValueError) as error:
            raise StoreError(
                "STORE_LIMITS_REQUIRED",
                "The memory limit or the thread count is not valid.",
                {"duckdb": str(error)},
            ) from error
        self._write_lock = threading.Lock()
        self._maintenance_lock = threading.Lock()
        # DuckDB spills to this folder when a query exceeds the memory limit.
        self._spill = tempfile.mkdtemp(prefix="lake-spill-")
        # The source workers' folders (#372), apart from the spill folder no worker may learn of,
        # and the SQLite source copies (#410), outside every folder the lake's connection may open.
        self._workers = tempfile.mkdtemp(prefix="lake-workers-")
        self._copies = tempfile.mkdtemp(prefix="lake-sources-")
        self._local = threading.local()
        setup = Isolation(self._connection, self._spill, self._one)
        try:
            setup.load_extensions()
            # Catalog tables: `<metadata database>[.<schema>]`; the schema name is validated by
            # the contract pattern ([a-z_][a-z0-9_]*) before it reaches SQL.
            self._metadata = METADATA
            if isinstance(location, PostgresLakeLocation) and location.metadata_schema:
                self._metadata = f"{METADATA}.{location.metadata_schema}"
            if storage is not None:
                setup.storage_secret(storage, data_path)
            else:
                Path(data_path).mkdir(parents=True, exist_ok=True)
            options = [f"DATA_PATH {_quote(data_path)}"]
            if location.encrypted:
                # Fixed when the lake is created; deletion vectors stay off (written unencrypted).
                # DuckDB writes encrypted files only with httpfs's crypto module loaded (without
                # it, its built-in one is read-only and every write fails, #127).
                setup.load_httpfs()
                options.append("ENCRYPTED")
            if isinstance(location, LocalLakeLocation):
                setup.attach_local(Path(location.path), location.catalog_path, options)
            elif isinstance(location, SqliteLakeLocation):
                setup.attach_sqlite_catalog(location, options)
            else:
                setup.attach_postgres(location, password, options)
            # Nothing may load code or reach data the lake did not ask for after this point.
            self._connection.sql("SET autoinstall_known_extensions = false")
            self._connection.sql("SET autoload_known_extensions = false")
            # Database sources (DuckDB, SQLite, PostgreSQL) carry SQL of their own: they are
            # never attached here, only read in a worker of their own (#372). A missing file is
            # refused now, before anything is read.
            self._databases: dict[str, tuple[DatabaseSource, str]] = {}
            readable: list[str] = []
            for source in attachments:
                if source_worker.is_database(source):
                    if not isinstance(source, PostgresSourceAttachment) and not (
                        Path(source.path).is_file()
                    ):
                        raise _source_not_found(source)
                    if isinstance(source, SqliteSourceAttachment):
                        # One consistent copy per open lake, WAL included (#126): every read of
                        # this lake sees the file as it was now. Pages only: no SQL is parsed.
                        copy = Path(self._copies) / f"{source.name}.sqlite"
                        sqlite_backup(source, copy)
                        source = source.model_copy(update={"path": copy.as_posix()})
                    self._databases[source.name] = (source, passwords.get(source.name, ""))
                elif isinstance(source, FileSourceAttachment):
                    readable += setup.attach_file_source(source)
            self._table_functions = self._names_of_table_functions()
            setup.sandbox(location, readable)
            # Every cursor inherits these; then no setting may change on this connection again,
            # whatever reaches it past the SQL guard (#417 audit F4, #441).
            # TIMESTAMPTZ values reach Arrow as instants in UTC whatever the machine's zone;
            # presenting them in a local zone is a later, explicit step.
            self._connection.sql("SET GLOBAL TimeZone = 'UTC'")
            # A name in FROM must never resolve to a Python object in scope.
            self._connection.sql("SET GLOBAL python_enable_replacements = false")
            self._connection.sql("SET lock_configuration = true")
        except BaseException:
            self._connection.close()
            for folder in (self._spill, self._workers, self._copies):
                shutil.rmtree(folder, ignore_errors=True)
            raise

    def _names_of_table_functions(self) -> frozenset[str]:
        row = self._one(TABLE_FUNCTIONS_SQL, {})
        text = str(row[0]) if row and row[0] is not None else ""
        return frozenset(text.split("|")) - {""} - SHARED_HARMLESS_FUNCTIONS

    def close(self) -> None:
        self._connection.close()
        for folder in (self._spill, self._workers, self._copies):
            shutil.rmtree(folder, ignore_errors=True)

    def _cursor(self) -> duckdb.DuckDBPyConnection:
        cursor = self._connection.cursor()
        cursor.sql(f"USE {LAKE}")  # its search path; the settings are the connection's (locked)
        return cursor

    def _one(
        self,
        sql: str,
        parameters: dict[str, object],
        cursor: duckdb.DuckDBPyConnection | None = None,
    ) -> tuple[object, ...] | None:
        """One metadata row, on `cursor` (a request's, under its guard, #160) or on a
        short-lived cursor; no unfinished result is left behind."""
        if cursor is not None:
            result = cursor.execute(sql, parameters)
            found: tuple[object, ...] | None = result.fetchone()
            result.fetchone()  # reaches the end of the one-row result: the cursor is free again
            return found
        own = self._cursor()
        try:
            row: tuple[object, ...] | None = own.execute(sql, parameters).fetchone()
        finally:
            own.close()
        return row

    def _snapshot(
        self, snapshot_id: int | None, cursor: duckdb.DuckDBPyConnection | None = None
    ) -> Snapshot | None:
        where = "" if snapshot_id is None else "WHERE snapshot_id = $id"
        parameters = {} if snapshot_id is None else {"id": snapshot_id}
        row = self._one(
            "SELECT snapshot_id, epoch_us(snapshot_time) FROM ducklake_snapshots($lake) "
            f"{where} ORDER BY snapshot_id DESC LIMIT 1",
            {"lake": LAKE, **parameters},
            cursor,
        )
        return None if row is None else _snapshot_of(row)

    def _committed(self, token: str) -> Snapshot | None:
        """The snapshot a transaction committed, by its token; None when it changed nothing
        (DuckLake then makes no snapshot). Never the latest snapshot: another writer of a shared
        lake may have committed since (#255)."""
        row = self._one(
            "SELECT snapshot_id, epoch_us(snapshot_time) FROM ducklake_snapshots($lake) "
            "WHERE commit_extra_info = $token",
            {"lake": LAKE, "token": token},
        )
        return None if row is None else _snapshot_of(row)

    def _oldest_snapshot(self, cursor: duckdb.DuckDBPyConnection | None = None) -> Snapshot | None:
        row = self._one(
            "SELECT snapshot_id, epoch_us(snapshot_time) FROM ducklake_snapshots($lake) "
            "ORDER BY snapshot_id LIMIT 1",
            {"lake": LAKE},
            cursor,
        )
        return None if row is None else _snapshot_of(row)

    def _missing_snapshot(
        self, snapshot_id: int, cursor: duckdb.DuckDBPyConnection | None = None
    ) -> StoreError:
        """Why a snapshot cannot be read: expired by maintenance (ids are never reused and every
        id up to the current one was committed, so a missing one among them expired), or never
        existed."""
        oldest = self._oldest_snapshot(cursor)
        current = self._snapshot(None, cursor)
        if oldest is not None and current is not None and 0 <= snapshot_id <= current.id:
            return StoreError(
                "STORE_SNAPSHOT_EXPIRED",
                f"Snapshot {snapshot_id} has expired: the oldest snapshot kept is {oldest.id} "
                f"({oldest.committed_at.isoformat()}); the lake's snapshot retention decides how "
                "long snapshots stay readable.",
                {"snapshot": str(snapshot_id), "oldest": str(oldest.id)},
            )
        return StoreError(
            "STORE_SNAPSHOT_NOT_FOUND",
            f"Snapshot {snapshot_id} does not exist in the lake.",
            {"snapshot": str(snapshot_id)},
        )

    def maintain(
        self, expire_before: dt.datetime, orphans_before: dt.datetime
    ) -> MaintenanceReport:
        """Expire the snapshots committed before `expire_before`, merge small files, rewrite
        files with many deletes, and delete the files no remaining snapshot uses, and orphaned
        files older than `orphans_before` (a writer may still be creating a newer one).

        DuckLake's own `CHECKPOINT` order, step by step so each one is counted: flush inlined
        data, expire, merge, rewrite, expire again (the snapshot merging replaced, when it is
        older than the cutoff), clean up, delete orphans. The current snapshot is never expired.
        Runs alone: one maintenance at a time, after every read and write of this lake ends
        (the writer lock, then every admission slot). Counts are read in SQL; no row reaches
        Python.
        """
        for instant in (expire_before, orphans_before):
            if instant.tzinfo is None:
                raise StoreError(
                    "STORE_PARAMETER_INVALID", "Maintenance cutoffs must be aware instants.", {}
                )
        cutoff = expire_before.astimezone(dt.UTC).isoformat()
        orphans = orphans_before.astimezone(dt.UTC).isoformat()
        deadline = time.monotonic() + self._timeout_ms(None) / 1000
        # One maintenance at a time (two taking part of the slots each would wait for each
        # other), then the writer lock, then every admission slot: no read or write of this lake
        # runs during maintenance (a read pinned to a snapshot being expired would lose its files).
        # The writer lock comes before the slots, as for a transaction, so neither holds what the
        # other waits for (#77).
        if not self._maintenance_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise StoreError("STORE_TIMEOUT", "Another maintenance of this lake is running.", {})
        try:
            if not self._write_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
                raise StoreError(
                    "STORE_TIMEOUT", "Maintenance waited too long for the writer ahead.", {}
                )
            taken = 0
            try:
                for _ in range(int(self._limits.max_concurrent or 1)):
                    self._admission.acquire(deadline)
                    taken += 1
                cursor = self._cursor()
                try:
                    report = maintained(cursor, cutoff, orphans)
                finally:
                    cursor.close()
            finally:
                for _ in range(taken):
                    self._admission.release()
                self._write_lock.release()
        finally:
            self._maintenance_lock.release()
        return report

    def tables(self) -> set[tuple[str, str]]:
        """(schema, table) of every table of the lake now, lower-cased."""
        return self._tables_at(self.current_snapshot().id)

    def snapshot(self, snapshot_id: int) -> Snapshot | None:
        """The snapshot `snapshot_id` while the lake keeps it; None once it expired or when it
        never existed."""
        if isinstance(snapshot_id, bool) or not isinstance(snapshot_id, int) or snapshot_id < 0:
            return None
        return self._snapshot(snapshot_id)

    def current_snapshot(self) -> Snapshot:
        snapshot = self._snapshot(None)
        if snapshot is None:
            raise StoreError("STORE_SNAPSHOT_NOT_FOUND", "The lake has no snapshot.", {})
        return snapshot

    def _tables_at(
        self, snapshot: int, cursor: duckdb.DuckDBPyConnection | None = None
    ) -> set[tuple[str, str]]:
        """(schema, table) of every lake table alive at `snapshot`, lower-cased."""
        if cursor is not None:
            return tables_at(cursor, self._metadata, snapshot)
        own = self._cursor()
        try:
            return tables_at(own, self._metadata, snapshot)
        finally:
            own.close()

    def _pinned(
        self, tree: dict[str, Any], snapshot: int, cursor: duckdb.DuckDBPyConnection
    ) -> str:
        """The read's SELECT (`_read_tree`) with `AT (VERSION => snapshot)` on every table it
        references, on the read's cursor (under its guard, #160).

        Every base table is pinned, including CTE references (DuckDB binds a CTE with an AT
        clause unchanged). A name that is not a lake table (or the read's own CTE) is refused.
        """
        tables = self._tables_at(snapshot, cursor)
        pin_tables(tree, snapshot, tables)
        rewritten = self._one(
            "SELECT json_deserialize_sql($tree)", {"tree": json.dumps(tree)}, cursor
        )
        return str(rewritten[0]) if rewritten else ""

    def describe(self, sql: str, parameters: Sequence[Parameter] = ()) -> list[tuple[str, str]]:
        """(name, DuckDB type) of each output column of one SELECT, from the binder only.

        Nothing is executed. Sources may be named (writes read them); the statement checks of
        writes apply (no file readers, no quoted file names, no catalog database). Parameters
        are bound as for a write (typically with a `null` value: only their type matters).
        """
        sql = _plain(sql)
        statement = _single_statement(sql, self._table_functions)
        if statement.type != duckdb.StatementType.SELECT:
            raise StoreError(
                "STORE_STATEMENT_INVALID",
                "Only a SELECT can be described.",
                {"statement": statement.type.name},
            )
        bound = _bound(statement, list(parameters))
        typed = _typed(sql, list(parameters))
        self._admission.acquire(time.monotonic() + self._timeout_ms(None) / 1000)
        cursor = None
        try:
            cursor = self._cursor()
            relation = cursor.sql(typed, params=bound) if bound else cursor.sql(typed)
            return [
                (str(name), str(kind))
                for name, kind in zip(relation.columns, relation.types, strict=True)
            ]
        except duckdb.Error as error:
            raise StoreError(
                "STORE_QUERY_FAILED", "DuckDB could not bind the query.", {"duckdb": str(error)}
            ) from None
        finally:
            if cursor is not None:
                cursor.close()
            self._admission.release()

    def _land(
        self,
        name: str,
        sql: str,
        parameters: Sequence[Parameter],
        timeout_ms: int | None,
        describe: bool = False,
    ) -> source_worker.SourceStream:
        """A worker reading `sql` from the database source `name` (#372): never this lake's
        connection, which does not hold the source."""
        found = self._databases.get(name)
        if found is None:
            raise StoreError(
                "STORE_SOURCE_INVALID",
                f"{name} is not a database source of this lake.",
                {"source": name},
            )
        source, password = found
        workers = Path(self._workers)
        # One worker at a time: the limits were split for one (`split_limits`).
        deadline = time.monotonic() + self._timeout_ms(timeout_ms) / 1000
        if not self._worker_slot.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise StoreError("STORE_TIMEOUT", "No source worker became free in time.", {})
        try:
            stream = source_worker.start(
                source,
                password,
                sql,
                parameters,
                memory_limit=self._worker_memory,
                threads=self._worker_threads,
                timeout_ms=self._timeout_ms(timeout_ms),
                batch_size=LAND_BATCH_SIZE,
                folder=workers,
                describe=describe,
            )
        except BaseException:
            self._worker_slot.release()
            raise
        stream.on_close = self._worker_slot.release
        return stream

    def describe_source(
        self, name: str, sql: str, parameters: Sequence[Parameter] = ()
    ) -> list[tuple[str, str]]:
        """The columns and the types they land with (ENUM as VARCHAR) of `sql` on the database
        source `name`, bound in its worker; no row is read."""
        stream = self._land(name, sql, parameters, None, describe=True)
        try:
            stream.open()
            return [(column, source_worker.landed_type(kind)) for column, kind in stream.types]
        finally:
            stream.close()

    def statistics(self, request: StatisticsRequest) -> StatisticsResult:
        """The file statistics of lake tables as of `request.snapshot`, from the lake's own
        catalog on the store's connection (#352). Only what an upper bound of read cost needs:
        each data file's row count before deletes and size, each column's exact minimum and
        maximum and null count where the lake keeps them, and the rows kept in the catalog
        (data inlining). Never a path or any other catalog detail. A name that is not a lake
        table at the snapshot is refused."""
        deadline = time.monotonic() + self._timeout_ms(None) / 1000
        self._admission.acquire(deadline)
        try:
            cursor = self._cursor()
            guard = Guard(cursor, deadline)
            try:
                if self._snapshot(request.snapshot, cursor) is None:
                    raise self._missing_snapshot(request.snapshot, cursor)
                tables = [
                    table_statistics(self._one, self._metadata, name.root, request.snapshot, cursor)
                    for name in request.tables
                ]
            except duckdb.Error as error:
                raise guard.error(error) from error
            finally:
                guard.stop()
                cursor.close()
        finally:
            self._admission.release()
        return StatisticsResult(kind="statistics_result", snapshot=request.snapshot, tables=tables)

    def changes(self, request: ChangesRequest) -> ChangesResult:
        """What changed in lake tables after `request.since` up to `request.snapshot` (#355),
        from the lake's own change history, counted by DuckDB: whether each table changed at
        all, and how many rows it had at `since` were deleted or updated. Counts only. A name
        that is a lake table at neither snapshot is refused, as is `since` after `snapshot`."""
        deadline = time.monotonic() + self._timeout_ms(None) / 1000
        self._admission.acquire(deadline)
        try:
            cursor = self._cursor()
            guard = Guard(cursor, deadline)
            try:
                for snapshot in (request.since, request.snapshot):
                    if self._snapshot(snapshot, cursor) is None:
                        raise self._missing_snapshot(snapshot, cursor)
                if request.since > request.snapshot:
                    raise StoreError(
                        "STORE_STATEMENT_INVALID",
                        "`since` is after `snapshot`.",
                        {"since": str(request.since), "snapshot": str(request.snapshot)},
                    )
                count = request.count_removed is not False
                tables = [
                    table_changes(
                        self._one,
                        self._metadata,
                        name.root,
                        request.since,
                        request.snapshot,
                        cursor,
                        count,
                    )
                    for name in request.tables
                ]
            except duckdb.Error as error:
                raise guard.error(error) from error
            finally:
                guard.stop()
                cursor.close()
        finally:
            self._admission.release()
        return ChangesResult.model_validate(
            {
                "kind": "changes_result",
                "since": request.since,
                "snapshot": request.snapshot,
                "tables": tables,
            }
        )

    def table_columns(self, request: TablesRequest) -> TablesResult:
        """Every lake table alive at `request.snapshot`, with its columns as of that snapshot
        (#358): the names and nullability from the lake's own catalog, the DuckDB types as
        DuckDB binds the table at that version. Nothing of any other version is read."""
        deadline = time.monotonic() + self._timeout_ms(None) / 1000
        self._admission.acquire(deadline)
        try:
            cursor = self._cursor()
            guard = Guard(cursor, deadline)
            try:
                if self._snapshot(request.snapshot, cursor) is None:
                    raise self._missing_snapshot(request.snapshot, cursor)
                tables = tables_with_columns(cursor, self._metadata, LAKE, request.snapshot)
            except duckdb.Error as error:
                raise guard.error(error) from error
            finally:
                guard.stop()
                cursor.close()
        finally:
            self._admission.release()
        return TablesResult.model_validate(
            {"kind": "tables_result", "snapshot": request.snapshot, "tables": tables}
        )

    def read(self, request: ReadRequest) -> pa.RecordBatchReader:
        """Run one SELECT as of `request.snapshot` and return it once, as Arrow batches."""
        return self.start_read(request).reader

    def _timeout_ms(self, requested: int | None) -> int:
        assert self._limits.default_timeout_ms is not None
        return requested if requested is not None else self._limits.default_timeout_ms

    def start_read(self, request: ReadRequest) -> ReadHandle:
        """Like `read`, with a handle that can cancel the read from another thread.

        The read waits for an admission slot, holds it until its stream ends, and is interrupted
        when its timeout (counted from the call, waiting included) expires.
        """
        sql = _plain(request.sql)
        statement = _single_statement(sql, self._table_functions)
        if statement.type != duckdb.StatementType.SELECT:
            raise StoreError(
                "STORE_STATEMENT_INVALID",
                "A read must be one SELECT statement.",
                {"statement": statement.type.name},
            )
        parameters = _bound(statement, request.parameters)
        typed = _typed(sql, request.parameters)
        tree = _read_tree(typed)  # before admission: no catalog needed (#160)
        deadline = time.monotonic() + self._timeout_ms(request.timeout_ms) / 1000
        self._admission.acquire(deadline)
        try:
            cursor = self._cursor()
        except BaseException:
            self._admission.release()
            raise
        guard = Guard(cursor, deadline)
        try:
            # Every query on the lake runs inside the slot (#77), and under the read's limit
            # (#160). The snapshot is checked once admitted: maintenance may have expired it
            # while this read waited behind it (maintenance runs alone, so it cannot expire it
            # from now on).
            if self._snapshot(request.snapshot, cursor) is None:
                raise self._missing_snapshot(request.snapshot, cursor)
            sql = self._pinned(tree, request.snapshot, cursor)
        except BaseException as error:
            guard.stop()
            cursor.close()
            self._admission.release()
            if isinstance(error, duckdb.Error):
                raise guard.error(error) from error
            raise
        return stream_read(
            cursor, guard, sql, parameters, request.batch_size, self._admission.release
        )

    @contextmanager
    def transaction(self) -> Iterator[Transaction]:
        """Statements inside the block commit together to one snapshot, or roll back on error.

        The snapshot reported in the result is the one this transaction created, found by a
        token it carries, even when another process commits to the same lake right after (#255).
        """
        if getattr(self._local, "writing", False):
            raise StoreError(
                "STORE_STATEMENT_INVALID",
                "A transaction cannot be opened inside another one in the same thread.",
                {},
            )
        deadline = time.monotonic() + self._timeout_ms(None) / 1000
        # The writer ahead first, then a slot, both under the same limit: a writer that only
        # waits holds no slot, so reads are not blocked behind it (#77). Maintenance takes the
        # writer lock before the slots too, so the two never wait for each other.
        if not self._write_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise StoreError(
                "STORE_TIMEOUT", "The transaction waited too long for the writer ahead.", {}
            )
        try:
            self._admission.acquire(deadline)
            try:
                yield from self._transaction(deadline)
            finally:
                self._admission.release()
        finally:
            self._write_lock.release()

    def _transaction(self, deadline: float) -> Iterator[Transaction]:
        self._local.writing = True
        try:
            cursor = self._cursor()
        except BaseException:
            self._local.writing = False
            raise
        try:
            # The store's own catalog queries run under the transaction's limit too (#160).
            outside = _bounded(cursor, deadline, lambda: transaction._outside_inventory(cursor))
            cursor.begin()
            handle = Transaction(
                cursor,
                self._table_functions,
                self._timeout_ms,
                deadline,
                self._land if self._databases else None,
            )
            try:
                # The snapshot this transaction reads: what a run that changes nothing ran on.
                handle.started = handle.bounded(lambda: self._snapshot(None, cursor))
                yield handle
                # A statement interrupted inside the block decides the outcome, even when
                # the block caught its error (#77).
                if handle.interrupted is not None:
                    raise handle.interrupted
                if handle.cancelled:
                    raise StoreError("STORE_CANCELLED", "The transaction was cancelled.", {})
                changed_outside, unpinnable = handle.bounded(
                    lambda: (
                        transaction._outside_inventory(cursor) != outside,
                        transaction._unpinnable_objects(cursor),
                    )
                )
                if handle.cancelled:
                    # A cancel that came as the checks finished still stops the commit.
                    raise StoreError("STORE_CANCELLED", "The transaction was cancelled.", {})
                if changed_outside:
                    raise StoreError(
                        "STORE_STATEMENT_INVALID",
                        "A transaction may change only tables and schemas of the lake.",
                        {},
                    )
                if unpinnable:
                    raise StoreError(
                        "STORE_STATEMENT_INVALID",
                        "Views and macros are not allowed: a read could not pin the "
                        "tables they use to a snapshot.",
                        {},
                    )
                # The commit runs inside the transaction's limit; once started it is one
                # atomic catalog transaction and is not interrupted.
                if time.monotonic() >= deadline:
                    raise StoreError(
                        "STORE_TIMEOUT", "The transaction exceeded its time limit.", {}
                    )
                # A token of this transaction alone on its snapshot, so the snapshot it
                # committed is found by the token, never as the latest one (#255).
                token = uuid.uuid4().hex
                handle.bounded(lambda: transaction._tag(cursor, token))
                if handle.cancelled:  # a cancel that came while tagging still stops the commit
                    raise StoreError("STORE_CANCELLED", "The transaction was cancelled.", {})
            except BaseException:
                cursor.rollback()
                raise
            try:
                # The point of no return: the commit and the snapshot read after it are not
                # interrupted, so a committed transaction is never reported as failed (#160).
                transaction._commit(cursor)
            except duckdb.TransactionException as error:
                raise StoreError(
                    "STORE_TRANSACTION_CONFLICT",
                    "Another writer changed the same data first; nothing was written.",
                    {"duckdb": str(error)},
                ) from error
            except duckdb.Error as error:
                raise StoreError(
                    "STORE_QUERY_FAILED",
                    "The transaction could not commit.",
                    {"duckdb": str(error)},
                ) from error
            after = self._committed(token)
        finally:
            cursor.close()
            self._local.writing = False
        handle.result = CommitResult(kind="commit_result", snapshot=after)


type AnySource = (
    SourceAttachment | FileSourceAttachment | SqliteSourceAttachment | PostgresSourceAttachment
)


def open_lake(
    location: LakeLocation | AnyLakeLocation,
    limits: Limits,
    sources: Sequence[AnySource] = (),
) -> Lake:
    return Lake(location, limits, sources)
