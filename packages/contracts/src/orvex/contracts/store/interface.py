"""The store's interface contract (#486 §5 step 2, ADR-0033): every operation its consumers use,
by capability, with the codes each may raise (`orvex.contracts.store.errors.StoreError`).

Each operation takes the call envelope first. Arrow crosses as tables (the codec's Arrow type): a
read's schema in its handle and then one batch per `read_batch`, never a live reader. What the
store holds open (lakes, transactions, reads, connections) is named by a handle
(`orvex.contracts.store.values`); the consumer's facade is `orvex.contracts.store.client`.

An operation changes state when it changes a lake, or what later calls see of a lake, a
transaction or a read. A ClickHouse handle only names a verified target (each call opens its
own HTTP connection and nothing in ClickHouse changes), so connecting and closing do not;
closing a handle that names nothing is harmless.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Protocol

import pyarrow as pa
from orvex.contracts.envelope import CallEnvelope
from orvex.contracts.interface import InterfaceContract, Operation
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
from orvex.contracts.version import Version

Location = LocalLakeLocation | SqliteLakeLocation | PostgresLakeLocation
Source = FileSourceAttachment | SqliteSourceAttachment | PostgresSourceAttachment
Columns = tuple[tuple[str, str], ...]
EstimateRow = dict[str, str | int | float | bool | None]


class Store(Protocol):
    # --- lake ----------------------------------------------------------------------------------
    def open_lake(
        self,
        envelope: CallEnvelope,
        location: Location,
        limits: Limits,
        sources: tuple[Source, ...],
    ) -> LakeHandle: ...

    def close_lake(self, envelope: CallEnvelope, lake: LakeHandle) -> None: ...

    def held(self, envelope: CallEnvelope) -> Held: ...

    def current_snapshot(self, envelope: CallEnvelope, lake: LakeHandle) -> Snapshot: ...

    def snapshot(
        self, envelope: CallEnvelope, lake: LakeHandle, snapshot_id: int
    ) -> Snapshot | None: ...

    def tables(self, envelope: CallEnvelope, lake: LakeHandle) -> tuple[tuple[str, str], ...]: ...

    def describe(
        self,
        envelope: CallEnvelope,
        lake: LakeHandle,
        sql: str,
        parameters: tuple[Parameter, ...],
    ) -> Columns: ...

    def describe_source(
        self,
        envelope: CallEnvelope,
        lake: LakeHandle,
        name: str,
        sql: str,
        parameters: tuple[Parameter, ...],
    ) -> Columns: ...

    def statistics(
        self, envelope: CallEnvelope, lake: LakeHandle, request: StatisticsRequest
    ) -> StatisticsResult: ...

    def changes(
        self, envelope: CallEnvelope, lake: LakeHandle, request: ChangesRequest
    ) -> ChangesResult: ...

    def table_columns(
        self, envelope: CallEnvelope, lake: LakeHandle, request: TablesRequest
    ) -> TablesResult: ...

    # --- read ----------------------------------------------------------------------------------
    def start_read(
        self, envelope: CallEnvelope, lake: LakeHandle, request: ReadRequest
    ) -> ReadHandle: ...

    def read_batch(self, envelope: CallEnvelope, read: ReadHandle) -> pa.Table | None: ...

    def cancel_read(self, envelope: CallEnvelope, read: ReadHandle) -> None: ...

    def close_read(self, envelope: CallEnvelope, read: ReadHandle) -> None: ...

    # --- write ---------------------------------------------------------------------------------
    def begin(self, envelope: CallEnvelope, lake: LakeHandle) -> TransactionHandle: ...

    def execute(
        self, envelope: CallEnvelope, transaction: TransactionHandle, request: StatementRequest
    ) -> None: ...

    def value(
        self, envelope: CallEnvelope, transaction: TransactionHandle, request: StatementRequest
    ) -> str | None: ...

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
    ) -> int: ...

    def load_arrow(
        self,
        envelope: CallEnvelope,
        transaction: TransactionHandle,
        schema: str,
        table: str,
        data: pa.Table,
        columns: Columns,
        timeout_ms: int | None,
    ) -> int: ...

    def cancel_transaction(
        self, envelope: CallEnvelope, transaction: TransactionHandle
    ) -> None: ...

    def commit(self, envelope: CallEnvelope, transaction: TransactionHandle) -> CommitResult: ...

    def rollback(self, envelope: CallEnvelope, transaction: TransactionHandle) -> None: ...

    # --- maintenance ---------------------------------------------------------------------------
    def maintain(
        self,
        envelope: CallEnvelope,
        lake: LakeHandle,
        expire_before: datetime,
        orphans_before: datetime,
    ) -> MaintenanceReport: ...

    # --- existing lake -------------------------------------------------------------------------
    def open_existing_lake(
        self,
        envelope: CallEnvelope,
        attachment: ExistingLakeAttachment,
        limits: ExistingLakeLimits,
    ) -> ExistingLakeHandle: ...

    def close_existing_lake(self, envelope: CallEnvelope, lake: ExistingLakeHandle) -> None: ...

    def existing_latest(self, envelope: CallEnvelope, lake: ExistingLakeHandle) -> Snapshot: ...

    def existing_verify(
        self,
        envelope: CallEnvelope,
        lake: ExistingLakeHandle,
        snapshot: Snapshot,
        current: ExistingLakeAttachment,
    ) -> Snapshot: ...

    def existing_committed_at(
        self, envelope: CallEnvelope, lake: ExistingLakeHandle, snapshot: int
    ) -> datetime | None: ...

    def existing_table_columns(
        self, envelope: CallEnvelope, lake: ExistingLakeHandle, snapshot: Snapshot
    ) -> TablesResult: ...

    def existing_views(
        self, envelope: CallEnvelope, lake: ExistingLakeHandle, snapshot: Snapshot
    ) -> tuple[str, ...]: ...

    def start_existing_read(
        self,
        envelope: CallEnvelope,
        lake: ExistingLakeHandle,
        request: ReadRequest,
        committed_at: datetime,
    ) -> ReadHandle: ...

    # --- clickhouse ----------------------------------------------------------------------------
    def connect_clickhouse(
        self, envelope: CallEnvelope, target: ClickHouseTarget, budget: Budget
    ) -> ClickHouseHandle: ...

    def close_clickhouse(self, envelope: CallEnvelope, engine: ClickHouseHandle) -> None: ...

    def clickhouse_columns(
        self, envelope: CallEnvelope, engine: ClickHouseHandle, table: str
    ) -> tuple[SourceColumn, ...]: ...

    def clickhouse_catalog(
        self, envelope: CallEnvelope, engine: ClickHouseHandle
    ) -> ClickHouseCatalog: ...

    def clickhouse_estimate(
        self,
        envelope: CallEnvelope,
        engine: ClickHouseHandle,
        sql: str,
        parameters: tuple[Parameter, ...],
    ) -> tuple[EstimateRow, ...]: ...

    def clickhouse_now(self, envelope: CallEnvelope, engine: ClickHouseHandle) -> datetime: ...

    def start_clickhouse_read(
        self,
        envelope: CallEnvelope,
        engine: ClickHouseHandle,
        sql: str,
        parameters: tuple[Parameter, ...],
        columns: Columns,
        timeout_ms: int | None,
    ) -> ReadHandle: ...

    # --- encoding ------------------------------------------------------------------------------
    def json_frame(self, envelope: CallEnvelope, schema: pa.Table) -> JsonFrame: ...

    def json_rows(self, envelope: CallEnvelope, rows: pa.Table, first: bool) -> tuple[str, ...]: ...

    # --- separation ----------------------------------------------------------------------------
    def is_object_storage(self, envelope: CallEnvelope, location: str) -> bool: ...

    def location_components(
        self, envelope: CallEnvelope, name: str, location: str
    ) -> tuple[str, ...]: ...

    def locations_overlap(
        self, envelope: CallEnvelope, name: str, first: str, second: str
    ) -> bool: ...

    def check_apart(
        self,
        envelope: CallEnvelope,
        name: str,
        data_path: str,
        catalog: str | None,
        protected: tuple[str, ...],
        protected_files: tuple[str, ...],
    ) -> None: ...


# The settings a ClickHouse reader's profile must fix, each to this value, for a read to mean
# what the semantic engine wrote (the `clickhouse` capability refuses a connection without them).
REQUIRED_SETTINGS: Mapping[str, str] = {
    "join_use_nulls": "1",  # an outer join gives NULL, never a type's default value
    "group_by_use_nulls": "1",  # a subtotal's key is NULL, never a default value
    "transform_null_in": "0",  # NULL membership is written out by the adapter
    "count_distinct_implementation": "uniqExact",  # an exact distinct count
    "cast_keep_nullable": "1",  # CAST keeps NULL
    "aggregate_functions_null_for_empty": "0",  # empty-set results are written out by the adapter
    "cancel_http_readonly_queries_on_client_close": "1",  # closing the connection cancels
}


# Every code of `contracts/store/v1`: each operation reaches DuckDB, a source or a check that
# may refuse with any of them, so each declares them all.
CODES = (
    "STORE_SNAPSHOT_NOT_FOUND",
    "STORE_SNAPSHOT_EXPIRED",
    "STORE_STATEMENT_INVALID",
    "STORE_QUERY_FAILED",
    "STORE_PARAMETER_INVALID",
    "STORE_LIMITS_REQUIRED",
    "STORE_EXTENSION_UNAVAILABLE",
    "STORE_TRANSACTION_CONFLICT",
    "STORE_SECRET_MISSING",
    "STORE_SECRET_INLINE",
    "STORE_CATALOG_UNAVAILABLE",
    "STORE_LOCATION_INVALID",
    "STORE_TLS_REQUIRED",
    "STORE_TYPE_UNSUPPORTED",
    "STORE_TIMEOUT",
    "STORE_CANCELLED",
    "STORE_BUSY",
    "SOURCE_NOT_FOUND",
    "STORE_SOURCE_INVALID",
    "STORE_SOURCE_FAILED",
    "STORE_CONNECTION_FAILED",
    "UNSUPPORTED_SOURCE_TYPE",
    "SOURCE_NOT_READ_ONLY",
    "ENGINE_VERSION_UNTESTED",
    "ENGINE_SETTING_REQUIRED",
)


def _operation(name: str, capability: str, *, changes_state: bool = False) -> Operation:
    return Operation(name, capability, CODES, changes_state=changes_state)


STORE = InterfaceContract(
    name="store",
    version=Version(1, 0),
    protocol=Store,
    error_type=StoreError,
    capabilities=(
        "lake",
        "read",
        "write",
        "maintenance",
        "existing-lake",
        "clickhouse",
        "encoding",
        "separation",
    ),
    errors=CODES,
    operations=(
        _operation("open_lake", "lake", changes_state=True),
        _operation("close_lake", "lake", changes_state=True),
        _operation("held", "lake"),
        _operation("current_snapshot", "lake"),
        _operation("snapshot", "lake"),
        _operation("tables", "lake"),
        _operation("describe", "lake"),
        _operation("describe_source", "lake"),
        _operation("statistics", "lake"),
        _operation("changes", "lake"),
        _operation("table_columns", "lake"),
        _operation("start_read", "read"),
        _operation("read_batch", "read"),
        _operation("cancel_read", "read", changes_state=True),
        _operation("close_read", "read", changes_state=True),
        _operation("begin", "write", changes_state=True),
        _operation("execute", "write", changes_state=True),
        _operation("value", "write"),
        _operation("land", "write", changes_state=True),
        _operation("load_arrow", "write", changes_state=True),
        _operation("cancel_transaction", "write", changes_state=True),
        _operation("commit", "write", changes_state=True),
        _operation("rollback", "write", changes_state=True),
        _operation("maintain", "maintenance", changes_state=True),
        _operation("open_existing_lake", "existing-lake", changes_state=True),
        _operation("close_existing_lake", "existing-lake", changes_state=True),
        _operation("existing_latest", "existing-lake"),
        _operation("existing_verify", "existing-lake"),
        _operation("existing_committed_at", "existing-lake"),
        _operation("existing_table_columns", "existing-lake"),
        _operation("existing_views", "existing-lake"),
        _operation("start_existing_read", "existing-lake"),
        _operation("connect_clickhouse", "clickhouse"),
        _operation("close_clickhouse", "clickhouse"),
        _operation("clickhouse_columns", "clickhouse"),
        _operation("clickhouse_catalog", "clickhouse"),
        _operation("clickhouse_estimate", "clickhouse"),
        _operation("clickhouse_now", "clickhouse"),
        _operation("start_clickhouse_read", "clickhouse"),
        _operation("json_frame", "encoding"),
        _operation("json_rows", "encoding"),
        _operation("is_object_storage", "separation"),
        _operation("location_components", "separation"),
        _operation("locations_overlap", "separation"),
        _operation("check_apart", "separation"),
    ),
)
