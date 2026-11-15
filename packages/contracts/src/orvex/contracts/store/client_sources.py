"""The consumer's side of the store contract's sources besides managed lakes: an existing
DuckLake attached read-only, and a verified ClickHouse connection, each reached through a
`StoreClient` (`orvex.contracts.store.client`)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any

import pyarrow as pa
from orvex.contracts.store.client import StoreClient
from orvex.contracts.store.errors import StoreError
from orvex.contracts.store.v1 import (
    ExistingLakeAttachment,
    Parameter,
    ReadRequest,
    Snapshot,
    TablesResult,
)
from orvex.contracts.store.values import (
    Budget,
    ClickHouseCatalog,
    ClickHouseHandle,
    ClickHouseTarget,
    ExistingLakeHandle,
    ExistingLakeLimits,
    SourceColumn,
)


class ExistingLakeReads:
    """What may be asked of one attached existing lake, each call in turn and under a deadline."""

    def __init__(self, client: StoreClient, handle: ExistingLakeHandle) -> None:
        self._client = client
        self.handle = handle

    @property
    def name(self) -> str:
        return self.handle.name

    def read(self, request: ReadRequest, committed_at: dt.datetime) -> pa.RecordBatchReader:
        """`request`'s statement at `request.snapshot`, which must still have been committed at
        `committed_at`."""
        ticket = self._client.call("start_existing_read", self.handle, request, committed_at)
        return self._client.reader(ticket)

    def table_columns(self, snapshot: Snapshot) -> TablesResult:
        found: TablesResult = self._client.call("existing_table_columns", self.handle, snapshot)
        return found

    def views(self, snapshot: Snapshot) -> frozenset[str]:
        """`schema.view` of every view of the lake alive at `snapshot`, lower-cased."""
        return frozenset(self._client.call("existing_views", self.handle, snapshot))

    def committed_at(self, snapshot: int) -> dt.datetime | None:
        found: dt.datetime | None = self._client.call(
            "existing_committed_at", self.handle, snapshot
        )
        return found

    def latest(self) -> Snapshot:
        found: Snapshot = self._client.call("existing_latest", self.handle)
        return found

    def verify(self, snapshot: Snapshot, current: ExistingLakeAttachment) -> Snapshot:
        found: Snapshot = self._client.call("existing_verify", self.handle, snapshot, current)
        return found

    def close(self) -> None:
        self._client.call("close_existing_lake", self.handle)


def open_existing_lake(
    client: StoreClient, attachment: ExistingLakeAttachment, limits: ExistingLakeLimits
) -> ExistingLakeReads:
    """Attach an existing DuckLake read-only, its catalog and sandbox checked first."""
    return ExistingLakeReads(client, client.call("open_existing_lake", attachment, limits))


class ClickHouse:
    """One verified connection to one ClickHouse database."""

    def __init__(self, client: StoreClient, handle: ClickHouseHandle) -> None:
        self._client = client
        self.handle = handle

    @property
    def database(self) -> str:
        """The database every read names its tables in."""
        return self.handle.database

    @property
    def version(self) -> str:
        return self.handle.version

    def columns(self, table: str) -> tuple[SourceColumn, ...]:
        found: tuple[SourceColumn, ...] = self._client.call(
            "clickhouse_columns", self.handle, table
        )
        return found

    def catalog(self) -> tuple[dict[str, tuple[SourceColumn, ...]], dict[str, StoreError]]:
        """Every table with its columns' exact types; apart, each table holding a column with no
        exact type, as the error naming that column."""
        found: ClickHouseCatalog = self._client.call("clickhouse_catalog", self.handle)
        refused = {
            table: StoreError(
                message.code.root,
                message.message,
                {detail.name: detail.value for detail in message.details},
            )
            for table, message in found.refused.items()
        }
        return dict(found.tables), refused

    def estimate(self, sql: str, parameters: Sequence[Parameter]) -> list[dict[str, Any]]:
        """`EXPLAIN ESTIMATE` of one read, checked and bound as the read is."""
        found = self._client.call("clickhouse_estimate", self.handle, sql, tuple(parameters))
        return [dict(row) for row in found]

    def now(self) -> dt.datetime:
        """The server's clock: a live read's instant (ADR-0027 §5)."""
        found: dt.datetime = self._client.call("clickhouse_now", self.handle)
        return found

    def read(
        self,
        sql: str,
        parameters: Sequence[Parameter],
        columns: Sequence[tuple[str, str]],
        timeout_ms: int | None = None,
    ) -> pa.RecordBatchReader:
        """Run one SELECT and return its result as Arrow in the store contract's types."""
        ticket = self._client.call(
            "start_clickhouse_read", self.handle, sql, tuple(parameters), tuple(columns), timeout_ms
        )
        return self._client.reader(ticket)

    def close(self) -> None:
        self._client.call("close_clickhouse", self.handle)


def connect_clickhouse(client: StoreClient, target: ClickHouseTarget, budget: Budget) -> ClickHouse:
    """Open and verify a connection: nothing is read unless every check passes."""
    return ClickHouse(client, client.call("connect_clickhouse", target, budget))
