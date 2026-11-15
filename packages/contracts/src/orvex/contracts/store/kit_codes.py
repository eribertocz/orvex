"""The cases of the store's kit that prove every error code each operation declares: one case per
(operation, code), which makes that operation refuse with that code. The kit is refused when it
is built if a declared code has no such case, or a case proves an undeclared one
(KIT_CODE_UNPROVEN): no code is declared that the kit does not prove.
"""

from __future__ import annotations

import datetime as dt
import functools
import os
import sqlite3
import threading
from collections.abc import Callable

import pyarrow as pa
from orvex.contracts.interface import InterfaceContract
from orvex.contracts.interface_errors import InterfaceContractError
from orvex.contracts.kit import CaseEnvironment, KitCase
from orvex.contracts.kit_client import KitClient
from orvex.contracts.store.kit_services import CLICKHOUSE_PASSWORD, SAFE, clickhouse
from orvex.contracts.store.kit_support import (
    EXISTING_LIMITS,
    LIMITS,
    commit,
    existing_lake,
    folder,
    local_location,
    open_lake,
    read_request,
    refused,
    sqlite_source,
    statement,
)
from orvex.contracts.store.v1 import (
    ChangesRequest,
    Parameter,
    PostgresLakeLocation,
    SqliteLakeLocation,
    StatementRequest,
    StatisticsRequest,
    TablesRequest,
)
from orvex.contracts.store.values import (
    Budget,
    ClickHouseHandle,
    ClickHouseTarget,
    ExistingLakeHandle,
    ExistingLakeLimits,
    LakeHandle,
    Limits,
    ReadHandle,
    TransactionHandle,
)

type Provoke = Callable[[KitClient], Callable[[], object]]

HEAVY = "SELECT count(*) AS n FROM range(4000000000) AS a"
BUDGET = Budget(timeout_ms=30_000, memory_bytes=1_000_000_000)
UNSET = "STORE_KIT_UNSET_PASSWORD"  # never set
SET = "STORE_KIT_SET_PASSWORD"  # set to a value no server accepts
FORGED = {
    "lake": LakeHandle("lake-forged"),
    "read": ReadHandle("read-forged", pa.table({})),
    "transaction": TransactionHandle("transaction-forged", None),
    "existing": ExistingLakeHandle("existing-lake-forged", "forged"),
    "clickhouse": ClickHouseHandle("clickhouse-forged", "db", ""),
}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _bad_parameter(sql: str = "SELECT $p::BIGINT AS p") -> tuple[str, tuple[Parameter, ...]]:
    return sql, (Parameter.model_validate({"name": "p", "type": "BIGINT", "value": "x"}),)


def _snapshot(client: KitClient, lake: LakeHandle) -> int:
    found: int = client.current_snapshot(lake).id
    return found


def _postgres(**changes: str) -> PostgresLakeLocation:
    """A PostgreSQL catalog on a port nothing listens on, by default with a set password."""
    os.environ[SET] = "kit-not-a-password"
    found = {
        "kind": "lake_location",
        "catalog": "postgres",
        "connection": "host=127.0.0.1 port=1 dbname=kit user=kit",
        "password_env": SET,
        "data_path": str(folder()) + "/",
        **changes,
    }
    return PostgresLakeLocation.model_validate(found)


# --- what a lake case starts from ----------------------------------------------------------------


def _lake(client: KitClient) -> LakeHandle:
    lake = open_lake(client, (sqlite_source(),))
    commit(client, lake, "CREATE TABLE t (n BIGINT)", "INSERT INTO t VALUES (1), (2)")
    return lake


def _expired(client: KitClient) -> tuple[LakeHandle, int]:
    """A lake whose first snapshot maintenance expired."""
    lake = open_lake(client)
    first = commit(client, lake, "CREATE TABLE t AS SELECT 1 AS n").id
    commit(client, lake, "INSERT INTO t VALUES (2)")
    client.maintain(lake, _now(), _now())
    return lake, first


def _full(client: KitClient) -> LakeHandle:
    """A lake of one slot and no queue, whose slot a read holds."""
    lake = client.open_lake(local_location(), Limits("256MB", 1, 1, 0, 60_000), ())
    snapshot = commit(client, lake, "CREATE TABLE t AS SELECT 1 AS n").id
    client.start_read(lake, read_request("SELECT * FROM t", snapshot))
    found: LakeHandle = lake
    return found


def _transaction(client: KitClient) -> TransactionHandle:
    found: TransactionHandle = client.begin(_lake(client))
    return found


def _cancelled(client: KitClient) -> TransactionHandle:
    transaction = _transaction(client)
    client.cancel_transaction(transaction)
    return transaction


def _timed(sql: str, timeout_ms: int = 50) -> StatementRequest:
    return StatementRequest(kind="statement", sql=sql, parameters=[], timeout_ms=timeout_ms)


def _forged(kind: str, operation: str, *arguments: object) -> Provoke:
    return lambda client: functools.partial(
        client.send, operation, client.envelope(operation), FORGED[kind], *arguments
    )


# --- lake ----------------------------------------------------------------------------------------


def _source_conflict(client: KitClient) -> Callable[[], object]:
    source = sqlite_source()
    return lambda: client.open_lake(local_location(), LIMITS, (source, source))


def _missing_source(client: KitClient) -> Callable[[], object]:
    source = sqlite_source().model_copy(update={"path": "/nonexistent/kit.sqlite"})
    return lambda: client.open_lake(local_location(), LIMITS, (source,))


def _remote_data_without_storage(client: KitClient) -> Callable[[], object]:
    root = folder()
    location = SqliteLakeLocation(
        kind="lake_location",
        catalog="sqlite",
        path=str(root / "catalog.sqlite"),
        data_path="s3://kit-bucket/lake/",
    )
    return lambda: client.open_lake(location, LIMITS, ())


def _describe_refusal(client: KitClient) -> Callable[[], object]:
    lake = _lake(client)
    return lambda: client.describe(lake, "DELETE FROM t", ())


def _describe_failure(client: KitClient) -> Callable[[], object]:
    lake = _lake(client)
    return lambda: client.describe(lake, "SELECT nothing_here FROM t", ())


def _describe_parameter(client: KitClient) -> Callable[[], object]:
    lake = _lake(client)
    sql, parameters = _bad_parameter()
    return lambda: client.describe(lake, sql, parameters)


def _source_refusal(sql: str, name: str = "field") -> Provoke:
    def provoke(client: KitClient) -> Callable[[], object]:
        lake = _lake(client)
        return lambda: client.describe_source(lake, name, sql, ())

    return provoke


def _statistics_at(table: str, which: str) -> Provoke:
    def provoke(client: KitClient) -> Callable[[], object]:
        if which == "expired":
            lake, snapshot = _expired(client)
        else:
            lake = _lake(client)
            snapshot = 999_999 if which == "unknown" else _snapshot(client, lake)
        request = StatisticsRequest.model_validate(
            {"kind": "statistics", "tables": [table], "snapshot": snapshot}
        )
        return lambda: client.statistics(lake, request)

    return provoke


def _changes_at(which: str) -> Provoke:
    def provoke(client: KitClient) -> Callable[[], object]:
        if which == "expired":
            lake, since = _expired(client)
            snapshot = _snapshot(client, lake)
        else:
            lake = _lake(client)
            snapshot = _snapshot(client, lake)
            since = {"unknown": 999_999, "backwards": snapshot}[which]
            snapshot = {"unknown": snapshot, "backwards": snapshot - 1}[which]
        request = ChangesRequest.model_validate(
            {"kind": "changes", "tables": ["main.t"], "since": since, "snapshot": snapshot}
        )
        return lambda: client.changes(lake, request)

    return provoke


def _columns_at(which: str) -> Provoke:
    def provoke(client: KitClient) -> Callable[[], object]:
        if which == "expired":
            lake, snapshot = _expired(client)
        else:
            lake = _lake(client)
            snapshot = 999_999
        request = TablesRequest(kind="tables", snapshot=snapshot)
        return lambda: client.table_columns(lake, request)

    return provoke


def _busy(operation: str) -> Provoke:
    def provoke(client: KitClient) -> Callable[[], object]:
        lake = _full(client)
        snapshot = _snapshot(client, lake)
        arguments: dict[str, tuple[object, ...]] = {
            "statistics": (
                StatisticsRequest.model_validate(
                    {"kind": "statistics", "tables": ["main.t"], "snapshot": snapshot}
                ),
            ),
            "changes": (
                ChangesRequest.model_validate(
                    {
                        "kind": "changes",
                        "tables": ["main.t"],
                        "since": snapshot,
                        "snapshot": snapshot,
                    }
                ),
            ),
            "table_columns": (TablesRequest(kind="tables", snapshot=snapshot),),
            "start_read": (read_request("SELECT * FROM t", snapshot),),
            "begin": (),
            "maintain": (_now(), _now()),
        }
        envelope = client.envelope(operation)
        return lambda: client.send(operation, envelope, lake, *arguments[operation])

    return provoke


# --- read ----------------------------------------------------------------------------------------


def _read_of(sql: str, snapshot: str = "current") -> Provoke:
    def provoke(client: KitClient) -> Callable[[], object]:
        if snapshot == "expired":
            lake, at = _expired(client)
        else:
            lake = _lake(client)
            at = 999_999 if snapshot == "unknown" else _snapshot(client, lake)
        return lambda: client.start_read(lake, read_request(sql, at))

    return provoke


def _batch_of(sql: str, *, timeout_ms: int | None = None, cancel: bool = False) -> Provoke:
    def provoke(client: KitClient) -> Callable[[], object]:
        lake = _lake(client)
        request = read_request(sql, _snapshot(client, lake))
        read = client.start_read(lake, request.model_copy(update={"timeout_ms": timeout_ms}))
        if cancel:
            client.cancel_read(read)
        return lambda: client.read_batch(read)

    return provoke


# --- write ---------------------------------------------------------------------------------------


def _nested(client: KitClient) -> Callable[[], object]:
    lake = _lake(client)
    client.begin(lake)
    return lambda: client.begin(lake)


def _writer_ahead(client: KitClient) -> Callable[[], object]:
    lake = client.open_lake(local_location(), Limits("256MB", 1, 2, 2, 300), ())
    client.begin(lake)

    def from_another_thread() -> object:
        outcome: list[object] = []
        thread = threading.Thread(target=lambda: outcome.append(_caught(client, lake)))
        thread.start()
        thread.join()
        if isinstance(outcome[0], BaseException):
            raise outcome[0]
        return outcome[0]

    return from_another_thread


def _caught(client: KitClient, lake: LakeHandle) -> object:
    try:
        return client.begin(lake)
    except BaseException as error:  # noqa: BLE001 - handed back to the case's own thread
        return error


def _in_transaction(operation: str, *arguments: object, state: str = "open") -> Provoke:
    def provoke(client: KitClient) -> Callable[[], object]:
        transaction = _cancelled(client) if state == "cancelled" else _transaction(client)
        envelope = client.envelope(operation)
        return lambda: client.send(operation, envelope, transaction, *arguments)

    return provoke


def _failed_then_commit(client: KitClient) -> Callable[[], object]:
    transaction = _transaction(client)
    refused(
        "STORE_QUERY_FAILED",
        lambda: client.execute(transaction, statement("INSERT INTO t VALUES ('x')")),
    )
    return lambda: client.commit(transaction)


def _timed_out_then_commit(client: KitClient) -> Callable[[], object]:
    transaction = _transaction(client)
    refused("STORE_TIMEOUT", lambda: client.execute(transaction, _timed(f"INSERT INTO t {HEAVY}")))
    return lambda: client.commit(transaction)


def _cancelled_commit(client: KitClient) -> Callable[[], object]:
    transaction = _cancelled(client)
    return lambda: client.commit(transaction)


def _conflict(client: KitClient) -> Callable[[], object]:
    """Two writers of one SQLite catalog change the same table."""
    root = folder()
    location = SqliteLakeLocation(
        kind="lake_location",
        catalog="sqlite",
        path=str(root / "catalog.sqlite"),
        data_path=(root / "data").as_posix() + "/",
    )
    first = client.open_lake(location, LIMITS, ())
    commit(client, first, "CREATE TABLE t (n INTEGER)")
    second = client.open_lake(location, LIMITS, ())
    mine = client.begin(first)
    theirs = client.begin(second)
    client.execute(mine, statement("INSERT INTO t VALUES (1)"))
    client.execute(theirs, statement("DROP TABLE t"))
    return lambda: client.commit(mine)


def _load(table: pa.Table, columns: tuple[tuple[str, str], ...], timeout_ms: int | None) -> Provoke:
    return _in_transaction("load_arrow", "main", "loaded", table, columns, timeout_ms)


# --- existing lake -------------------------------------------------------------------------------


def _attach(change: Callable[[KitClient], object]) -> Provoke:
    def provoke(client: KitClient) -> Callable[[], object]:
        made = change(client)
        attachment, limits = made if isinstance(made, tuple) else (made, EXISTING_LIMITS)
        return lambda: client.open_existing_lake(attachment, limits)

    return provoke


def _attachment(client: KitClient) -> object:
    lake, attachment = existing_lake(client)
    client.close_existing_lake(lake)
    return attachment


def _encrypted(client: KitClient) -> object:
    attachment = _attachment(client)
    location = attachment.location.model_copy(update={"encrypted": True})  # type: ignore[attr-defined]
    return attachment.model_copy(update={"location": location})  # type: ignore[attr-defined]


def _unprotected(client: KitClient) -> object:
    return _attachment(client).model_copy(update={"protected_locations": None})  # type: ignore[attr-defined]


def _no_threads(client: KitClient) -> object:
    return _attachment(client), ExistingLakeLimits(memory_limit="256MB", threads=0, timeout_ms=1)


def _untested_catalog(client: KitClient) -> object:
    attachment = _attachment(client)
    connection = sqlite3.connect(attachment.location.path)  # type: ignore[attr-defined]
    try:
        connection.execute("UPDATE ducklake_metadata SET value = '9.9' WHERE key = 'version'")
        connection.commit()
    finally:
        connection.close()
    return attachment


def _on_postgres(**changes: str) -> Callable[[KitClient], object]:
    def change(client: KitClient) -> object:
        return _attachment(client).model_copy(update={"location": _postgres(**changes)})  # type: ignore[attr-defined]

    return change


def _existing(operation: str, *, snapshot: str = "latest", sql: str | None = None) -> Provoke:
    def provoke(client: KitClient) -> Callable[[], object]:
        lake, attachment = existing_lake(client)
        latest = client.existing_latest(lake)
        moved = latest.model_copy(
            update={"committed_at": latest.committed_at - dt.timedelta(days=1)}
        )
        at = moved if snapshot == "moved" else latest
        arguments: dict[str, tuple[object, ...]] = {
            "existing_verify": (at, attachment.model_copy(update={"name": "other"})),
            "existing_table_columns": (at,),
            "start_existing_read": (
                read_request(sql or "SELECT amount FROM readings", at.id),
                at.committed_at,
            ),
        }
        envelope = client.envelope(operation)
        return lambda: client.send(operation, envelope, lake, *arguments[operation])

    return provoke


# --- clickhouse ----------------------------------------------------------------------------------


def _connect(target: Callable[[], ClickHouseTarget]) -> Provoke:
    return lambda client: lambda: client.connect_clickhouse(target(), BUDGET)


def _plain_remote() -> ClickHouseTarget:
    return ClickHouseTarget("clickhouse.example", 8123, "db", "kit", UNSET, tls=False)


def _secret_unset() -> ClickHouseTarget:
    return ClickHouseTarget("localhost", 8123, "db", "kit", UNSET, tls=False)


def _nothing_listens() -> ClickHouseTarget:
    os.environ[SET] = "kit-not-a-password"
    return ClickHouseTarget("127.0.0.1", 1, "db", "kit", SET, tls=False)


def _administrator() -> ClickHouseTarget:
    server = clickhouse()
    return server.target(server.user, CLICKHOUSE_PASSWORD)


def _unsafe_reader() -> ClickHouseTarget:
    server = clickhouse()
    return server.reader(server.database(), {**SAFE, "join_use_nulls": "0"})


def _engine(operation: str, *arguments: object) -> Provoke:
    def provoke(client: KitClient) -> Callable[[], object]:
        server = clickhouse()
        database = server.database(
            "readings (id UInt32, amount Decimal(18, 2)) ENGINE = MergeTree ORDER BY id",
            "points (id UInt32, at Array(String)) ENGINE = MergeTree ORDER BY id",
        )
        engine = client.connect_clickhouse(server.reader(database), BUDGET)
        envelope = client.envelope(operation)
        return lambda: client.send(operation, envelope, engine, *arguments)

    return provoke


# --- encoding and separation ---------------------------------------------------------------------


def _untyped(operation: str) -> Provoke:
    table = pa.table({"x": pa.array([1], pa.int64())})
    arguments = {"json_frame": (table.schema.empty_table(),), "json_rows": (table, True)}
    return lambda client: functools.partial(
        client.send, operation, client.envelope(operation), *arguments[operation]
    )


def _separation(operation: str) -> Provoke:
    def provoke(client: KitClient) -> Callable[[], object]:
        root = str(local_location().path)
        arguments: dict[str, tuple[object, ...]] = {
            "location_components": ("c", "relative/path"),
            "locations_overlap": ("c", "s3://bucket/../key", "s3://bucket/key"),
            "check_apart": ("c", root + "/data", None, (root,), ()),
        }
        envelope = client.envelope(operation)
        return lambda: client.send(operation, envelope, *arguments[operation])

    return provoke


PARAMETER = "STORE_PARAMETER_INVALID"
SNAPSHOT_GONE = "STORE_SNAPSHOT_NOT_FOUND"
EXPIRED = "STORE_SNAPSHOT_EXPIRED"
STATEMENT = "STORE_STATEMENT_INVALID"
FAILED = "STORE_QUERY_FAILED"
SOURCE = "STORE_SOURCE_INVALID"
CANCELLED = "STORE_CANCELLED"
TIMEOUT = "STORE_TIMEOUT"
BUSY = "STORE_BUSY"
ONE = pa.table({"n": pa.array([1], pa.int64())})
CAST = pa.table({"n": pa.array(["x"], pa.string())})
MANY = pa.table({"n": pa.array(range(3_000_000), pa.int64())})

PROOFS: dict[tuple[str, str], Provoke] = {
    # lake
    ("open_lake", "STORE_LIMITS_REQUIRED"): lambda c: (
        lambda: c.open_lake(local_location(), Limits(None, None), ())
    ),
    ("open_lake", "STORE_LOCATION_INVALID"): _remote_data_without_storage,
    ("open_lake", "STORE_SECRET_MISSING"): lambda c: (
        lambda: c.open_lake(_postgres(password_env=UNSET), LIMITS, ())
    ),
    ("open_lake", "STORE_SECRET_INLINE"): lambda c: (
        lambda: c.open_lake(
            _postgres(connection="host=127.0.0.1 port=1 dbname=kit user=kit password=p"), LIMITS, ()
        )
    ),
    ("open_lake", "STORE_TLS_REQUIRED"): lambda c: (
        lambda: c.open_lake(
            _postgres(connection="host=db.example.com port=5432 dbname=kit user=kit"), LIMITS, ()
        )
    ),
    ("open_lake", "STORE_CATALOG_UNAVAILABLE"): lambda c: (
        lambda: c.open_lake(_postgres(), LIMITS, ())
    ),
    ("open_lake", "SOURCE_NOT_FOUND"): _missing_source,
    ("open_lake", SOURCE): _source_conflict,
    ("close_lake", PARAMETER): _forged("lake", "close_lake"),
    ("current_snapshot", PARAMETER): _forged("lake", "current_snapshot"),
    ("snapshot", PARAMETER): _forged("lake", "snapshot", 1),
    ("tables", PARAMETER): _forged("lake", "tables"),
    ("describe", PARAMETER): _describe_parameter,
    ("describe", STATEMENT): _describe_refusal,
    ("describe", FAILED): _describe_failure,
    ("describe_source", PARAMETER): _forged("lake", "describe_source", "field", "SELECT 1", ()),
    ("describe_source", STATEMENT): _source_refusal("DELETE FROM meters"),
    ("describe_source", FAILED): _source_refusal("SELECT nothing_here FROM meters"),
    ("describe_source", SOURCE): _source_refusal("SELECT 1", name="nope"),
    ("statistics", PARAMETER): _forged(
        "lake",
        "statistics",
        StatisticsRequest.model_validate(
            {"kind": "statistics", "tables": ["main.t"], "snapshot": 1}
        ),
    ),
    ("statistics", SNAPSHOT_GONE): _statistics_at("main.t", "unknown"),
    ("statistics", EXPIRED): _statistics_at("main.t", "expired"),
    ("statistics", BUSY): _busy("statistics"),
    ("statistics", STATEMENT): _statistics_at("main.nope", "current"),
    ("changes", PARAMETER): _forged(
        "lake",
        "changes",
        ChangesRequest.model_validate(
            {"kind": "changes", "tables": ["main.t"], "since": 1, "snapshot": 1}
        ),
    ),
    ("changes", SNAPSHOT_GONE): _changes_at("unknown"),
    ("changes", EXPIRED): _changes_at("expired"),
    ("changes", BUSY): _busy("changes"),
    ("changes", STATEMENT): _changes_at("backwards"),
    ("table_columns", PARAMETER): _forged(
        "lake", "table_columns", TablesRequest(kind="tables", snapshot=1)
    ),
    ("table_columns", SNAPSHOT_GONE): _columns_at("unknown"),
    ("table_columns", EXPIRED): _columns_at("expired"),
    ("table_columns", BUSY): _busy("table_columns"),
    # read
    ("start_read", PARAMETER): _forged("lake", "start_read", read_request("SELECT 1", 1)),
    ("start_read", SNAPSHOT_GONE): _read_of("SELECT * FROM t", "unknown"),
    ("start_read", EXPIRED): _read_of("SELECT * FROM t", "expired"),
    ("start_read", BUSY): _busy("start_read"),
    ("start_read", STATEMENT): _read_of("SELECT * FROM read_csv('/etc/passwd')"),
    ("read_batch", PARAMETER): _forged("read", "read_batch"),
    ("read_batch", FAILED): _batch_of(
        "SELECT CAST(v AS INTEGER) AS n FROM (VALUES ('1'), ('x')) AS s(v)"
    ),
    ("read_batch", TIMEOUT): _batch_of(HEAVY, timeout_ms=50),
    ("read_batch", CANCELLED): _batch_of(HEAVY, cancel=True),
    ("cancel_read", PARAMETER): _forged("read", "cancel_read"),
    ("close_read", PARAMETER): _forged("read", "close_read"),
    # write
    ("begin", PARAMETER): _forged("lake", "begin"),
    ("begin", BUSY): _busy("begin"),
    ("begin", STATEMENT): _nested,
    ("begin", TIMEOUT): _writer_ahead,
    ("execute", PARAMETER): _in_transaction(
        "execute",
        StatementRequest(
            kind="statement",
            sql=_bad_parameter("INSERT INTO t VALUES ($p)")[0],
            parameters=list(_bad_parameter()[1]),
        ),
    ),
    ("execute", STATEMENT): _in_transaction("execute", statement("ATTACH '/tmp/kit.db' AS other")),
    ("execute", FAILED): _in_transaction("execute", statement("INSERT INTO t VALUES ('x')")),
    ("execute", CANCELLED): _in_transaction(
        "execute", statement("INSERT INTO t VALUES (3)"), state="cancelled"
    ),
    ("execute", TIMEOUT): _in_transaction("execute", _timed(f"INSERT INTO t {HEAVY}")),
    ("value", PARAMETER): _forged("transaction", "value", statement("SELECT 1")),
    ("value", STATEMENT): _in_transaction("value", statement("DELETE FROM t")),
    ("value", FAILED): _in_transaction("value", statement("SELECT 'x'::INTEGER")),
    ("value", CANCELLED): _in_transaction("value", statement("SELECT 1"), state="cancelled"),
    ("value", TIMEOUT): _in_transaction("value", _timed(HEAVY)),
    ("land", PARAMETER): _forged("transaction", "land", "field", "SELECT 1", (), "main", "l", None),
    ("land", SOURCE): _in_transaction("land", "nope", "SELECT 1", (), "main", "l", None),
    ("land", FAILED): _in_transaction(
        "land", "field", "SELECT * FROM nothing_here", (), "main", "l", None
    ),
    ("land", CANCELLED): _in_transaction(
        "land", "field", "SELECT * FROM meters", (), "main", "l", None, state="cancelled"
    ),
    ("land", TIMEOUT): _in_transaction(
        "land",
        "field",
        "WITH RECURSIVE r(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM r WHERE i < 100000000) "
        "SELECT count(*) AS n FROM r",
        (),
        "main",
        "l",
        50,
    ),
    ("load_arrow", PARAMETER): _forged(
        "transaction", "load_arrow", "main", "loaded", ONE, (("n", "BIGINT"),), None
    ),
    ("load_arrow", FAILED): _load(CAST, (("n", "INTEGER"),), None),
    ("load_arrow", CANCELLED): _in_transaction(
        "load_arrow", "main", "loaded", ONE, (("n", "BIGINT"),), None, state="cancelled"
    ),
    ("load_arrow", TIMEOUT): _load(MANY, (("n", "BIGINT"),), 1),
    ("cancel_transaction", PARAMETER): _forged("transaction", "cancel_transaction"),
    ("commit", PARAMETER): _forged("transaction", "commit"),
    ("commit", CANCELLED): _cancelled_commit,
    ("commit", FAILED): _failed_then_commit,
    ("commit", TIMEOUT): _timed_out_then_commit,
    ("commit", "STORE_TRANSACTION_CONFLICT"): _conflict,
    ("rollback", PARAMETER): _forged("transaction", "rollback"),
    # maintenance
    ("maintain", PARAMETER): _forged("lake", "maintain", _now(), _now()),
    ("maintain", BUSY): _busy("maintain"),
    # existing lake
    ("open_existing_lake", "STORE_LIMITS_REQUIRED"): _attach(_no_threads),
    ("open_existing_lake", "STORE_LOCATION_INVALID"): _attach(_encrypted),
    ("open_existing_lake", "STORE_SECRET_MISSING"): _attach(_on_postgres(password_env=UNSET)),
    ("open_existing_lake", "STORE_TLS_REQUIRED"): _attach(
        _on_postgres(connection="host=db.example.com port=5432 dbname=kit user=kit")
    ),
    ("open_existing_lake", "STORE_CONNECTION_FAILED"): _attach(_on_postgres()),
    ("open_existing_lake", SOURCE): _attach(_unprotected),
    ("open_existing_lake", "ENGINE_VERSION_UNTESTED"): _attach(_untested_catalog),
    ("close_existing_lake", PARAMETER): _forged("existing", "close_existing_lake"),
    ("existing_latest", PARAMETER): _forged("existing", "existing_latest"),
    ("existing_verify", PARAMETER): _forged("existing", "existing_verify", None, None),
    ("existing_verify", SOURCE): _existing("existing_verify"),
    ("existing_committed_at", PARAMETER): _forged("existing", "existing_committed_at", 1),
    ("existing_table_columns", PARAMETER): _forged("existing", "existing_table_columns", None),
    ("existing_table_columns", SOURCE): _existing("existing_table_columns", snapshot="moved"),
    ("existing_views", PARAMETER): _forged("existing", "existing_views", None),
    ("start_existing_read", PARAMETER): _forged(
        "existing", "start_existing_read", read_request("SELECT 1", 1), _now()
    ),
    ("start_existing_read", STATEMENT): _existing(
        "start_existing_read", sql="SELECT * FROM read_csv('/etc/passwd')"
    ),
    ("start_existing_read", FAILED): _existing(
        "start_existing_read", sql="SELECT nothing_here FROM readings"
    ),
    ("start_existing_read", SOURCE): _existing("start_existing_read", snapshot="moved"),
    # clickhouse
    ("connect_clickhouse", "STORE_TLS_REQUIRED"): _connect(_plain_remote),
    ("connect_clickhouse", "STORE_SECRET_MISSING"): _connect(_secret_unset),
    ("connect_clickhouse", "STORE_CONNECTION_FAILED"): _connect(_nothing_listens),
    ("connect_clickhouse", "SOURCE_NOT_READ_ONLY"): _connect(_administrator),
    ("connect_clickhouse", "ENGINE_SETTING_REQUIRED"): _connect(_unsafe_reader),
    ("clickhouse_columns", PARAMETER): _forged("clickhouse", "clickhouse_columns", "t"),
    ("clickhouse_columns", "SOURCE_NOT_FOUND"): _engine("clickhouse_columns", "nothing_here"),
    ("clickhouse_columns", "UNSUPPORTED_SOURCE_TYPE"): _engine("clickhouse_columns", "points"),
    ("clickhouse_catalog", PARAMETER): _forged("clickhouse", "clickhouse_catalog"),
    ("clickhouse_estimate", PARAMETER): _forged(
        "clickhouse", "clickhouse_estimate", "SELECT 1", ()
    ),
    ("clickhouse_estimate", STATEMENT): _engine("clickhouse_estimate", "SELECT 1 FORMAT JSON", ()),
    ("clickhouse_now", PARAMETER): _forged("clickhouse", "clickhouse_now"),
    ("start_clickhouse_read", PARAMETER): _forged(
        "clickhouse", "start_clickhouse_read", "SELECT 1", (), (("a", "INTEGER"),), None
    ),
    ("start_clickhouse_read", STATEMENT): _engine(
        "start_clickhouse_read", "SELECT 1 SETTINGS readonly = 0", (), (("a", "INTEGER"),), None
    ),
    ("start_clickhouse_read", FAILED): _engine(
        "start_clickhouse_read", "SELECT nothing_here FROM readings", (), (("a", "INTEGER"),), None
    ),
    # encoding and separation
    ("json_frame", "STORE_TYPE_UNSUPPORTED"): _untyped("json_frame"),
    ("json_rows", "STORE_TYPE_UNSUPPORTED"): _untyped("json_rows"),
    ("location_components", SOURCE): _separation("location_components"),
    ("locations_overlap", SOURCE): _separation("locations_overlap"),
    ("check_apart", SOURCE): _separation("check_apart"),
}


def _identifier(operation: str, code: str) -> str:
    return f"{operation.replace('_', '-')}.raises.{code.lower().replace('_', '-')}"


def _case(operation: str, code: str, provoke: Provoke) -> KitCase:
    def check(client: KitClient) -> None:
        refused(code, provoke(client))

    return KitCase(
        _identifier(operation, code), operation, f"{operation} refuses with {code}", check
    )


type Isolated = dict[tuple[str, str], tuple[Callable[[KitClient], None], CaseEnvironment]]


def _isolated(operation: str, code: str, isolated: Isolated) -> KitCase:
    check, environment = isolated[operation, code]
    expectation = f"{operation} refuses with {code}, in the environment the case declares"
    return KitCase(
        _identifier(operation, code), operation, expectation, check, environment=environment
    )


def code_cases(contract: InterfaceContract, isolated: Isolated) -> tuple[KitCase, ...]:
    """One case per declared (operation, code), those of `isolated` each in the environment it
    declares (their checks live beside the kit, where a case's own process finds it);
    KIT_CODE_UNPROVEN when the declared and the proven codes differ."""
    declared = {(o.name, code) for o in contract.operations for code in o.errors}
    proven = set(PROOFS) | set(isolated)
    unproven = sorted(declared - proven)
    undeclared = sorted(proven - declared)
    if unproven or undeclared:
        raise InterfaceContractError(
            "KIT_CODE_UNPROVEN",
            f"kit {contract.name}: declared codes without a case {unproven}; "
            f"cases of undeclared codes {undeclared}",
            {"contract": contract.name},
        )
    return tuple(
        _isolated(operation, code, isolated)
        if (operation, code) in isolated
        else _case(operation, code, PROOFS[operation, code])
        for operation, code in sorted(proven)
    )
