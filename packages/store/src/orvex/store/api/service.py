"""The store's implementation of its interface contract (`orvex.contracts.store.interface`): each
operation on what `Holdings` keeps open, every state-changing call once per idempotency key."""

from __future__ import annotations

import contextlib
import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pyarrow as pa
from orvex.contracts.envelope import CallEnvelope
from orvex.contracts.store.arrow_ipc import (
    batch_bytes,
    one_batch,
    stream_bytes,
    stream_reader,
    stream_schema,
)
from orvex.contracts.store.errors import StoreError
from orvex.contracts.store.v1 import (
    ChangesRequest,
    ChangesResult,
    CommitResult,
    ExistingLakeAttachment,
    FileSourceAttachment,
    LocalLakeLocation,
    MaintenanceReport,
    Parameter,
    PostgresLakeLocation,
    PostgresSourceAttachment,
    ReadRequest,
    Snapshot,
    SqliteLakeLocation,
    SqliteSourceAttachment,
    StatementRequest,
    StatisticsRequest,
    StatisticsResult,
    TablesRequest,
    TablesResult,
)
from orvex.contracts.store.values import (
    Budget,
    ClickHouseCatalog,
    ClickHouseHandle,
    ClickHouseTarget,
    ExistingLakeHandle,
    ExistingLakeLimits,
    Held,
    JsonFrame,
    LakeHandle,
    Limits,
    ReadHandle,
    SourceColumn,
    TransactionHandle,
)
from orvex.store import separation
from orvex.store.api.held import STORE_SCOPE, Holdings
from orvex.store.clickhouse import ClickHouse
from orvex.store.existing_lake import ExistingLake
from orvex.store.existing_lake_reads import LakeReads
from orvex.store.json_encoding import JSON_TAIL, json_head, json_rows
from orvex.store.lake import Lake, Transaction

LAKE, TRANSACTION, READ, EXISTING, CLICKHOUSE = (
    "lake",
    "transaction",
    "read",
    "existing-lake",
    "clickhouse",
)


class _RolledBack(Exception):
    """Thrown into a transaction's block to roll it back."""


@dataclass(slots=True)
class _Write:
    block: contextlib.AbstractContextManager[Transaction]
    transaction: Transaction


@dataclass(slots=True)
class _Read:
    reader: pa.RecordBatchReader
    cancel: Callable[[], None] | None


@dataclass(slots=True)
class _Existing:
    lake: ExistingLake
    reads: LakeReads


class Store:
    """One process's store: every lake, transaction, read and connection it holds open."""

    def __init__(self) -> None:
        self._held = Holdings()

    # --- lake ----------------------------------------------------------------------------------

    def open_lake(
        self,
        envelope: CallEnvelope,
        location: LocalLakeLocation | SqliteLakeLocation | PostgresLakeLocation,
        limits: Limits,
        sources: tuple[
            FileSourceAttachment | SqliteSourceAttachment | PostgresSourceAttachment, ...
        ],
    ) -> LakeHandle:
        def work() -> LakeHandle:
            return LakeHandle(self._held.add(LAKE, Lake(location, limits, sources)))

        return self._held.once(STORE_SCOPE, "open_lake", envelope.idempotency_key, work)

    def close_lake(self, envelope: CallEnvelope, lake: LakeHandle) -> None:
        def work() -> None:
            opened = self._lake(lake)
            self._held.remove(lake.id)
            opened.close()

        self._held.once(STORE_SCOPE, f"close_lake:{lake.id}", envelope.idempotency_key, work)

    def held(self, envelope: CallEnvelope) -> Held:
        return Held(
            lakes=self._held.ids(LAKE),
            transactions=self._held.ids(TRANSACTION),
            reads=self._held.ids(READ),
            existing_lakes=self._held.ids(EXISTING),
            clickhouse=self._held.ids(CLICKHOUSE),
            cancelled=self._held.cancelled(),
        )

    def current_snapshot(self, envelope: CallEnvelope, lake: LakeHandle) -> Snapshot:
        return self._lake(lake).current_snapshot()

    def snapshot(
        self, envelope: CallEnvelope, lake: LakeHandle, snapshot_id: int
    ) -> Snapshot | None:
        return self._lake(lake).snapshot(snapshot_id)

    def tables(self, envelope: CallEnvelope, lake: LakeHandle) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self._lake(lake).tables()))

    def describe(
        self, envelope: CallEnvelope, lake: LakeHandle, sql: str, parameters: tuple[Parameter, ...]
    ) -> tuple[tuple[str, str], ...]:
        return tuple(self._lake(lake).describe(sql, parameters))

    def describe_source(
        self,
        envelope: CallEnvelope,
        lake: LakeHandle,
        name: str,
        sql: str,
        parameters: tuple[Parameter, ...],
    ) -> tuple[tuple[str, str], ...]:
        return tuple(self._lake(lake).describe_source(name, sql, parameters))

    def statistics(
        self, envelope: CallEnvelope, lake: LakeHandle, request: StatisticsRequest
    ) -> StatisticsResult:
        return self._lake(lake).statistics(request)

    def changes(
        self, envelope: CallEnvelope, lake: LakeHandle, request: ChangesRequest
    ) -> ChangesResult:
        return self._lake(lake).changes(request)

    def table_columns(
        self, envelope: CallEnvelope, lake: LakeHandle, request: TablesRequest
    ) -> TablesResult:
        return self._lake(lake).table_columns(request)

    # --- read ----------------------------------------------------------------------------------

    def start_read(
        self, envelope: CallEnvelope, lake: LakeHandle, request: ReadRequest
    ) -> ReadHandle:
        running = self._lake(lake).start_read(request)
        return self._reading(running.reader, running.cancel, lake.id)

    def read_batch(self, envelope: CallEnvelope, read: ReadHandle) -> bytes | None:
        entry = self._held.entry(READ, read.id)
        held = entry.value
        assert isinstance(held, _Read)
        if entry.cancelled and held.cancel is None:
            raise StoreError("STORE_CANCELLED", "The read was cancelled.", {})
        try:
            batch = held.reader.read_next_batch()
        except StopIteration:
            return None
        return batch_bytes(batch)

    def cancel_read(self, envelope: CallEnvelope, read: ReadHandle) -> None:
        def work() -> None:
            entry = self._held.entry(READ, read.id)
            entry.cancelled = True
            held = entry.value
            assert isinstance(held, _Read)
            if held.cancel is not None:
                held.cancel()

        self._held.once(read.id, "cancel_read", envelope.idempotency_key, work)

    def close_read(self, envelope: CallEnvelope, read: ReadHandle) -> None:
        def work() -> None:
            self._held.entry(READ, read.id)
            entry = self._held.remove(read.id)
            if entry is not None and isinstance(entry.value, _Read):
                entry.value.reader.close()

        self._held.once(STORE_SCOPE, f"close_read:{read.id}", envelope.idempotency_key, work)

    # --- write ---------------------------------------------------------------------------------

    def begin(self, envelope: CallEnvelope, lake: LakeHandle) -> TransactionHandle:
        def work() -> TransactionHandle:
            block = self._lake(lake).transaction()
            transaction = block.__enter__()
            identifier = self._held.add(TRANSACTION, _Write(block, transaction), lake.id)
            return TransactionHandle(identifier, transaction.started)

        return self._held.once(lake.id, "begin", envelope.idempotency_key, work)

    def execute(
        self, envelope: CallEnvelope, transaction: TransactionHandle, request: StatementRequest
    ) -> None:
        open_ = self._write(transaction).transaction
        self._held.once(
            transaction.id, "execute", envelope.idempotency_key, lambda: open_.execute(request)
        )

    def value(
        self, envelope: CallEnvelope, transaction: TransactionHandle, request: StatementRequest
    ) -> str | None:
        return self._write(transaction).transaction.value(request)

    def land(
        self,
        envelope: CallEnvelope,
        transaction: TransactionHandle,
        source: str,
        sql: str,
        parameters: tuple[Parameter, ...],
        schema: str,
        table: str,
        timeout_ms: int | None,
    ) -> int:
        open_ = self._write(transaction).transaction
        return self._held.once(
            transaction.id,
            "land",
            envelope.idempotency_key,
            lambda: open_.land(source, sql, parameters, schema, table, timeout_ms),
        )

    def load_arrow(
        self,
        envelope: CallEnvelope,
        transaction: TransactionHandle,
        schema: str,
        table: str,
        data: bytes,
        columns: tuple[tuple[str, str], ...],
        timeout_ms: int | None,
    ) -> int:
        open_ = self._write(transaction).transaction
        return self._held.once(
            transaction.id,
            "load_arrow",
            envelope.idempotency_key,
            lambda: open_.load_arrow(
                schema, table, stream_reader(data), columns, timeout_ms=timeout_ms
            ),
        )

    def cancel_transaction(self, envelope: CallEnvelope, transaction: TransactionHandle) -> None:
        def work() -> None:
            self._held.entry(TRANSACTION, transaction.id).cancelled = True
            self._write(transaction).transaction.cancel()

        self._held.once(transaction.id, "cancel", envelope.idempotency_key, work)

    def commit(self, envelope: CallEnvelope, transaction: TransactionHandle) -> CommitResult:
        def work() -> CommitResult:
            held = self._write(transaction)
            self._held.remove(transaction.id)
            held.block.__exit__(None, None, None)
            assert held.transaction.result is not None
            return held.transaction.result

        return self._held.once(
            STORE_SCOPE, f"commit:{transaction.id}", envelope.idempotency_key, work
        )

    def rollback(self, envelope: CallEnvelope, transaction: TransactionHandle) -> None:
        def work() -> None:
            held = self._write(transaction)
            self._held.remove(transaction.id)
            reason = _RolledBack()
            held.block.__exit__(_RolledBack, reason, None)

        self._held.once(STORE_SCOPE, f"rollback:{transaction.id}", envelope.idempotency_key, work)

    # --- maintenance ---------------------------------------------------------------------------

    def maintain(
        self,
        envelope: CallEnvelope,
        lake: LakeHandle,
        expire_before: dt.datetime,
        orphans_before: dt.datetime,
    ) -> MaintenanceReport:
        opened = self._lake(lake)
        return self._held.once(
            lake.id,
            "maintain",
            envelope.idempotency_key,
            lambda: opened.maintain(expire_before, orphans_before),
        )

    # --- existing lake -------------------------------------------------------------------------

    def open_existing_lake(
        self, envelope: CallEnvelope, attachment: ExistingLakeAttachment, limits: ExistingLakeLimits
    ) -> ExistingLakeHandle:
        def work() -> ExistingLakeHandle:
            lake = ExistingLake(
                attachment, memory_limit=limits.memory_limit, threads=limits.threads
            )
            try:
                reads = LakeReads(lake, timeout_ms=limits.timeout_ms)
            except BaseException:
                lake.close()
                raise
            return ExistingLakeHandle(self._held.add(EXISTING, _Existing(lake, reads)), lake.name)

        return self._held.once(STORE_SCOPE, "open_existing_lake", envelope.idempotency_key, work)

    def close_existing_lake(self, envelope: CallEnvelope, lake: ExistingLakeHandle) -> None:
        def work() -> None:
            opened = self._existing(lake)
            self._held.remove(lake.id)
            opened.lake.close()

        self._held.once(
            STORE_SCOPE, f"close_existing_lake:{lake.id}", envelope.idempotency_key, work
        )

    def existing_latest(self, envelope: CallEnvelope, lake: ExistingLakeHandle) -> Snapshot:
        return self._existing(lake).reads.latest()

    def existing_verify(
        self,
        envelope: CallEnvelope,
        lake: ExistingLakeHandle,
        snapshot: Snapshot,
        current: ExistingLakeAttachment,
    ) -> Snapshot:
        return self._existing(lake).reads.verify(snapshot, current)

    def existing_committed_at(
        self, envelope: CallEnvelope, lake: ExistingLakeHandle, snapshot: int
    ) -> dt.datetime | None:
        return self._existing(lake).reads.committed_at(snapshot)

    def existing_table_columns(
        self, envelope: CallEnvelope, lake: ExistingLakeHandle, snapshot: Snapshot
    ) -> TablesResult:
        return self._existing(lake).reads.table_columns(snapshot)

    def existing_views(
        self, envelope: CallEnvelope, lake: ExistingLakeHandle, snapshot: Snapshot
    ) -> tuple[str, ...]:
        return tuple(sorted(self._existing(lake).reads.views(snapshot)))

    def start_existing_read(
        self,
        envelope: CallEnvelope,
        lake: ExistingLakeHandle,
        request: ReadRequest,
        committed_at: dt.datetime,
    ) -> ReadHandle:
        reader = self._existing(lake).reads.read(request, committed_at)
        return self._reading(reader, None, lake.id)

    # --- clickhouse ----------------------------------------------------------------------------

    def connect_clickhouse(
        self, envelope: CallEnvelope, target: ClickHouseTarget, budget: Budget
    ) -> ClickHouseHandle:
        engine = ClickHouse.connect(target, budget)
        identifier = self._held.add(CLICKHOUSE, engine)
        return ClickHouseHandle(identifier, engine.database, engine.version)

    def close_clickhouse(self, envelope: CallEnvelope, engine: ClickHouseHandle) -> None:
        if engine.id in self._held.ids(CLICKHOUSE):
            self._held.remove(engine.id)

    def clickhouse_columns(
        self, envelope: CallEnvelope, engine: ClickHouseHandle, table: str
    ) -> tuple[SourceColumn, ...]:
        return self._clickhouse(engine).columns(table)

    def clickhouse_catalog(
        self, envelope: CallEnvelope, engine: ClickHouseHandle
    ) -> ClickHouseCatalog:
        tables, refused = self._clickhouse(engine).catalog()
        return ClickHouseCatalog(
            tables=tables, refused={name: error.to_message() for name, error in refused.items()}
        )

    def clickhouse_estimate(
        self,
        envelope: CallEnvelope,
        engine: ClickHouseHandle,
        sql: str,
        parameters: tuple[Parameter, ...],
    ) -> tuple[dict[str, str | int | float | bool | None], ...]:
        rows: list[dict[str, Any]] = self._clickhouse(engine).estimate(sql, parameters)
        return tuple(rows)

    def clickhouse_now(self, envelope: CallEnvelope, engine: ClickHouseHandle) -> dt.datetime:
        return self._clickhouse(engine).now()

    def start_clickhouse_read(
        self,
        envelope: CallEnvelope,
        engine: ClickHouseHandle,
        sql: str,
        parameters: tuple[Parameter, ...],
        columns: tuple[tuple[str, str], ...],
        timeout_ms: int | None,
    ) -> ReadHandle:
        reader = self._clickhouse(engine).read(sql, parameters, columns, timeout_ms)
        return self._reading(reader, None, engine.id)

    # --- encoding ------------------------------------------------------------------------------

    def json_frame(self, envelope: CallEnvelope, schema: bytes) -> JsonFrame:
        return JsonFrame(tuple(json_head(stream_schema(schema))), JSON_TAIL)

    def json_rows(self, envelope: CallEnvelope, batch: bytes, first: bool) -> tuple[str, ...]:
        return tuple(json_rows(one_batch(batch), first))

    # --- separation ----------------------------------------------------------------------------

    def is_object_storage(self, envelope: CallEnvelope, location: str) -> bool:
        return separation.is_object_storage(location)

    def location_components(
        self, envelope: CallEnvelope, name: str, location: str
    ) -> tuple[str, ...]:
        return separation.components(name, location)

    def locations_overlap(self, envelope: CallEnvelope, name: str, first: str, second: str) -> bool:
        return separation.overlaps(name, first, second)

    def check_apart(
        self,
        envelope: CallEnvelope,
        name: str,
        data_path: str,
        catalog: str | None,
        protected: tuple[str, ...],
        protected_files: tuple[str, ...],
    ) -> None:
        separation.check_apart(name, data_path, catalog, protected, protected_files)

    # --- what is held --------------------------------------------------------------------------

    def _lake(self, lake: LakeHandle) -> Lake:
        found = self._held.get(LAKE, lake.id)
        assert isinstance(found, Lake)
        return found

    def _write(self, transaction: TransactionHandle) -> _Write:
        found = self._held.get(TRANSACTION, transaction.id)
        assert isinstance(found, _Write)
        return found

    def _existing(self, lake: ExistingLakeHandle) -> _Existing:
        found = self._held.get(EXISTING, lake.id)
        assert isinstance(found, _Existing)
        return found

    def _clickhouse(self, engine: ClickHouseHandle) -> ClickHouse:
        found = self._held.get(CLICKHOUSE, engine.id)
        assert isinstance(found, ClickHouse)
        return found

    def _reading(
        self, reader: pa.RecordBatchReader, cancel: Callable[[], None] | None, owner: str
    ) -> ReadHandle:
        schema = stream_bytes(reader.schema)
        return ReadHandle(self._held.add(READ, _Read(reader, cancel), owner), schema)
