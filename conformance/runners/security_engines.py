"""Serving a security fixture on each engine, and asking it as a caller (#441, #447: the one
executor of the security runner and of the tests that use its engines):
- `managed`: the fixture's tables in a temporary managed lake;
- `existing-ducklake`: its datasets in an existing DuckLake that another system owns, built from
  `lake.toml` on SQLite or PostgreSQL (required in CI); the mapping tables stay in the managed
  lake (ADR-0027 §6);
- `clickhouse`: its datasets in a database of their own on a ClickHouse server
  (`ClickHouseServer`; by default the one `SEMANTIC_TEST_CLICKHOUSE` names), read by a reader
  that may only select from it.

An engine that cannot be served here calls `on_unavailable` (by default `EngineUnavailable`;
the tests make it a skip locally and a failure in CI).

`ask` returns what the caller receives for a query, a catalog or an `explain`: rows, columns,
notices, the error code (a store error as the server answers it: a lake's data refused with
`STORE_SOURCE_INVALID`, anything else `INTERNAL_ERROR`), a catalog's members, and every text
(the result, the caller's view of the receipt, the document, the error's code and details) for
the forbidden check.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any, NoReturn

import duckdb
from orvex.contracts.store.client import Lake
from orvex.contracts.store.client_sources import (
    ClickHouse,
    ExistingLakeReads,
    connect_clickhouse,
    open_existing_lake,
)
from orvex.contracts.store.errors import StoreError
from orvex.contracts.store.v1 import (
    StatementRequest,
)
from orvex.contracts.store.values import Budget, ClickHouseTarget, ExistingLakeLimits
from orvex.semantic.adapters import clickhouse as ch
from orvex.semantic.adapters.clickhouse import CAPABILITIES as CLICKHOUSE_CAPABILITIES
from orvex.semantic.catalog.building import catalog_for_context
from orvex.semantic.engines import EngineEntry, Routing
from orvex.semantic.errors import SemanticError
from orvex.semantic.execution import Answer, answer
from orvex.semantic.explanation import explain
from orvex.semantic.receipt.viewer import CallerViewer
from orvex.semantic.result_cache import ResultCache
from orvex.server import lake_refusals
from orvex.server.clickhouse_adapter import ClickHouseAdapter
from orvex.server.clickhouse_engines import tables
from orvex.server.clickhouse_evidence import EvidenceSchedule, checked
from orvex.server.engine_catalog import EXISTING_DUCKLAKE_CAPABILITIES
from orvex.server.existing_lake_adapter import ExistingLakeAdapter
from orvex.server.existing_lake_snapshots import SnapshotSchedule
from orvex.server.store_access import store

from conformance.runners import corpus_execution as ce
from conformance.runners import security_existing_lake as sx
from conformance.runners import security_fixtures as sf
from conformance.runners.security_existing_lake import sql_value

PROJECT = "security-cases"
INTERNAL = "INTERNAL_ERROR"  # what the server answers for a store error reaching it
NOW = dt.datetime(2026, 11, 8, 12, 0, tzinfo=dt.UTC)  # the schedules' first check
READER_PASSWORD = "SECURITY_CASES_CLICKHOUSE_READER"  # set for each served database only
BUDGET = Budget(timeout_ms=30_000, memory_bytes=1_000_000_000)
LIMITS = ExistingLakeLimits(memory_limit="512MB", threads=1, timeout_ms=60_000)
PROFILE = {
    "readonly": "1",
    "join_use_nulls": "1",
    "group_by_use_nulls": "1",
    "transform_null_in": "0",
    "count_distinct_implementation": "uniqExact",
    "cast_keep_nullable": "1",
    "aggregate_functions_null_for_empty": "0",
    "cancel_http_readonly_queries_on_client_close": "1",
    "max_execution_time": "30",
    "max_memory_usage": "1000000000",
}


class EngineUnavailable(RuntimeError):
    """An engine this run cannot serve: a service not configured or not reachable."""


def _raise(skip_reason: str, needed: str) -> NoReturn:
    raise EngineUnavailable(f"{needed} ({skip_reason})")


on_unavailable: Callable[[str, str], NoReturn] = _raise


def unavailable(skip_reason: str, needed: str) -> NoReturn:
    on_unavailable(skip_reason, needed)


@dataclass
class ClickHouseServer:
    """A ClickHouse server reached as its administrator over HTTP (`host:port:user`)."""

    host: str
    port: int
    user: str
    password: str
    database: str = ""

    @classmethod
    def named(cls, address: str, password: str = "") -> ClickHouseServer:
        host, port, user = address.split(":")
        return cls(host, int(port), user, password)

    def run(self, statement: str) -> str:
        query = urllib.parse.urlencode({"database": self.database} if self.database else {})
        request = urllib.request.Request(
            f"http://{self.host}:{self.port}/?{query}",
            data=statement.encode(),
            headers={"X-ClickHouse-User": self.user, "X-ClickHouse-Key": self.password},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return str(response.read().decode())
        except urllib.error.HTTPError as error:
            with error:
                raise RuntimeError(error.read().decode()[:2000]) from None

    def version(self) -> str:
        """The version the server reports, `26.10.1.2` for instance."""
        return self.run("SELECT version()").strip()


def default_clickhouse() -> ClickHouseServer:
    named = os.environ.get("SEMANTIC_TEST_CLICKHOUSE")
    if not named:
        unavailable(
            "no ClickHouse server configured for this run",
            "SEMANTIC_TEST_CLICKHOUSE is required in CI",
        )
    return ClickHouseServer.named(named, os.environ.get("SEMANTIC_TEST_CLICKHOUSE_PASSWORD", ""))


@dataclass
class Served:
    """The managed lake (the policies' mapping tables, and the datasets for `managed`) and the
    routing and adapters an engine other than the managed lake adds."""

    lake: Lake
    routing: Routing | None = None
    adapters: Mapping[str, Any] | None = None
    reads: list[object] | None = None  # every read the other engine made
    # Deletes the rows of a table matching the given values, as that engine's owner would (a
    # mapping in the managed lake), and makes the result the served version (`delete_row`).
    delete: Callable[[str, Mapping[str, object]], None] | None = None
    # Makes a case's `lake-change` on an existing DuckLake (`security_existing_lake.change`),
    # then lets its schedule verify the lake as it would next.
    lake_change: Callable[[Mapping[str, Any]], None] | None = None
    cache: ResultCache | None = None  # the result cache every question of this place shares

    def engine(self) -> Any:  # noqa: ANN401 - `execution.Engine`
        more: dict[str, Any] = {}
        if self.routing is not None:
            more = {"routing": self.routing, "adapters": dict(self.adapters or {})}
        return replace(ce.engine_for(self.lake, **more), cache=self.cache)


def _datasets(found: sf.Fixture) -> list[str]:
    """The tables the fixture's datasets read: every table that is not a managed mapping."""
    return [name for name in found.catalog if name not in found.managed]


class _Undeclared(ExistingLakeAdapter):
    """A connection whose declaration was refused: it serves nothing, and says why."""

    def __init__(
        self, reads: ExistingLakeReads, schedule: SnapshotSchedule, refusal: StoreError
    ) -> None:
        super().__init__(reads, schedule, "test")
        self._refusal = refusal

    def pin(self) -> Mapping[str, object]:
        raise self._refusal

    def read(self, *_: object) -> Any:  # noqa: ANN401 - never reached: nothing is pinned
        raise self._refusal


def _declared(
    host: sx.Host, reads: ExistingLakeReads, schedule: SnapshotSchedule, made: list[object]
) -> _Undeclared:
    """The connection declared again with its location as changed, as the server opens it: it
    must be refused before DuckLake attaches anything, with no read made (DQ-018)."""
    client = store()
    held = client.call("held").existing_lakes
    reads_before = len(made)
    try:
        open_existing_lake(client, host.attachment(), LIMITS).close()
    except StoreError as error:
        refusal = error
    else:
        raise RuntimeError("the lake was declared with a location overlapping a protected one")
    if "overlaps" not in str(refusal.details.get("reason", "")):  # refused, but for the overlap
        raise RuntimeError(f"the declaration was refused for another reason: {refusal.code}")
    # That nothing is attached before the refusal is the store's own test
    # (`test_existing_lake_files.py`); here, nothing is held and nothing is read.
    if client.call("held").existing_lakes != held or len(made) != reads_before:
        raise RuntimeError("the lake was held or read before its declaration was refused")
    return _Undeclared(reads, schedule, refusal)


def managed_delete(lake: Any, table: str, row: Mapping[str, object]) -> None:  # noqa: ANN401
    """Delete the matching rows of a managed-lake table (a dataset's, or a mapping's)."""
    where = " AND ".join(f'"{k}" = {sql_value(str(v))}' for k, v in row.items())
    named = ".".join(f'"{part}"' for part in table.split("."))
    with lake.transaction() as transaction:
        transaction.execute(StatementRequest(
            kind="statement", parameters=[], sql=f"DELETE FROM {named} WHERE {where}"))  # fmt: skip


@contextmanager
def served(
    found: sf.Fixture, engine: str, catalog: str, clickhouse: ClickHouseServer | None = None
) -> Iterator[Served]:
    """The fixture on `engine` (`catalog`: the existing DuckLake's, `sqlite` or `postgres`;
    `clickhouse`: the server, by default `default_clickhouse()`)."""
    if engine == "managed":
        with sf.managed_lake(found) as lake:
            yield Served(lake, delete=lambda table, row: managed_delete(lake, table, row))
        return
    datasets: Any = found.compiled.ossie.document.get("datasets", ())
    names = [str(d["name"]) for d in datasets]
    with sf.managed_lake(found, found.managed) as lake:
        if engine == "existing-ducklake":
            entry = EngineEntry(
                "warehouse",
                "ducklake",
                "existing_ducklake",
                "warehouse",  # the schedule's connection name
                EXISTING_DUCKLAKE_CAPABILITIES,
            )
            with sx.existing_lake(found, catalog, unavailable) as host:
                reads = open_existing_lake(store(), host.attachment(), LIMITS)
                try:
                    made: list[object] = []
                    reading = reads.read

                    def counted(request: Any, committed_at: Any) -> Any:  # noqa: ANN401
                        made.append(request)
                        return reading(request, committed_at)

                    reads.read = counted  # type: ignore[method-assign]
                    schedule = SnapshotSchedule(
                        PROJECT,
                        "warehouse",
                        reads,
                        attachment=host.attachment,  # the protected set as it stands
                        evidence=lambda snapshot: None,
                        schedule="5m",
                        freshness="1h",
                    )
                    assert schedule.check(NOW).color == "green"
                    adapter = ExistingLakeAdapter(reads, schedule, "test")
                    adapters: dict[str, Any] = {entry.connection: adapter}
                    checks = [NOW]

                    def deleted(table: str, row: Mapping[str, object]) -> None:
                        if table in found.managed:  # a mapping: in the managed lake
                            managed_delete(lake, table, row)
                            return
                        where = " AND ".join(f'"{k}" = {sql_value(str(v))}' for k, v in row.items())
                        host.owner(f'DELETE FROM l."{table}" WHERE {where}')
                        verified()
                        assert schedule.state(checks[-1]).color == "green"

                    def verified() -> None:  # the schedule's next check
                        checks.append(checks[-1] + dt.timedelta(minutes=10))
                        schedule.check(checks[-1])

                    def lake_changed(step: Mapping[str, Any]) -> None:
                        then = sx.change(host, step, verified)
                        if then == sx.VERIFY:
                            verified()
                        elif then == sx.DECLARE:
                            adapters[entry.connection] = _declared(host, reads, schedule, made)

                    yield Served(
                        lake,
                        Routing(dict.fromkeys(names, entry)),
                        adapters,
                        made,
                        deleted,
                        lake_change=lake_changed,
                    )
                finally:
                    reads.close()
            return
        with _clickhouse(found, clickhouse or default_clickhouse()) as (server, client):
            routing, adapters = _wired(client, found.compiled)

            def deleted(table: str, row: Mapping[str, object]) -> None:
                if table in found.managed:  # a mapping: in the managed lake
                    managed_delete(lake, table, row)
                    return
                where = " AND ".join(f"`{k}` = {sql_value(str(v))}" for k, v in row.items())
                server.run(
                    f"ALTER TABLE {server.database}.`{table}` DELETE WHERE {where} "
                    "SETTINGS mutations_sync = 2"
                )

            yield Served(lake, routing, adapters, delete=deleted)


def _wired(client: ClickHouse, compiled: Any) -> tuple[Routing, dict[str, Any]]:  # noqa: ANN401
    """Every dataset routed to ClickHouse, through its adapter and its checked evidence."""
    _, _, names = tables(client)
    datasets: Any = compiled.ossie.document.get("datasets", ())
    entry = EngineEntry(
        "warehouse", "clickhouse", "live", "localhost/test", CLICKHOUSE_CAPABILITIES
    )
    routing = Routing({str(d["name"]): entry for d in datasets})
    schedule = EvidenceSchedule(
        "warehouse", None, lambda now: checked(compiled, routing, "warehouse", client, names, now)
    )
    schedule.check(NOW)
    adapter = ClickHouseAdapter("warehouse", client, names, evidence=schedule.records)
    return routing, {"warehouse": adapter}


@contextmanager
def _clickhouse(
    found: sf.Fixture, server: ClickHouseServer
) -> Iterator[tuple[ClickHouseServer, ClickHouse]]:
    """The fixture's datasets in a database of their own on `server`, read by a reader that may
    only select from it."""
    name = f"s_{uuid.uuid4().hex[:12]}"
    password = uuid.uuid4().hex
    admin = ClickHouseServer(server.host, server.port, server.user, server.password)
    try:
        admin.run(f"CREATE DATABASE {name}")
    except OSError as error:
        unavailable(f"ClickHouse at {server.host}:{server.port} is not reachable", str(error))
    listed = ", ".join(f"{key} = '{value}'" for key, value in PROFILE.items())
    admin.run(f"CREATE SETTINGS PROFILE {name} SETTINGS {listed}")
    admin.run(f"CREATE USER {name} IDENTIFIED BY '{password}' SETTINGS PROFILE {name}")
    admin.run(f"GRANT SELECT ON {name}.* TO {name}")
    admin.database = name
    try:
        for table in _datasets(found):
            _load(admin, table, found.catalog[table], found.rows[table])
        os.environ[READER_PASSWORD] = password
        target = ClickHouseTarget(admin.host, admin.port, name, name, READER_PASSWORD, tls=False)
        yield admin, connect_clickhouse(store(), target, BUDGET)
    finally:
        os.environ.pop(READER_PASSWORD, None)
        admin.database = ""
        admin.run(f"DROP USER IF EXISTS {name}")
        admin.run(f"DROP SETTINGS PROFILE IF EXISTS {name}")
        admin.run(f"DROP DATABASE IF EXISTS {name}")


def _load(server: ClickHouseServer, table: str, columns: Mapping[str, Any], rows: sf.Rows) -> None:
    """One table, in the mapped ClickHouse types, with the same rows as the lake."""
    types = {
        n: f"Nullable({ch.engine_type(c.type)})" if c.nullable else ch.engine_type(c.type)
        for n, c in columns.items()
    }
    listed = ", ".join(f"{ch.quote_name(n)} {t}" for n, t in types.items())
    server.run(
        f"CREATE TABLE {ch.quote_name(table)} ({listed}) ENGINE = MergeTree ORDER BY tuple()"
    )
    if rows:
        body = "\n".join(json.dumps(dict(zip(columns, row, strict=True))) for row in rows)
        server.run(
            f"INSERT INTO {ch.quote_name(table)} SETTINGS input_format_null_as_default = 0"
            f" FORMAT JSONEachRow\n{body}"
        )


# --- one answer, as the caller receives it ---------------------------------------------------


@dataclass(frozen=True)
class Received:
    rows: list[list[str | None]] | None
    columns: list[str]
    notices: list[str]
    code: str | None
    texts: list[str]  # everything the caller receives, as text
    members: frozenset[str] = frozenset()  # a catalog's fields and metrics, `dataset.field`


def ask(
    found: sf.Fixture,
    place: Served,
    request: Mapping[str, Any],
    caller: Any,  # noqa: ANN401
    whole: bool = False,
) -> Any:  # noqa: ANN401 - a `Received`, or with `whole` also the `Answer`
    """The caller's answer to a case request: a `semantic-query`, the caller's `catalog`, or
    the caller's `explain` of a query; with `whole`, a query's answer too (its receipt)."""
    try:
        policies = found.policies(caller["groups"])
    except SemanticError as error:  # policies that do not read: the model is refused
        text = json.dumps({"code": error.code, "details": dict(error.details)})
        return Received(None, [], [], error.code, [text])
    if request["kind"] == "catalog":
        document = catalog_for_context(found.compiled, policies, dict(caller))
        members = {
            f"{d['name']}.{f['name']}".lower() for d in document["datasets"] for f in d["fields"]
        } | {str(m["name"]).lower() for d in document["datasets"] for m in d["metrics"]}
        members |= {str(m["name"]).lower() for m in document.get("metrics", [])}
        return Received(None, [], [], None, [json.dumps(document)], frozenset(members))
    if request["kind"] == "explain":
        try:
            document = explain(
                json.dumps(request["body"]), found.compiled, policies, dict(caller),
                place.engine(), CallerViewer(found.compiled, policies, caller),
            )  # fmt: skip
        except SemanticError as error:
            text = json.dumps({"code": error.code, "details": dict(error.details)})
            return Received(None, [], [], error.code, [text])
        return Received(None, [], [], None, [json.dumps(document, default=str)])
    try:
        got: Answer = answer(
            json.dumps(request["body"]), found.compiled, policies, dict(caller), place.engine()
        )
    except SemanticError as error:
        text = json.dumps({"code": error.code, "details": dict(error.details)})
        return Received(None, [], [], error.code, [text])
    except StoreError as error:  # as the server answers it (`app.store_refusal`)
        refused = lake_refusals.as_semantic(error)
        if refused is None:
            return Received(None, [], [], INTERNAL, [json.dumps({"code": INTERNAL})])
        text = json.dumps({"code": refused.code, "details": dict(refused.details)})
        return Received(None, [], [], refused.code, [text])
    except duckdb.Error:  # an error the store did not name: internal, as the server answers it
        return Received(None, [], [], INTERNAL, [json.dumps({"code": INTERNAL})])
    envelope = got.result.envelope()
    view = got.receipt.view(CallerViewer(found.compiled, policies, caller))
    received = Received(
        ce.rows_of(got),
        [column_name(c) for c in envelope["columns"]],
        [str(n["code"]) for n in envelope.get("notices", [])],
        None,
        [got.result.wire(store().arrow_to_json), json.dumps(view, default=str)],
    )
    return (received, got) if whole else received


def column_name(column: Mapping[str, Any]) -> str:
    """A column as a case's header names it: `dataset.field`, or a metric's name."""
    member = column.get("member") or {"name": column["name"]}
    named = [str(member[k]) for k in ("dataset", "name") if k in member]
    return ".".join(named).lower()
