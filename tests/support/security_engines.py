"""Serving a security fixture on each engine, and asking it as a caller (#441; #447 reuses it):
- `managed`: the fixture's tables in a temporary managed lake;
- `existing-ducklake`: its datasets in an existing DuckLake that another system owns, built from
  `lake.toml` on SQLite or PostgreSQL (required in CI); the mapping tables stay in the managed
  lake (ADR-0027 §6);
- `clickhouse`: its datasets in a database of their own on the configured server (required in CI).

`ask` returns what the caller receives for a query, a catalog or an `explain`: rows, columns,
notices, the error code, a catalog's members, and every text (the result, the caller's view of
the receipt, the document, the error's code and details) for the forbidden check.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import duckdb
from orvex.contracts.store.v1 import (
    ExistingLakeAttachment,
    PostgresLakeLocation,
    SqliteLakeLocation,
    StatementRequest,
)
from orvex.semantic.catalog.building import catalog_for_context
from orvex.semantic.engines import EngineEntry, Routing
from orvex.semantic.errors import SemanticError
from orvex.semantic.execution import Answer, answer
from orvex.semantic.explanation import explain
from orvex.semantic.receipt.viewer import CallerViewer
from orvex.semantic.result_cache import ResultCache
from orvex.server.engine_catalog import EXISTING_DUCKLAKE_CAPABILITIES
from orvex.server.existing_lake_adapter import ExistingLakeAdapter
from orvex.server.existing_lake_snapshots import SnapshotSchedule
from orvex.store.clickhouse import ClickHouse, ClickHouseTarget
from orvex.store.existing_lake import ExistingLake
from orvex.store.existing_lake_reads import LakeReads
from orvex.store.json_encoding import arrow_to_json
from orvex.store.lake import Lake

from conformance.runners import corpus_execution as ce
from conformance.runners import security_fixtures as sf
from tests.conformance import test_clickhouse_adapter as chs
from tests.conformance import test_clickhouse_engine as che
from tests.conformance.test_existing_ducklake import NOW
from tests.support.environment import unavailable

PROJECT = "security-cases"
POSTGRES_PASSWORD = "STORE_TEST_POSTGRES_PASSWORD"


@dataclass
class Served:
    """The managed lake (the policies' mapping tables, and the datasets for `managed`) and the
    routing and adapters an engine other than the managed lake adds."""

    lake: Lake
    routing: Routing | None = None
    adapters: Mapping[str, Any] | None = None
    reads: list[object] | None = None  # every read the other engine made
    # Deletes the rows of a dataset's table matching the given values, as that engine's owner
    # would, and makes the result the engine's served version (#444, `delete_row`).
    delete: Callable[[str, Mapping[str, object]], None] | None = None
    cache: ResultCache | None = None  # the result cache every question of this place shares

    def engine(self) -> Any:  # noqa: ANN401 - `execution.Engine`
        more: dict[str, Any] = {}
        if self.routing is not None:
            more = {"routing": self.routing, "adapters": dict(self.adapters or {})}
        return replace(ce.engine_for(self.lake, **more), cache=self.cache)


def _datasets(found: sf.Fixture) -> list[str]:
    """The tables the fixture's datasets read: every table that is not a managed mapping."""
    return [name for name in found.catalog if name not in found.managed]


def _sql_value(value: str | None) -> str:
    return "NULL" if value is None else "'" + value.replace("'", "''") + "'"


def _snapshots(found: sf.Fixture) -> list[list[str]]:
    """The owner's statements, one list per snapshot of `lake.toml`, in order."""
    settings = found.lake_settings()
    created: set[str] = set()
    statements: list[list[str]] = []
    for entry in settings["snapshots"]:
        table = entry["table"]
        columns = found.catalog[table]
        rows = list(csv.reader(io.StringIO((found.folder / entry["file"]).read_text("utf-8"))))
        step = []
        if table not in created:
            listed = ", ".join(f'"{c}" {kind.type}' for c, kind in columns.items())
            step.append(f'CREATE TABLE l."{table}" ({listed})')
            created.add(table)
        else:
            step.append(f'DELETE FROM l."{table}"')
        for row in rows[1:]:
            values = ", ".join(_sql_value(None if v == "NULL" else v) for v in row)
            step.append(f'INSERT INTO l."{table}" VALUES ({values})')
        statements.append(step)
    return statements


def _postgres() -> dict[str, str]:
    connection = os.environ.get("STORE_TEST_POSTGRES")
    if not connection or not os.environ.get(POSTGRES_PASSWORD):
        unavailable(
            "no PostgreSQL configured for this run",
            "STORE_TEST_POSTGRES and STORE_TEST_POSTGRES_PASSWORD are required in CI",
        )
    return dict(pair.split("=", 1) for pair in connection.split())


def _execute(writer: duckdb.DuckDBPyConnection, database: str, sql: str) -> None:
    quoted = sql.replace("'", "''")
    writer.execute(f"CALL postgres_execute('{database}', '{quoted}', use_transaction := false)")


@dataclass
class _Owned:
    """An existing DuckLake as its owner holds it: the attachment a reader is given, and the
    owner's own way back in to change it."""

    attachment: ExistingLakeAttachment
    owner: Callable[[str], None]  # one statement, run by the owner in its own transaction


@contextmanager
def _existing_lake(found: sf.Fixture, catalog: str) -> Iterator[_Owned]:
    """An existing DuckLake that another system owns and writes, on `catalog`, holding the
    fixture's datasets; its data on a file system of its own (ADR-0027 §3)."""
    shared = Path("/dev/shm")
    if not shared.is_dir() or shared.stat().st_dev == Path(tempfile.gettempdir()).stat().st_dev:
        unavailable("no second file system here", "CI needs /dev/shm on a file system of its own")
    root = Path(tempfile.mkdtemp(dir=shared, prefix="security-cases-"))
    data = root / found.lake_settings()["data_location"]
    data.mkdir(parents=True)
    writer = duckdb.connect()
    cleanup: list[str] = []
    try:
        for extension in ("ducklake", "sqlite", "postgres"):
            writer.install_extension(extension)
            writer.load_extension(extension)
        if catalog == "sqlite":
            path = root / "catalog.sqlite"
            attach = (
                f"ATTACH 'ducklake:sqlite:{path}' AS l "
                f"(DATA_PATH '{data.as_posix()}/', DATA_INLINING_ROW_LIMIT 0)"
            )
            writer.execute(attach)
            location: Any = SqliteLakeLocation(
                kind="lake_location", catalog="sqlite", path=str(path), data_path=f"{data}/"
            )
        else:
            # A database of its own, a catalog schema its owner writes and a reader role that
            # may read only that schema, as a careful DBA sets them up (ADR-0027 §3).
            settings = _postgres()
            suffix = uuid.uuid4().hex[:12]
            database, owner, reader = f"sc_{suffix}", f"own_{suffix}", f"rd_{suffix}"
            quoted = os.environ[POSTGRES_PASSWORD].replace("'", "''")
            writer.execute(f"CREATE TEMPORARY SECRET (TYPE postgres, PASSWORD '{quoted}')")
            admin = " ".join(f"{k}={v}" for k, v in settings.items())
            writer.execute(f"ATTACH '{admin}' AS admin (TYPE postgres)")
            _execute(writer, "admin", f"CREATE DATABASE {database}")
            for role in (owner, reader):
                _execute(writer, "admin", f"CREATE ROLE {role} LOGIN PASSWORD '{quoted}'")
            cleanup += [database, owner, reader]
            on = " ".join(f"{k}={v}" for k, v in {**settings, "dbname": database}.items())
            writer.execute(f"ATTACH '{on}' AS db (TYPE postgres)")
            _execute(writer, "db", "REVOKE ALL ON SCHEMA public FROM PUBLIC")
            _execute(writer, "db", f"CREATE SCHEMA cat AUTHORIZATION {owner}")
            _execute(writer, "db", f"GRANT USAGE ON SCHEMA cat TO {reader}")
            as_owner = " ".join(
                f"{k}={v}" for k, v in {**settings, "dbname": database, "user": owner}.items()
            )
            attach = (
                f"ATTACH 'ducklake:postgres:{as_owner}' AS l "
                f"(DATA_PATH '{data.as_posix()}/', METADATA_SCHEMA 'cat', "
                "DATA_INLINING_ROW_LIMIT 0)"
            )
            writer.execute(attach)
            location = PostgresLakeLocation(
                kind="lake_location",
                catalog="postgres",
                connection=" ".join(
                    f"{k}={v}" for k, v in {**settings, "dbname": database, "user": reader}.items()
                ),
                password_env=POSTGRES_PASSWORD,
                metadata_schema="cat",
                data_path=f"{data}/",
            )
        for step in _snapshots(found):
            writer.execute("BEGIN TRANSACTION")
            for statement in step:
                writer.execute(statement)
            writer.execute("COMMIT")
        writer.execute("DETACH l")
        if catalog == "postgres":
            _execute(writer, "db", f"GRANT SELECT ON ALL TABLES IN SCHEMA cat TO {cleanup[2]}")
            writer.execute("DETACH db")
        if catalog == "sqlite":  # an owner keeps a shared catalog in WAL mode (ADR-0027 §3)
            connection = sqlite3.connect(root / "catalog.sqlite")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.close()

        def changed(statement: str) -> None:
            writer.execute(attach)
            try:
                writer.execute(statement)
            finally:
                writer.execute("DETACH l")

        yield _Owned(
            ExistingLakeAttachment(
                kind="existing_lake_attachment",
                name="warehouse",
                location=location,
                roles_in_use=[],
                protected_files=[],
                protected_locations=[],
            ),
            changed,
        )
    finally:
        if cleanup:
            database, owner, reader = cleanup
            _execute(writer, "admin", f"DROP DATABASE {database} WITH (FORCE)")
            for role in (owner, reader):
                _execute(writer, "admin", f"DROP ROLE {role}")
        writer.close()
        shutil.rmtree(root, ignore_errors=True)


@contextmanager
def served(found: sf.Fixture, engine: str, catalog: str) -> Iterator[Served]:
    if engine == "managed":
        with sf.managed_lake(found) as lake:

            def deleted(table: str, row: Mapping[str, object]) -> None:
                where = " AND ".join(f'"{k}" = {_sql_value(str(v))}' for k, v in row.items())
                with lake.transaction() as transaction:
                    transaction.execute(StatementRequest(
                        kind="statement", parameters=[],
                        sql=f'DELETE FROM "{table}" WHERE {where}'))  # fmt: skip

            yield Served(lake, delete=deleted)
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
            with _existing_lake(found, catalog) as owned:
                attachment = owned.attachment
                existing = ExistingLake(attachment, memory_limit="512MB", threads=1)
                try:
                    reads = LakeReads(existing, timeout_ms=60_000)
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
                        attachment=lambda: attachment,
                        evidence=lambda snapshot: None,
                        schedule="5m",
                        freshness="1h",
                    )
                    assert schedule.check(NOW).color == "green"
                    adapter = ExistingLakeAdapter(reads, schedule, "test")
                    checks = [NOW]

                    def deleted(table: str, row: Mapping[str, object]) -> None:
                        where = " AND ".join(
                            f'"{k}" = {_sql_value(str(v))}' for k, v in row.items()
                        )
                        owned.owner(f'DELETE FROM l."{table}" WHERE {where}')
                        checks.append(checks[-1] + dt.timedelta(minutes=10))  # its schedule
                        assert schedule.check(checks[-1]).color == "green"

                    yield Served(
                        lake,
                        Routing(dict.fromkeys(names, entry)),
                        {entry.connection: adapter},
                        made,
                        deleted,
                    )
                finally:
                    existing.close()
            return
        with _clickhouse(found) as (server, client):
            _, evidence, wiring = che.wired(client, found.compiled)
            evidence.check(NOW)

            def deleted(table: str, row: Mapping[str, object]) -> None:
                where = " AND ".join(f"`{k}` = {_sql_value(str(v))}" for k, v in row.items())
                server.run(
                    f"ALTER TABLE {server.database}.`{table}` DELETE WHERE {where} "
                    "SETTINGS mutations_sync = 2"
                )

            yield Served(lake, wiring["routing"], wiring["adapters"], delete=deleted)


@contextmanager
def _clickhouse(found: sf.Fixture) -> Iterator[tuple[chs.Server, ClickHouse]]:
    """The fixture's datasets in a database of their own on the configured server, read by a
    reader that may only select from it, as `test_clickhouse_adapter.server` sets one up."""
    named = os.environ.get("SEMANTIC_TEST_CLICKHOUSE")
    if not named:
        unavailable(
            "no ClickHouse server configured for this run",
            "SEMANTIC_TEST_CLICKHOUSE is required in CI",
        )
    host, port, user = named.split(":")
    server = chs.Server(
        host, int(port), user, os.environ.get("SEMANTIC_TEST_CLICKHOUSE_PASSWORD", "")
    )
    name = f"s_{uuid.uuid4().hex[:12]}"
    password = uuid.uuid4().hex
    server.run(f"CREATE DATABASE {name}")
    listed = ", ".join(f"{key} = '{value}'" for key, value in chs.PROFILE.items())
    server.run(f"CREATE SETTINGS PROFILE {name} SETTINGS {listed}")
    server.run(f"CREATE USER {name} IDENTIFIED BY '{password}' SETTINGS PROFILE {name}")
    server.run(f"GRANT SELECT ON {name}.* TO {name}")
    server.database = name
    try:
        tables = _datasets(found)
        chs.load(server, {t: found.catalog[t] for t in tables}, {t: found.rows[t] for t in tables})
        os.environ[che.PASSWORD] = password
        target = ClickHouseTarget(server.host, server.port, name, name, che.PASSWORD, tls=False)
        yield server, ClickHouse.connect(target, che.BUDGET)
    finally:
        os.environ.pop(che.PASSWORD, None)
        server.database = ""
        server.run(f"DROP USER IF EXISTS {name}")
        server.run(f"DROP SETTINGS PROFILE IF EXISTS {name}")
        server.run(f"DROP DATABASE IF EXISTS {name}")


# --- one answer, as the caller receives it ---------------------------------------------------


@dataclass(frozen=True)
class Received:
    rows: list[list[str | None]] | None
    columns: list[str]
    notices: list[str]
    code: str | None
    texts: list[str]  # everything the caller receives, as text
    members: frozenset[str] = frozenset()  # a catalog's fields and metrics, `dataset.field`


def ask(found: sf.Fixture, place: Served, request: Mapping[str, Any], caller: Any) -> Received:  # noqa: ANN401
    """The caller's answer to a case request: a `semantic-query`, the caller's `catalog`, or
    the caller's `explain` of a query."""
    policies = found.policies(caller["groups"])
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
    envelope = got.result.envelope()
    view = got.receipt.view(CallerViewer(found.compiled, policies, caller))
    return Received(
        ce.rows_of(got),
        [_column(c) for c in envelope["columns"]],
        [str(n["code"]) for n in envelope.get("notices", [])],
        None,
        [got.result.wire(arrow_to_json), json.dumps(view, default=str)],
    )


def _column(column: Mapping[str, Any]) -> str:
    """A column as a case's header names it: `dataset.field`, or a metric's name."""
    member = column.get("member") or {"name": column["name"]}
    named = [str(member[k]) for k in ("dataset", "name") if k in member]
    return ".".join(named).lower()
