"""The values the store's interface contract (`orvex.contracts.store.interface`) carries besides
the generated types of `contracts/store/v1`: limits, connection targets, columns and the
handles that name what the store holds open for a caller.

A handle is a plain value: the store keeps the open lake, transaction, read or connection, and
the handle names it by an id the store chose (unguessable, never reused). A handle that names
nothing the store holds is refused with STORE_PARAMETER_INVALID.
"""

from __future__ import annotations

from dataclasses import dataclass

import pyarrow as pa
from orvex.contracts.store.v1 import Snapshot, StoreError


@dataclass(frozen=True, slots=True)
class Limits:
    """Resource limits of a lake; every field is required (law 7).

    `max_concurrent` statements (reads in progress and open transactions) run at once; up to
    `queue_size` more wait in arrival order; `default_timeout_ms` applies when a request sets no
    `timeout_ms`.
    """

    memory_limit: str | None
    threads: int | None
    max_concurrent: int | None = None
    queue_size: int | None = None
    default_timeout_ms: int | None = None


@dataclass(frozen=True, slots=True)
class ExistingLakeLimits:
    """The limits of one attached existing lake: its instance's memory and threads, and the time
    limit of each of its reads that sets none."""

    memory_limit: str
    threads: int
    timeout_ms: int


@dataclass(frozen=True, slots=True)
class ClickHouseTarget:
    """Where the server is and who connects; the password only as an environment variable."""

    host: str
    port: int
    database: str
    user: str
    password_env: str
    tls: bool = True
    ca_file: str | None = None  # absent: the operating system's authorities


@dataclass(frozen=True, slots=True)
class Budget:
    """The most one query may use: the server's limits may be tighter, never looser."""

    timeout_ms: int
    memory_bytes: int


@dataclass(frozen=True, slots=True)
class SourceColumn:
    name: str
    engine_type: str  # as ClickHouse names it
    type: str  # the DuckDB type it maps to


@dataclass(frozen=True, slots=True)
class ClickHouseCatalog:
    """Every table of a ClickHouse database with its columns' exact types, in order; apart, each
    table holding a column with no exact type, as the error naming that column."""

    tables: dict[str, tuple[SourceColumn, ...]]
    refused: dict[str, StoreError]


@dataclass(frozen=True, slots=True)
class JsonFrame:
    """What a `ResultJson` document holds around its rows: the fragments before the first row
    (its columns) and the one after the last."""

    head: tuple[str, ...]
    tail: str


@dataclass(frozen=True, slots=True)
class LakeHandle:
    id: str


@dataclass(frozen=True, slots=True)
class TransactionHandle:
    """An open write transaction and the snapshot it reads (#255)."""

    id: str
    started: Snapshot | None


@dataclass(frozen=True, slots=True)
class ReadHandle:
    """A running read: its Arrow schema, as a table with no row, then its batches one at a time
    (`read_batch`)."""

    id: str
    schema: pa.Table


@dataclass(frozen=True, slots=True)
class ExistingLakeHandle:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class ClickHouseHandle:
    """A verified connection: the database every read names its tables in, the server's
    version."""

    id: str
    database: str
    version: str


@dataclass(frozen=True, slots=True)
class Held:
    """The ids of what the store holds open now, each kind in order; the cancelled
    transactions and reads among them."""

    lakes: tuple[str, ...]
    transactions: tuple[str, ...]
    reads: tuple[str, ...]
    existing_lakes: tuple[str, ...]
    clickhouse: tuple[str, ...]
    cancelled: tuple[str, ...]
