"""The store's conformance kit (#486 G1): every operation of `orvex.contracts.store.interface`,
its exactness, its fail-closed refusals and the security cases consumers rely on (the SQL guard,
the sandbox, forged handles, TLS and secrets), and one idempotency case per operation that
changes state, and one case per error code each operation declares (`kit_codes`). No skips; its
case count is recorded.

Most cases run on local folders and SQLite files; the ClickHouse cases take a server from the
environment (`kit_services`) and fail, never skip, without one.
"""

from __future__ import annotations

import datetime as dt
import functools
from collections.abc import Callable
from decimal import Decimal

import pyarrow as pa
from orvex.contracts.envelope import CallEnvelope
from orvex.contracts.kit import Idempotent, Kit, KitCase
from orvex.contracts.kit_client import KitClient
from orvex.contracts.store.interface import STORE
from orvex.contracts.store.kit_codes import code_cases
from orvex.contracts.store.kit_support import (
    EXISTING_LIMITS,
    LIMITS,
    Scenario,
    check,
    commit,
    drain,
    existing_lake,
    local_location,
    open_lake,
    read_request,
    refused,
    rows,
    sqlite_source,
    statement,
    write,
)
from orvex.contracts.store.v1 import (
    ChangesRequest,
    Parameter,
    StatisticsRequest,
    TablesRequest,
)
from orvex.contracts.store.values import (
    Budget,
    ClickHouseHandle,
    ExistingLakeHandle,
    LakeHandle,
    ReadHandle,
    TransactionHandle,
)

EXACT = (
    "SELECT 12345678901234567890.1234567890::DECIMAL(38,10) AS amount, "
    "170141183460469231731687303715884105727::HUGEINT AS huge, "
    "TIMESTAMPTZ '2026-03-29 01:30:00+00' AS at, NULL::BIGINT AS missing"
)
FORGED_LAKE = LakeHandle("lake-forged")
FORGED_READ = ReadHandle("read-forged", pa.table({}))
FORGED_TRANSACTION = TransactionHandle("transaction-forged", None)
FORGED_EXISTING = ExistingLakeHandle("existing-lake-forged", "forged")
FORGED_ENGINE = ClickHouseHandle("clickhouse-forged", "db", "")
BUDGET = Budget(timeout_ms=1_000, memory_bytes=1 << 30)
PARAMETER = "STORE_PARAMETER_INVALID"
STATEMENT = "STORE_STATEMENT_INVALID"
SOURCE = "STORE_SOURCE_INVALID"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# --- lake ----------------------------------------------------------------------------------------

_opening = Scenario(lambda client: local_location())


def _open_call(client: KitClient, envelope: CallEnvelope) -> object:
    return client.send("open_lake", envelope, _opening.of(client), LIMITS, ())


def _lakes(client: KitClient) -> int:
    return len(client.held().lakes)


_closing = Scenario(open_lake)


def _close_call(client: KitClient, envelope: CallEnvelope) -> object:
    return client.send("close_lake", envelope, _closing.of(client))


def _held_lists(client: KitClient) -> None:
    lake = open_lake(client)
    check(lake.id in client.held().lakes, "an open lake is held")
    client.close_lake(lake)
    check(lake.id not in client.held().lakes, "a closed lake is let go")
    refused(PARAMETER, lambda: client.current_snapshot(lake))


def _current_after_commit(client: KitClient) -> None:
    lake = open_lake(client)
    committed = write(client, lake, "CREATE TABLE t AS SELECT 1 AS a")
    check(client.current_snapshot(lake) == committed.snapshot, "the commit's snapshot is current")


def _snapshot_unknown(client: KitClient) -> None:
    lake = open_lake(client)
    first = commit(client, lake, "CREATE TABLE t AS SELECT 1 AS a")
    check(client.snapshot(lake, first.id) == first, "a kept snapshot is found")
    check(client.snapshot(lake, 999_999) is None, "an unknown snapshot is None")
    check(client.snapshot(lake, -1) is None, "a negative id is None")


def _tables(client: KitClient) -> None:
    lake = open_lake(client)
    write(client, lake, 'CREATE TABLE "Readings" (a INTEGER)', "CREATE TABLE other (b INTEGER)")
    found = client.tables(lake)
    check(found == (("main", "other"), ("main", "readings")), f"tables, lower-cased: {found}")


def _describe(client: KitClient) -> None:
    lake = open_lake(client)
    found = client.describe(lake, "SELECT 1.50::DECIMAL(18,2) AS a, NULL::VARCHAR AS b", ())
    check(found == (("a", "DECIMAL(18,2)"), ("b", "VARCHAR")), f"described: {found}")


def _describe_source(client: KitClient) -> None:
    lake = open_lake(client, (sqlite_source(),))
    found = client.describe_source(lake, "field", "SELECT * FROM meters", ())
    check(found == (("id", "BIGINT"), ("name", "VARCHAR")), f"source columns: {found}")


def _statistics(client: KitClient) -> None:
    lake = open_lake(client)
    snapshot = commit(client, lake, "CREATE TABLE t AS SELECT * FROM range(3) AS r(a)")
    request = StatisticsRequest.model_validate(
        {"kind": "statistics", "tables": ["main.t"], "snapshot": snapshot.id}
    )
    (table,) = client.statistics(lake, request).tables
    counted = table.inlined_rows + sum(file.row_count for file in table.files)
    check(counted == 3, f"three rows are counted: {table}")


def _changes(client: KitClient) -> None:
    lake = open_lake(client)
    since = commit(client, lake, "CREATE TABLE t AS SELECT * FROM range(3) AS r(a)")
    after = commit(client, lake, "DELETE FROM t WHERE a = 0")
    request = ChangesRequest.model_validate(
        {"kind": "changes", "tables": ["main.t"], "since": since.id, "snapshot": after.id}
    )
    (table,) = client.changes(lake, request).tables
    check(table.changed and table.removed_rows == 1, f"one row removed: {table}")


def _table_columns(client: KitClient) -> None:
    lake = open_lake(client)
    first = commit(client, lake, "CREATE TABLE t (a DECIMAL(18,2) NOT NULL)")
    write(client, lake, "ALTER TABLE t ADD COLUMN b VARCHAR")
    (table,) = client.table_columns(lake, TablesRequest(kind="tables", snapshot=first.id)).tables
    names = [(c.name, c.type) for c in table.columns]
    check(names == [("a", "DECIMAL(18,2)")], f"the columns as of the snapshot: {names}")


# --- read ----------------------------------------------------------------------------------------


def _exact(client: KitClient) -> None:
    lake = open_lake(client)
    write(client, lake, f"CREATE TABLE t AS {EXACT}")
    (row,) = rows(client, lake, "SELECT * FROM t")
    check(row["amount"] == Decimal("12345678901234567890.1234567890"), f"DECIMAL: {row}")
    check(str(row["huge"]) == "170141183460469231731687303715884105727", f"HUGEINT: {row}")
    check(row["at"] == dt.datetime(2026, 3, 29, 1, 30, tzinfo=dt.UTC), f"TIMESTAMPTZ: {row}")
    check(row["missing"] is None, f"NULL is never zero: {row}")


def _read_parameters(client: KitClient) -> None:
    lake = open_lake(client)
    snapshot = commit(client, lake, "CREATE TABLE t AS SELECT * FROM range(5) AS r(a)").id
    request = read_request("SELECT count(*) AS n FROM t WHERE a >= $low", snapshot)
    bound = request.model_copy(
        update={
            "parameters": [
                Parameter.model_validate({"name": "low", "type": "BIGINT", "value": "3"})
            ]
        }
    )
    found = drain(client, client.start_read(lake, bound)).to_pylist()
    check(found == [{"n": 2}], f"the parameter is bound: {found}")


def _read_ends(client: KitClient) -> None:
    lake = open_lake(client)
    snapshot = commit(client, lake, "CREATE TABLE t AS SELECT * FROM range(3) AS r(a)").id
    read = client.start_read(lake, read_request("SELECT a FROM t ORDER BY a", snapshot))
    seen = []
    while (data := client.read_batch(read)) is not None:
        seen += data.column("a").to_pylist()
    check(seen == [0, 1, 2], f"every row, once: {seen}")
    check(client.read_batch(read) is None, "an ended read stays ended")
    client.close_read(read)


_cancelling = Scenario(
    lambda client: client.start_read(
        (lake := open_lake(client)),
        read_request("SELECT * FROM range(10) AS r(a)", client.current_snapshot(lake).id),
    )
)


def _cancelled(client: KitClient) -> int:
    return len(client.held().cancelled)


_closing_read = Scenario(
    lambda client: client.start_read(
        (lake := open_lake(client)),
        read_request("SELECT 1 AS a", client.current_snapshot(lake).id),
    )
)


def _reads(client: KitClient) -> int:
    return len(client.held().reads)


# --- write ---------------------------------------------------------------------------------------

_beginning = Scenario(open_lake)


def _transactions(client: KitClient) -> int:
    return len(client.held().transactions)


def _in_transaction(client: KitClient) -> TransactionHandle:
    lake = open_lake(client, (sqlite_source(),))
    write(client, lake, "CREATE TABLE t (a INTEGER)")
    found: TransactionHandle = client.begin(lake)
    return found


_executing = Scenario(_in_transaction)
_landing = Scenario(_in_transaction)
_loading = Scenario(_in_transaction)
_cancelling_write = Scenario(_in_transaction)


def _count(client: KitClient, transaction: TransactionHandle, table: str) -> str | None:
    """How many rows `table` has inside the transaction, or `absent`."""
    exists = statement(
        f"SELECT count(*) FROM information_schema.tables WHERE table_name = '{table}'"
    )
    if client.value(transaction, exists) == "0":
        return "absent"
    found: str | None = client.value(transaction, statement(f"SELECT count(*) FROM {table}"))
    return found


def _value(client: KitClient) -> None:
    lake = open_lake(client)
    write(client, lake, "CREATE TABLE t AS SELECT 1.50::DECIMAL(18,2) AS a")
    transaction = client.begin(lake)
    check(client.value(transaction, statement("SELECT a FROM t")) == "1.50", "the first cell")
    check(client.value(transaction, statement("SELECT a FROM t WHERE false")) is None, "no row")
    client.rollback(transaction)


def _load_exact(client: KitClient) -> None:
    lake = open_lake(client)
    batch = pa.record_batch({"amount": pa.array([Decimal("1.25"), None], pa.decimal128(18, 2))})
    transaction = client.begin(lake)
    loaded = client.load_arrow(
        transaction,
        "main",
        "loaded",
        pa.Table.from_batches([batch]),
        (("amount", "DECIMAL(18,2)"),),
        None,
    )
    client.commit(transaction)
    check(loaded == 2, f"two rows loaded: {loaded}")
    found = rows(client, lake, "SELECT amount FROM loaded")
    check(found == [{"amount": Decimal("1.25")}, {"amount": None}], f"loaded exactly: {found}")


def _cancel_rolls_back(client: KitClient) -> None:
    lake = open_lake(client)
    transaction = client.begin(lake)
    client.execute(transaction, statement("CREATE TABLE t (a INTEGER)"))
    client.cancel_transaction(transaction)
    refused("STORE_CANCELLED", lambda: client.commit(transaction))
    check(client.tables(lake) == (), "a cancelled transaction writes nothing")


def _uncommitted(client: KitClient) -> tuple[LakeHandle, TransactionHandle]:
    lake = open_lake(client)
    transaction = client.begin(lake)
    client.execute(transaction, statement("CREATE TABLE t (a INTEGER)"))
    return lake, transaction


_committing = Scenario(_uncommitted)


def _committed(client: KitClient) -> int:
    lake, _ = _committing.of(client)
    current: int = client.current_snapshot(lake).id
    return current


_rolling_back = Scenario(lambda client: client.begin(open_lake(client)))


def _rollback_discards(client: KitClient) -> None:
    lake = open_lake(client)
    transaction = client.begin(lake)
    client.execute(transaction, statement("CREATE TABLE t (a INTEGER)"))
    client.rollback(transaction)
    check(client.tables(lake) == (), "a rolled-back transaction writes nothing")
    refused(PARAMETER, lambda: client.execute(transaction, statement("SELECT 1")))


# --- maintenance ---------------------------------------------------------------------------------


def _maintained_lake(client: KitClient) -> tuple[LakeHandle, int]:
    lake = open_lake(client)
    first = commit(client, lake, "CREATE TABLE t AS SELECT 1 AS a").id
    write(client, lake, "INSERT INTO t VALUES (2)")
    return lake, first


_maintaining = Scenario(_maintained_lake)


def _expired(client: KitClient) -> bool:
    lake, first = _maintaining.of(client)
    return client.snapshot(lake, first) is None


# --- existing lake -------------------------------------------------------------------------------

_attaching = Scenario(lambda client: existing_lake(client)[1])


def _existing(client: KitClient) -> int:
    return len(client.held().existing_lakes)


_detaching = Scenario(lambda client: existing_lake(client)[0])


def _existing_reads(client: KitClient) -> None:
    lake, attachment = existing_lake(client)
    latest = client.existing_latest(lake)
    check(client.existing_verify(lake, latest, attachment) == latest, "the latest is verified")
    check(client.existing_committed_at(lake, latest.id) == latest.committed_at, "its time")
    check(client.existing_committed_at(lake, 999_999) is None, "an unknown snapshot has none")
    (table,) = client.existing_table_columns(lake, latest).tables
    check(table.table == "readings", f"its table: {table.table}")
    check(client.existing_views(lake, latest) == (), "no view")
    read = client.start_existing_read(
        lake, read_request("SELECT amount FROM readings", latest.id), latest.committed_at
    )
    found = drain(client, read).to_pylist()
    check(found == [{"amount": Decimal("12.50")}], f"read exactly: {found}")
    client.close_existing_lake(lake)


# --- clickhouse ----------------------------------------------------------------------------------


def _clickhouse_close(client: KitClient) -> None:
    client.close_clickhouse(FORGED_ENGINE)
    client.close_clickhouse(FORGED_ENGINE)
    check(client.held().clickhouse == (), "closing what is not held changes nothing")


# --- encoding ------------------------------------------------------------------------------------


def _read_schema(client: KitClient) -> tuple[pa.Table, pa.RecordBatch]:
    lake = open_lake(client)
    write(client, lake, "CREATE TABLE t AS SELECT 1.50::DECIMAL(18,2) AS a, NULL::HUGEINT AS h")
    read = client.start_read(
        lake, read_request("SELECT * FROM t", client.current_snapshot(lake).id)
    )
    table = drain(client, read)
    return read.schema, table.to_batches()[0]


def _json_frame(client: KitClient) -> None:
    schema, _ = _read_schema(client)
    frame = client.json_frame(schema)
    check(frame.tail == "]}", f"the tail closes the rows and the document: {frame.tail}")
    found = "".join(frame.head)
    expected = (
        '{"kind":"result_json","columns":[{"name": "a", "type": "DECIMAL(18,2)"}, '
        '{"name": "h", "type": "HUGEINT"}],"rows":['
    )
    check(found == expected, f"the head: {found}")


def _json_rows(client: KitClient) -> None:
    schema, batch = _read_schema(client)
    stream = pa.Table.from_batches([batch], schema.schema)
    check(client.json_rows(stream, True) == ('["1.50", null]',), "exact text, NULL as null")
    check(client.json_rows(stream, False) == (',["1.50", null]',), "a later row is separated")


# --- separation ----------------------------------------------------------------------------------


def _object_storage(client: KitClient) -> None:
    for location in ("s3://b/k", "gs://b/k", "az://c/k"):
        check(client.is_object_storage(location), f"{location} is object storage")
    for location in ("/data/lake", "http://host/x", "S3://b/k"):
        check(not client.is_object_storage(location), f"{location} is not object storage")


def _components(client: KitClient) -> None:
    found = client.location_components("c", "s3://bucket/key/")
    check(found == ("object", "bucket", "key"), f"object storage: {found}")
    check(client.location_components("c", "/")[0] == "local", "a local path")
    for location in ("relative/path", "s3://bucket/../key", "http://host/x", "/a\x00b"):
        refused(SOURCE, functools.partial(client.location_components, "c", location))


def _overlap(client: KitClient) -> None:
    check(client.locations_overlap("c", "s3://b/a", "s3://b/a/x"), "inside overlaps")
    check(not client.locations_overlap("c", "s3://b/a", "s3://b/ab"), "a sibling does not")


def _apart(client: KitClient) -> None:
    client.check_apart("c", "s3://bucket/lake/", None, ("s3://bucket/other/",), ())


def _case(
    identifier: str, operation: str, expectation: str, run: Callable[[KitClient], None]
) -> KitCase:
    return KitCase(identifier, operation, expectation, run)


CASES = (
    # lake
    KitCase(
        "open-lake.idempotent",
        "open_lake",
        "one key opens one lake",
        Idempotent(_open_call, _lakes),
    ),
    KitCase(
        "close-lake.idempotent",
        "close_lake",
        "one key closes once",
        Idempotent(_close_call, lambda c: (_closing.of(c), _lakes(c))[1]),
    ),
    _case(
        "held.lists-open",
        "held",
        "what is held is listed; a closed lake is let go and refused",
        _held_lists,
    ),
    _case(
        "current-snapshot.after-commit",
        "current_snapshot",
        "the snapshot a commit made is current",
        _current_after_commit,
    ),
    _case(
        "snapshot.kept-or-none",
        "snapshot",
        "a kept snapshot is found; an unknown or negative id is None",
        _snapshot_unknown,
    ),
    _case("tables.lower-cased", "tables", "every table, lower-cased, in order", _tables),
    _case(
        "describe.binder-only",
        "describe",
        "a SELECT's columns from the binder; anything else refused",
        _describe,
    ),
    _case(
        "describe-source.landed-types",
        "describe_source",
        "a source's columns as they land; an unknown source refused",
        _describe_source,
    ),
    _case(
        "statistics.counts",
        "statistics",
        "every row is counted; an unknown table refused",
        _statistics,
    ),
    _case(
        "changes.removed",
        "changes",
        "removed rows are counted; `since` after `snapshot` refused",
        _changes,
    ),
    _case(
        "table-columns.as-of",
        "table_columns",
        "the columns as of the snapshot asked",
        _table_columns,
    ),
    # read
    _case(
        "start-read.exact",
        "start_read",
        "DECIMAL, HUGEINT, TIMESTAMPTZ and NULL are read exactly",
        _exact,
    ),
    _case(
        "start-read.parameters",
        "start_read",
        "a parameter is bound by name with its type",
        _read_parameters,
    ),
    _case(
        "read-batch.ends",
        "read_batch",
        "every row once, then None; a forged read is refused",
        _read_ends,
    ),
    KitCase(
        "cancel-read.idempotent",
        "cancel_read",
        "one key cancels once",
        Idempotent(lambda c, e: c.send("cancel_read", e, _cancelling.of(c)), _cancelled),
    ),
    KitCase(
        "close-read.idempotent",
        "close_read",
        "one key lets a read go once",
        Idempotent(
            lambda c, e: c.send("close_read", e, _closing_read.of(c)),
            lambda c: (_closing_read.of(c), _reads(c))[1],
        ),
    ),
    # write
    KitCase(
        "begin.idempotent",
        "begin",
        "one key begins one transaction",
        Idempotent(
            lambda c, e: c.send("begin", e, _beginning.of(c)),
            lambda c: (_beginning.of(c), _transactions(c))[1],
        ),
    ),
    KitCase(
        "execute.idempotent",
        "execute",
        "one key inserts once",
        Idempotent(
            lambda c, e: c.send(
                "execute", e, _executing.of(c), statement("INSERT INTO t VALUES (1)")
            ),
            lambda c: _count(c, _executing.of(c), "t"),
        ),
    ),
    _case("value.first-cell", "value", "the first cell as text; None when there is no row", _value),
    KitCase(
        "land.idempotent",
        "land",
        "one key lands once",
        Idempotent(
            lambda c, e: c.send(
                "land",
                e,
                _landing.of(c),
                "field",
                "SELECT * FROM meters",
                (),
                "main",
                "landed",
                None,
            ),
            lambda c: _count(c, _landing.of(c), "landed"),
        ),
    ),
    KitCase(
        "load-arrow.idempotent",
        "load_arrow",
        "one key loads once",
        Idempotent(
            lambda c, e: c.send(
                "load_arrow",
                e,
                _loading.of(c),
                "main",
                "loaded",
                pa.table({"a": pa.array([1, 2], pa.int64())}),
                (("a", "BIGINT"),),
                None,
            ),
            lambda c: _count(c, _loading.of(c), "loaded"),
        ),
    ),
    _case("load-arrow.exact", "load_arrow", "a DECIMAL and a NULL are loaded exactly", _load_exact),
    KitCase(
        "cancel-transaction.idempotent",
        "cancel_transaction",
        "one key cancels once",
        Idempotent(
            lambda c, e: c.send("cancel_transaction", e, _cancelling_write.of(c)),
            lambda c: (_cancelling_write.of(c), _cancelled(c))[1],
        ),
    ),
    _case(
        "cancel-transaction.rolls-back",
        "cancel_transaction",
        "a cancelled transaction never commits",
        _cancel_rolls_back,
    ),
    KitCase(
        "commit.idempotent",
        "commit",
        "one key commits once and answers the same",
        Idempotent(lambda c, e: c.send("commit", e, _committing.of(c)[1]), _committed),
    ),
    KitCase(
        "rollback.idempotent",
        "rollback",
        "one key rolls back once",
        Idempotent(
            lambda c, e: c.send("rollback", e, _rolling_back.of(c)),
            lambda c: (_rolling_back.of(c), _transactions(c))[1],
        ),
    ),
    _case(
        "rollback.discards",
        "rollback",
        "a rolled-back transaction writes nothing and is let go",
        _rollback_discards,
    ),
    # maintenance
    KitCase(
        "maintain.idempotent",
        "maintain",
        "one key maintains once and answers the same",
        Idempotent(
            lambda c, e: c.send("maintain", e, _maintaining.of(c)[0], _now(), _now()),
            _expired,
        ),
    ),
    # existing lake
    KitCase(
        "open-existing-lake.idempotent",
        "open_existing_lake",
        "one key attaches once",
        Idempotent(
            lambda c, e: c.send("open_existing_lake", e, _attaching.of(c), EXISTING_LIMITS),
            lambda c: (_attaching.of(c), _existing(c))[1],
        ),
    ),
    KitCase(
        "close-existing-lake.idempotent",
        "close_existing_lake",
        "one key detaches once",
        Idempotent(
            lambda c, e: c.send("close_existing_lake", e, _detaching.of(c)),
            lambda c: (_detaching.of(c), _existing(c))[1],
        ),
    ),
    _case(
        "existing-latest.reads",
        "existing_latest",
        "latest, verify, committed-at, columns, views and a read, exact",
        _existing_reads,
    ),
    # clickhouse
    _case(
        "close-clickhouse.harmless",
        "close_clickhouse",
        "closing what is not held changes nothing",
        _clickhouse_close,
    ),
    # encoding
    _case(
        "json-frame.columns",
        "json_frame",
        "the columns with their DuckDB types and the closing tail; no type is refused",
        _json_frame,
    ),
    _case("json-rows.exact", "json_rows", "exact text, NULL as null, rows separated", _json_rows),
    # separation
    _case(
        "is-object-storage.schemes",
        "is_object_storage",
        "s3://, gs:// and az:// only, case as written",
        _object_storage,
    ),
    _case(
        "location-components.parts",
        "location_components",
        "object storage and local paths by parts; anything else refused",
        _components,
    ),
    _case(
        "locations-overlap.prefix",
        "locations_overlap",
        "inside overlaps, a sibling with the same prefix does not",
        _overlap,
    ),
    _case(
        "check-apart.protected",
        "check_apart",
        "a lake inside a protected location is refused",
        _apart,
    ),
)

RECORDED = 153
STORE_KIT = Kit(contract=STORE, recorded=RECORDED, cases=(*CASES, *code_cases(STORE)))
