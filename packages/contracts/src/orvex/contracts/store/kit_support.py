"""What the store's conformance kit (`orvex.contracts.store.kit`) builds its cases from: lakes in
folders of their own, statements, reads decoded from Arrow IPC, refusals by code, and the state
an idempotency case sets up once per implementation."""

from __future__ import annotations

import atexit
import shutil
import sqlite3
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow as pa
from orvex.contracts.kit_client import KitClient
from orvex.contracts.store.arrow_ipc import one_batch, stream_schema
from orvex.contracts.store.errors import StoreError
from orvex.contracts.store.v1 import (
    CommitResult,
    ExistingLakeAttachment,
    LocalLakeLocation,
    ReadRequest,
    Snapshot,
    SqliteLakeLocation,
    SqliteSourceAttachment,
    StatementRequest,
)
from orvex.contracts.store.values import (
    ExistingLakeHandle,
    ExistingLakeLimits,
    LakeHandle,
    Limits,
    ReadHandle,
)

LIMITS = Limits(
    memory_limit="256MB", threads=1, max_concurrent=4, queue_size=16, default_timeout_ms=60_000
)
EXISTING_LIMITS = ExistingLakeLimits(memory_limit="256MB", threads=1, timeout_ms=60_000)
_ROOT: list[Path] = []


def folder() -> Path:
    """A new empty folder, removed when the process ends."""
    if not _ROOT:
        _ROOT.append(Path(tempfile.mkdtemp(prefix="store-kit-")))
        atexit.register(shutil.rmtree, _ROOT[0], ignore_errors=True)
    return Path(tempfile.mkdtemp(dir=_ROOT[0]))


def local_location() -> LocalLakeLocation:
    return LocalLakeLocation(kind="lake_location", catalog="local", path=str(folder()))


def sqlite_source(name: str = "field") -> SqliteSourceAttachment:
    """A SQLite file with one table `meters (id INTEGER, name TEXT)` of two rows."""
    path = folder() / f"{name}.sqlite"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE meters (id INTEGER, name TEXT)")
        connection.executemany("INSERT INTO meters VALUES (?, ?)", [(1, "north"), (2, "south")])
        connection.commit()
    finally:
        connection.close()
    return SqliteSourceAttachment(
        kind="source_attachment", name=name, type="sqlite", path=str(path)
    )


def statement(sql: str) -> StatementRequest:
    return StatementRequest(kind="statement", sql=sql, parameters=[])


def read_request(sql: str, snapshot: int) -> ReadRequest:
    return ReadRequest(kind="read", sql=sql, parameters=[], snapshot=snapshot, batch_size=1024)


def open_lake(client: KitClient, sources: tuple[SqliteSourceAttachment, ...] = ()) -> LakeHandle:
    found: LakeHandle = client.open_lake(local_location(), LIMITS, sources)
    return found


def write(client: KitClient, lake: LakeHandle, *sqls: str) -> CommitResult:
    """One transaction of `sqls`, committed."""
    transaction = client.begin(lake)
    for sql in sqls:
        client.execute(transaction, statement(sql))
    found: CommitResult = client.commit(transaction)
    return found


def commit(client: KitClient, lake: LakeHandle, *sqls: str) -> Snapshot:
    """The snapshot one committed transaction of `sqls` made."""
    snapshot = write(client, lake, *sqls).snapshot
    check(snapshot is not None, "a commit reports the snapshot it made")
    assert snapshot is not None
    return snapshot


def drain(client: KitClient, read: ReadHandle) -> pa.Table:
    """Every batch of a read, then the read let go."""
    schema = stream_schema(read.schema)
    batches = []
    try:
        while (data := client.read_batch(read)) is not None:
            batches.append(one_batch(data))
    finally:
        client.close_read(read)
    return pa.Table.from_batches(batches, schema=schema)


def rows(client: KitClient, lake: LakeHandle, sql: str) -> list[dict[str, Any]]:
    snapshot = client.current_snapshot(lake).id
    table: list[dict[str, Any]] = drain(
        client, client.start_read(lake, read_request(sql, snapshot))
    ).to_pylist()
    return table


def existing_lake(client: KitClient) -> tuple[ExistingLakeHandle, ExistingLakeAttachment]:
    """An existing DuckLake another system wrote (a SQLite catalog in WAL mode, one table
    `main.readings` of one DECIMAL row), attached read-only."""
    root = folder()
    location = SqliteLakeLocation(
        kind="lake_location",
        catalog="sqlite",
        path=str(root / "catalog.sqlite"),
        data_path=(root / "data").as_posix() + "/",
    )
    owner = client.open_lake(location, LIMITS, ())
    write(client, owner, "CREATE TABLE readings AS SELECT 12.50::DECIMAL(18,2) AS amount")
    client.close_lake(owner)
    connection = sqlite3.connect(location.path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
    finally:
        connection.close()
    attachment = ExistingLakeAttachment(
        kind="existing_lake_attachment",
        name="owner",
        location=location,
        roles_in_use=[],
        protected_files=[],
        credentials_in_use=[],
        protected_locations=[],
    )
    handle: ExistingLakeHandle = client.open_existing_lake(attachment, EXISTING_LIMITS)
    return handle, attachment


def check(condition: bool, text: str) -> None:
    if not condition:
        raise AssertionError(text)


def refused(code: str, call: Callable[[], object]) -> StoreError:
    """The store's refusal of `call` with `code`; anything else fails the case."""
    try:
        call()
    except StoreError as error:
        check(error.code == code, f"expected {code}, got {error.code}: {error.message}")
        return error
    raise AssertionError(f"expected {code}, the call was accepted")


class Scenario:
    """The state one idempotency case sets up once for the client it is run with."""

    def __init__(self, setup: Callable[[KitClient], Any]) -> None:
        self._setup = setup
        self._current: tuple[KitClient, Any] | None = None

    def of(self, client: KitClient) -> Any:  # noqa: ANN401 - the case's own state
        if self._current is None or self._current[0] is not client:
            self._current = (client, self._setup(client))
        return self._current[1]
