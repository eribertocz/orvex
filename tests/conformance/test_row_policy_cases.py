"""The normative row-policy cases, run (#441): every `semantic-query` case of the `rows`, `leak`
and `direct-query` areas that is not pending, on every engine it lists, with its caller and its
control, against the fixture it is written for.

- `managed`: the fixture's tables in a temporary managed lake.
- `existing-ducklake`: the fixture's datasets in an existing DuckLake that another system owns,
  built from `lake.toml` on each catalog it lists (SQLite, and PostgreSQL where one is configured,
  as CI requires); the policies' mapping tables stay in the managed lake (ADR-0027 §6).
- `clickhouse`: the datasets in the configured ClickHouse server (`SEMANTIC_TEST_CLICKHOUSE`),
  required in CI.

Each answer is checked whole: the outcome, the exact rows in order, the notice or the code, and
that no forbidden value is a whole token anywhere the caller receives: the result, its notices,
the caller's own view of the receipt, or the error's code and details. #447 runs every other
area through the same fixtures (`conformance/runners/security_fixtures.py`).
"""

from __future__ import annotations

import csv
import io
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb
import pytest
from orvex.contracts.store.v1 import (
    ExistingLakeAttachment,
    PostgresLakeLocation,
    SqliteLakeLocation,
)
from orvex.semantic.engines import EngineEntry, Routing
from orvex.semantic.errors import SemanticError
from orvex.semantic.execution import Answer, answer
from orvex.semantic.receipt.viewer import CallerViewer
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
from tests.conformance.test_security_scenarios import SEPARATOR, cases
from tests.support.environment import unavailable

AREAS = frozenset({"rows", "indirect-leakage", "direct-query"})
PROJECT = "security-cases"
POSTGRES_PASSWORD = "STORE_TEST_POSTGRES_PASSWORD"


def _runnable() -> list[tuple[str, str, str]]:
    """(case, engine, catalog) for every case this module runs, its pending engines included:
    there the case must be refused before any read (`test_a_pending_engine_refuses_...`)."""
    found = []
    for name, case in sorted(cases().items()):
        if case["area"] not in AREAS or case["request"]["kind"] != "semantic-query":
            continue
        if "pending" in case:
            continue
        for engine in case["engines"]:
            if engine == "existing-ducklake":
                settings = sf.fixture(case["fixture"], ce.VENDORS[0]).lake_settings()
                found += [(name, engine, c) for c in settings["catalogs"]]
            else:
                found.append((name, engine, ""))
    return found


RUNNABLE = _runnable()
HOLDING = [(n, e, c) for n, e, c in RUNNABLE if e not in cases()[n].get("pending_engines", {})]


# --- engines ---------------------------------------------------------------------------------


@dataclass
class Served:
    """The managed lake (the policies' mapping tables, and the datasets for `managed`) and the
    routing and adapters an engine other than the managed lake adds."""

    lake: Lake
    routing: Routing | None = None
    adapters: Mapping[str, Any] | None = None
    reads: list[object] | None = None  # every read the other engine made

    def engine(self) -> Any:  # noqa: ANN401 - `execution.Engine`
        more: dict[str, Any] = {}
        if self.routing is not None:
            more = {"routing": self.routing, "adapters": dict(self.adapters or {})}
        return ce.engine_for(self.lake, **more)


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


@contextmanager
def _existing_lake(found: sf.Fixture, catalog: str) -> Iterator[ExistingLakeAttachment]:
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
            writer.execute(
                f"ATTACH 'ducklake:sqlite:{path}' AS l "
                f"(DATA_PATH '{data.as_posix()}/', DATA_INLINING_ROW_LIMIT 0)"
            )
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
            writer.execute(
                f"ATTACH 'ducklake:postgres:{as_owner}' AS l "
                f"(DATA_PATH '{data.as_posix()}/', METADATA_SCHEMA 'cat', "
                "DATA_INLINING_ROW_LIMIT 0)"
            )
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
        yield ExistingLakeAttachment(
            kind="existing_lake_attachment",
            name="warehouse",
            location=location,
            roles_in_use=[],
            protected_files=[],
            protected_locations=[],
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
            yield Served(lake)
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
            with _existing_lake(found, catalog) as attachment:
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
                    yield Served(
                        lake,
                        Routing(dict.fromkeys(names, entry)),
                        {entry.connection: adapter},
                        made,
                    )
                finally:
                    existing.close()
            return
        with _clickhouse(found) as (_, client):
            _, evidence, wiring = che.wired(client, found.compiled)
            evidence.check(NOW)
            yield Served(lake, wiring["routing"], wiring["adapters"])


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


def ask(found: sf.Fixture, place: Served, request: Mapping[str, Any], caller: Any) -> Received:  # noqa: ANN401
    policies = found.policies(caller["groups"])
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


def expected_rows(text: str) -> tuple[list[str], list[list[str | None]]]:
    lines = list(csv.reader(io.StringIO(text)))
    return lines[0], [[None if v == "NULL" else v for v in row] for row in lines[1:]]


def check(received: Received, expect: Mapping[str, Any], label: str) -> None:
    outcome = expect["outcome"]
    if outcome == "error":
        assert received.code == expect["code"], (label, received)
    else:
        assert received.code is None, (label, received)
    if outcome == "rows":
        columns, rows = expected_rows(expect["rows"])
        assert received.columns == columns, (label, received.columns)
        assert received.rows == rows, (label, received.rows)
    if outcome == "zero-rows":
        assert received.rows == [], (label, received.rows)
    if "notice" in expect:
        assert expect["notice"] in received.notices, (label, received.notices)
    tokens = {token for text in received.texts for token in SEPARATOR.split(text)}
    leaked = sorted(set(expect.get("forbidden", ())) & tokens)
    assert not leaked, (label, leaked)


@pytest.mark.parametrize(
    ("name", "engine", "catalog"),
    HOLDING,
    ids=[f"{n}-{e}{'-' + c if c else ''}" for n, e, c in HOLDING],
)
def test_a_row_policy_case_holds_on_its_engine(name: str, engine: str, catalog: str) -> None:
    case = cases()[name]
    found = sf.fixture(case["fixture"], ce.VENDORS[0])
    with served(found, engine, catalog) as place:
        check(ask(found, place, case["request"], case["caller"]), case["expect"], name)
        control = case["control"]
        check(
            ask(found, place, control.get("request", case["request"]), control["caller"]),
            {**control, "forbidden": ()},
            f"{name} control",
        )


def test_the_cases_this_module_runs_are_the_issues_cases() -> None:
    """#441 AC2: the 15 cases it took out of pending, and nothing else silently dropped."""
    taken = {n for n, e, _ in RUNNABLE if e not in cases()[n].get("pending_engines", {})}
    expected = {f"SEC-ROWS-00{i}" for i in range(1, 9)} | {"SEC-LEAK-001", "SEC-LEAK-002"}
    expected |= {f"SEC-DQ-00{i}" for i in range(1, 6)}
    assert expected <= taken
    for name, case in cases().items():
        if case["area"] in AREAS and case["request"]["kind"] == "semantic-query":
            assert name in taken or "pending" in case, name


PENDING = [(n, e, c) for n, e, c in RUNNABLE if e in cases()[n].get("pending_engines", {})]


@pytest.mark.parametrize(
    ("name", "engine", "catalog"), PENDING, ids=[f"{n}-{e}-{c}" for n, e, c in PENDING]
)
def test_a_pending_engine_refuses_before_any_read(name: str, engine: str, catalog: str) -> None:
    """A case pending on one engine is never counted as passing there: its request, for its
    caller and its control, is refused before that engine reads anything."""
    case = cases()[name]
    found = sf.fixture(case["fixture"], ce.VENDORS[0])
    with served(found, engine, catalog) as place:
        for caller in (case["caller"], case["control"]["caller"]):
            received = ask(found, place, case["request"], caller)
            assert received.code == "UNSUPPORTED_OPERATION", received
            assert received.rows is None
        assert place.reads == []


def test_the_pending_engines_are_the_existing_lakes_detail_pages() -> None:
    """#441 (Owner, option A): only the existing DuckLake waits, for its detail pages (P5-14)."""
    waiting = {(n, e): cases()[n]["pending_engines"][e] for n, e, _ in PENDING}
    assert waiting == {(f"SEC-DQ-00{i}", "existing-ducklake"): "P5-14" for i in range(1, 6)}
