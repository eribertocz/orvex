"""The consumer's side of the store's interface contract: `StoreClient` and the lake, transaction
and read it opens, every call sent through a binding with its call envelope (#486 §2 item 6).

A consumer is given a binding of the chosen implementation (`orvex.contracts.binding.Bound`) and
the caller it acts for; it never knows the module behind it. Arrow arrives as IPC streams and is
handed on as a `pyarrow.RecordBatchReader`; a read the consumer drops or finishes is let go at
the store, so it never keeps an admission slot.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import pyarrow as pa
from orvex.contracts.binding import Bound
from orvex.contracts.envelope import CallEnvelope
from orvex.contracts.store.arrow_ipc import batch_bytes, one_batch, stream_bytes, stream_schema
from orvex.contracts.store.errors import StoreError
from orvex.contracts.store.interface import STORE, Location, Source
from orvex.contracts.store.v1 import (
    ChangesRequest,
    ChangesResult,
    CommitResult,
    LakeLocation,
    MaintenanceReport,
    Parameter,
    ReadRequest,
    Snapshot,
    SourceAttachment,
    StatementRequest,
    StatisticsRequest,
    StatisticsResult,
    TablesRequest,
    TablesResult,
)
from orvex.contracts.store.values import JsonFrame, LakeHandle, Limits, TransactionHandle
from orvex.contracts.store.values import ReadHandle as ReadTicket


@dataclass(frozen=True, slots=True)
class Caller:
    """Who a consumer's calls act for: the envelope fields besides the contract's own."""

    actor: str
    project_id: str
    revision: str
    correlation_id: str


class StoreClient:
    """The store, reached through `bound` for `caller`."""

    __slots__ = ("_bound", "_caller")

    def __init__(self, bound: Bound, caller: Caller) -> None:
        if bound.contract is not STORE:
            raise ValueError(f"a store client needs the store contract, not {bound.contract.name}")
        self._bound = bound
        self._caller = caller

    @property
    def caller(self) -> Caller:
        return self._caller

    def acting_for(self, caller: Caller) -> StoreClient:
        """The same store, for another caller (a new correlation id included)."""
        return StoreClient(self._bound, caller)

    def call(self, operation: str, /, *arguments: object) -> Any:  # noqa: ANN401 - its result
        changes = STORE.operation(operation).changes_state
        envelope = CallEnvelope(
            contract_name=STORE.name,
            contract_version=str(STORE.version),
            correlation_id=self._caller.correlation_id,
            actor=self._caller.actor,
            project_id=self._caller.project_id,
            revision=self._caller.revision,
            idempotency_key=uuid.uuid4().hex if changes else None,
        )
        return self._bound.call(operation, envelope, *arguments)

    def open_lake(
        self,
        location: LakeLocation | Location,
        limits: Limits,
        sources: Sequence[SourceAttachment | Source] = (),
    ) -> Lake:
        where = location.root if isinstance(location, LakeLocation) else location
        attached = tuple(s.root if isinstance(s, SourceAttachment) else s for s in sources)
        return Lake(self, self.call("open_lake", where, limits, attached))

    def reader(self, ticket: ReadTicket) -> pa.RecordBatchReader:
        """The Arrow stream of a running read; the read is let go when it ends or is dropped."""
        schema = stream_schema(ticket.schema)
        return pa.RecordBatchReader.from_batches(schema, _Batches(self, ticket))

    def arrow_to_json(self, reader: pa.RecordBatchReader) -> Iterator[str]:
        """The `ResultJson` document of `reader` as text fragments, one batch at a time;
        unsupported types are reported before any row is produced."""
        frame: JsonFrame = self.call("json_frame", stream_bytes(reader.schema))
        yield from frame.head
        first = True
        for batch in reader:
            fragments = self.call("json_rows", batch_bytes(batch, reader.schema), first)
            first = first and not fragments
            yield from fragments
        yield frame.tail

    def is_object_storage(self, location: str) -> bool:
        return bool(self.call("is_object_storage", location))

    def location_components(self, name: str, location: str) -> tuple[str, ...]:
        """The parts two locations are compared by: `object` then the bucket and each key
        segment, or `local` then the resolved path's parts; anything else is refused."""
        found: tuple[str, ...] = self.call("location_components", name, location)
        return found

    def locations_overlap(self, name: str, first: str, second: str) -> bool:
        return bool(self.call("locations_overlap", name, first, second))

    def check_apart(
        self,
        name: str,
        data_path: str,
        catalog: str | None,
        protected: Sequence[str],
        protected_files: Sequence[str] = (),
    ) -> None:
        self.call("check_apart", name, data_path, catalog, tuple(protected), tuple(protected_files))


class _Batches:
    """A read's batches, fetched one at a time; the read is let go once, at its end, on an
    error or when nothing refers to it any more."""

    def __init__(self, client: StoreClient, ticket: ReadTicket) -> None:
        self._client = client
        self._ticket = ticket
        self._open = True

    def __iter__(self) -> _Batches:
        return self

    def __next__(self) -> pa.RecordBatch:
        if not self._open:
            raise StopIteration
        try:
            data = self._client.call("read_batch", self._ticket)
        except BaseException:
            self.close()
            raise
        if data is None:
            self.close()
            raise StopIteration
        return one_batch(data)

    def close(self) -> None:
        if self._open:
            self._open = False
            self._client.call("close_read", self._ticket)

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self.close()


@dataclass(slots=True)
class ReadHandle:
    """A running read: its Arrow stream and a way to cancel it from another thread."""

    reader: pa.RecordBatchReader
    ticket: ReadTicket
    client: StoreClient

    def cancel(self) -> None:
        self.client.call("cancel_read", self.ticket)


class Lake:
    """One open lake. Reads may run concurrently; write transactions run one at a time."""

    def __init__(self, client: StoreClient, handle: LakeHandle) -> None:
        self._client = client
        self.handle = handle

    def close(self) -> None:
        self._client.call("close_lake", self.handle)

    def current_snapshot(self) -> Snapshot:
        found: Snapshot = self._client.call("current_snapshot", self.handle)
        return found

    def snapshot(self, snapshot_id: int) -> Snapshot | None:
        """The snapshot while the lake keeps it; None once it expired or if it never existed."""
        found: Snapshot | None = self._client.call("snapshot", self.handle, snapshot_id)
        return found

    def tables(self) -> set[tuple[str, str]]:
        """(schema, table) of every table of the lake now, lower-cased."""
        return set(self._client.call("tables", self.handle))

    def describe(self, sql: str, parameters: Sequence[Parameter] = ()) -> list[tuple[str, str]]:
        """(name, DuckDB type) of each output column of one SELECT, from the binder only."""
        return list(self._client.call("describe", self.handle, sql, tuple(parameters)))

    def describe_source(
        self, name: str, sql: str, parameters: Sequence[Parameter] = ()
    ) -> list[tuple[str, str]]:
        """The columns and the types they land with of `sql` on the database source `name`."""
        found = self._client.call("describe_source", self.handle, name, sql, tuple(parameters))
        return list(found)

    def statistics(self, request: StatisticsRequest) -> StatisticsResult:
        found: StatisticsResult = self._client.call("statistics", self.handle, request)
        return found

    def changes(self, request: ChangesRequest) -> ChangesResult:
        found: ChangesResult = self._client.call("changes", self.handle, request)
        return found

    def table_columns(self, request: TablesRequest) -> TablesResult:
        found: TablesResult = self._client.call("table_columns", self.handle, request)
        return found

    def read(self, request: ReadRequest) -> pa.RecordBatchReader:
        """Run one SELECT as of `request.snapshot` and return it once, as Arrow batches."""
        return self.start_read(request).reader

    def start_read(self, request: ReadRequest) -> ReadHandle:
        """Like `read`, with a handle that can cancel the read from another thread."""
        ticket: ReadTicket = self._client.call("start_read", self.handle, request)
        return ReadHandle(self._client.reader(ticket), ticket, self._client)

    @contextlib.contextmanager
    def transaction(self) -> Iterator[Transaction]:
        """Statements inside the block commit together to one snapshot, or roll back on error."""
        handle: TransactionHandle = self._client.call("begin", self.handle)
        transaction = Transaction(self._client, handle)
        try:
            yield transaction
        except BaseException:
            # The block's own error is the one raised; the rollback's could only hide it.
            with contextlib.suppress(StoreError):
                self._client.call("rollback", handle)
            raise
        transaction.result = self._client.call("commit", handle)

    def maintain(
        self, expire_before: dt.datetime, orphans_before: dt.datetime
    ) -> MaintenanceReport:
        """Expire, merge, rewrite and clean up the lake's files (`orvex.store` README)."""
        found: MaintenanceReport = self._client.call(
            "maintain", self.handle, expire_before, orphans_before
        )
        return found


class Transaction:
    """A write transaction; `result` is set when the block commits."""

    def __init__(self, client: StoreClient, handle: TransactionHandle) -> None:
        self._client = client
        self.handle = handle
        self.result: CommitResult | None = None

    @property
    def started(self) -> Snapshot | None:
        """The snapshot the transaction reads (#255)."""
        return self.handle.started

    def execute(self, request: StatementRequest) -> None:
        self._client.call("execute", self.handle, request)

    def value(self, request: StatementRequest) -> str | None:
        """The first column of the first row of one SELECT inside this transaction, as text."""
        found: str | None = self._client.call("value", self.handle, request)
        return found

    def land(
        self,
        source: str,
        sql: str,
        parameters: Sequence[Parameter],
        schema: str,
        table: str,
        timeout_ms: int | None = None,
    ) -> int:
        """Replace `schema.table` with what `sql` returns on the database source `source`."""
        found: int = self._client.call(
            "land", self.handle, source, sql, tuple(parameters), schema, table, timeout_ms
        )
        return found

    def load_arrow(
        self,
        schema: str,
        table: str,
        reader: pa.RecordBatchReader,
        columns: Sequence[tuple[str, str]],
        timeout_ms: int | None = None,
    ) -> int:
        """Create `schema.table` from an Arrow stream, each column cast to its DuckDB type."""
        data = stream_bytes(reader.schema, reader)
        found: int = self._client.call(
            "load_arrow", self.handle, schema, table, data, tuple(columns), timeout_ms
        )
        return found

    def cancel(self) -> None:
        """Cancel from any thread: a running statement is interrupted, and the block rolls back."""
        self._client.call("cancel_transaction", self.handle)
