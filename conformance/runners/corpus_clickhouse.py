"""Running a corpus case on ClickHouse (P2-31 PR 4, #225; ADR-0027 §4, ADR-0028 §3): the case's
tables loaded into a ClickHouse database with the mapped types, and the case answered there
through the product's own wiring: the store's verified, read-only connection
(`orvex.store.clickhouse`), the server's adapter (`clickhouse_adapter`) and the evidence its
schedule verifies (`clickhouse_evidence`).

The server is `CORPUS_CLICKHOUSE` (`host:port:user`, an administrator of a disposable server)
with the password in `CORPUS_CLICKHOUSE_PASSWORD` (empty when unset). Each case gets its own
database, a read-only settings profile with the settings the store requires, and a reader that
may only read that database; all three are dropped after the case. The loader copies each table
of the case's managed lake, as `corpus_execution` loaded it, with the same values and the
mapped types (`Nullable` where the lake's column is). The question is then answered with every
dataset routed to ClickHouse; the managed lake is read only for the policies' mapping values.

The engine is offered only with the development switch on (`<SLUG>_DEV_CLICKHOUSE=1`); with it
off no adapter is supplied, and every case is refused for its route, as the product refuses it.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from orvex.contracts.store.v1 import ReadRequest, TablesRequest
from orvex.semantic.adapters import clickhouse as ch
from orvex.semantic.adapters.clickhouse import CAPABILITIES as CLICKHOUSE_CAPABILITIES
from orvex.semantic.compiler.compiled import CompiledModel
from orvex.semantic.engines import EngineEntry, ExternalAdapter, Routing
from orvex.semantic.execution import answer
from orvex.semantic.policies import PolicyModel
from orvex.server import development_switch
from orvex.server.clickhouse_adapter import ClickHouseAdapter
from orvex.server.clickhouse_engines import tables
from orvex.server.clickhouse_evidence import EvidenceSchedule, checked
from orvex.server.identity import load_identity
from orvex.store.clickhouse import REQUIRED_SETTINGS, Budget, ClickHouse, ClickHouseTarget
from orvex.store.lake import Lake

from conformance.runners import corpus_execution as ce

SERVER = "CORPUS_CLICKHOUSE"
PASSWORD = "CORPUS_CLICKHOUSE_PASSWORD"
READER_PASSWORD = "CORPUS_CLICKHOUSE_READER_PASSWORD"  # set for each case, never stored
CONNECTION = "corpus_warehouse"
BUDGET = Budget(timeout_ms=60_000, memory_bytes=2_000_000_000)
PROFILE = {
    "readonly": "1",
    **REQUIRED_SETTINGS,
    "max_execution_time": "60",
    "max_memory_usage": "2000000000",
}


class NotConfigured(RuntimeError):
    """No ClickHouse server is given for the corpus."""


class Admin:
    """The administrator's HTTP connection to the disposable server."""

    def __init__(self) -> None:
        named = os.environ.get(SERVER)
        if not named:
            raise NotConfigured(f"{SERVER} names the ClickHouse server (host:port:user)")
        self.host, port, self.user = named.split(":")
        self.port = int(port)
        self.password = os.environ.get(PASSWORD, "")

    def run(self, statement: str, database: str = "") -> str:
        query = urllib.parse.urlencode({"database": database} if database else {})
        request = urllib.request.Request(
            f"http://{self.host}:{self.port}/?{query}",
            data=statement.encode(),
            headers={"X-ClickHouse-User": self.user, "X-ClickHouse-Key": self.password},
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return str(response.read().decode())
        except urllib.error.HTTPError as error:
            with error:
                raise RuntimeError(error.read().decode()[:2000]) from None


@contextmanager
def database() -> Iterator[tuple[Admin, str]]:
    """A database, its read-only profile and its reader, dropped after the block."""
    admin = Admin()
    name = f"corpus_{secrets.token_hex(6)}"
    password = secrets.token_hex(16)
    listed = ", ".join(f"{key} = '{value}'" for key, value in PROFILE.items())
    admin.run(f"CREATE DATABASE {name}")
    try:
        admin.run(f"CREATE SETTINGS PROFILE {name} SETTINGS {listed}")
        admin.run(f"CREATE USER {name} IDENTIFIED BY '{password}' SETTINGS PROFILE {name}")
        admin.run(f"GRANT SELECT ON {name}.* TO {name}")
        os.environ[READER_PASSWORD] = password
        yield admin, name
    finally:
        os.environ.pop(READER_PASSWORD, None)
        admin.run(f"DROP USER IF EXISTS {name}")
        admin.run(f"DROP SETTINGS PROFILE IF EXISTS {name}")
        admin.run(f"DROP DATABASE IF EXISTS {name}")


def load(admin: Admin, name: str, lake: Lake) -> None:
    """Every table of `lake`'s current snapshot, in the mapped ClickHouse types, same rows."""
    snapshot = lake.current_snapshot().id
    found = lake.table_columns(TablesRequest(kind="tables", snapshot=snapshot))
    for table in found.tables:
        if table.schema_ != "main":
            raise RuntimeError("a ClickHouse dataset reads a table of its connection's database")
        listed = ", ".join(
            f"{ch.quote_name(c.name)} "
            + (f"Nullable({ch.engine_type(c.type)})" if c.nullable else ch.engine_type(c.type))
            for c in table.columns
        )
        written = ch.quote_name(table.table)
        admin.run(f"CREATE TABLE {written} ({listed}) ENGINE = MergeTree ORDER BY tuple()", name)
        texts = ", ".join(
            f"strftime(timezone('UTC', {_quoted(c.name)}), '%Y-%m-%d %H:%M:%S.%f')"
            if c.type.upper().startswith("TIMESTAMP")
            else f"CAST({_quoted(c.name)} AS VARCHAR)"
            for c in table.columns
        )
        request = ReadRequest(
            kind="read",
            sql=f"SELECT {texts} FROM {_quoted(table.table)}",
            parameters=[],
            snapshot=snapshot,
            batch_size=10_000,
        )
        rows = lake.read(request).read_all().to_pylist()
        if not rows:
            continue
        names = [c.name for c in table.columns]
        body = "\n".join(json.dumps(dict(zip(names, r.values(), strict=True))) for r in rows)
        admin.run(
            f"INSERT INTO {written} SETTINGS input_format_null_as_default = 0,"
            f" date_time_input_format = 'best_effort' FORMAT JSONEachRow\n{body}",
            name,
        )


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def clickhouse(
    query: str,
    compiled: CompiledModel,
    policies: PolicyModel,
    context: Mapping[str, Any],
    lake: Lake,
) -> ce.Answered:
    """The answer with every dataset on ClickHouse, holding `lake`'s tables."""
    datasets: Any = compiled.ossie.document.get("datasets", ())
    with database() as (admin, name):
        load(admin, name, lake)
        target = ClickHouseTarget(admin.host, admin.port, name, name, READER_PASSWORD, tls=False)
        engine = ClickHouse.connect(target, BUDGET)
        entry = EngineEntry(
            CONNECTION,
            "clickhouse",
            "live",
            f"{admin.host}:{admin.port}/{name}",
            CLICKHOUSE_CAPABILITIES,
        )
        routing = Routing({str(d["name"]): entry for d in datasets})
        _, _, columns = tables(engine)
        schedule = EvidenceSchedule(
            CONNECTION,
            None,
            lambda now: checked(compiled, routing, CONNECTION, engine, columns, now),
        )
        schedule.check(dt.datetime.now(dt.UTC))
        adapters: dict[str, ExternalAdapter] = {}
        if development_switch.clickhouse(load_identity()):
            adapters = {
                CONNECTION: ClickHouseAdapter(
                    CONNECTION, engine, columns, evidence=schedule.records
                )
            }
        found = answer(
            query,
            compiled,
            policies,
            dict(context),
            ce.engine_for(lake, routing=routing, adapters=adapters),
        )
        execution = found.receipt.document()["execution"]
        if execution["engine"] != "clickhouse":
            raise AssertionError("a corpus question on ClickHouse was answered elsewhere")
        return ce.Answered(found.result.envelope(), ce.rows_of(found))


def run_case(
    case: str,
    folder: Path,
    model_path: Path,
    expected_code: str | None,
    schema: str | None = None,
) -> ce.CaseRun:
    """`corpus_execution.run_case` on ClickHouse."""
    return ce.run_case(case, folder, model_path, expected_code, schema, ask=clickhouse)
