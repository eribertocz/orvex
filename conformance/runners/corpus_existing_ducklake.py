"""Running a corpus case on an existing DuckLake (P2-30 PR 3, #399; ADR-0027 §3, §5): the case's
tables written by another system into a DuckLake with a PostgreSQL catalog, attached read-only
with the existing-DuckLake engine, and the case answered through it.

Each case gets a database of its own on the PostgreSQL server `CORPUS_POSTGRES` (a libpq
connection string without password; the password in `CORPUS_POSTGRES_PASSWORD`), with an owner
role that writes the lake and a dedicated reader role that may read only its catalog schema, as
the engine requires. The owner copies each table of the case's managed lake, as
`corpus_execution` loaded it, into the existing lake with the same column types. The question is
then answered with every dataset routed to the existing lake: its snapshot verified on the
schedule, its evidence checked, the engine's adapter pinned at the served snapshot, and reads
through the store's guarded reads. The managed lake is never read for data: its read refuses.

The engine is offered only with the development switch on (`<SLUG>_DEV_EXISTING_DUCKLAKE=1`);
with it off no adapter is supplied, and every case is refused for its route, as the product
refuses it. Database, roles and data are dropped after each case.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import tempfile
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from importlib.metadata import version
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
from orvex.contracts.store.v1 import (
    ExistingLakeAttachment,
    PostgresLakeLocation,
    ReadRequest,
    Snapshot,
    TablesRequest,
)
from orvex.semantic.compiler.compiled import CompiledModel
from orvex.semantic.engines import EngineEntry, Routing
from orvex.semantic.execution import answer
from orvex.semantic.policies import PolicyModel
from orvex.server import development_switch
from orvex.server.engine_catalog import EXISTING_DUCKLAKE_CAPABILITIES
from orvex.server.existing_lake_adapter import ExistingLakeAdapter
from orvex.server.existing_lake_evidence import violated
from orvex.server.existing_lake_snapshots import SnapshotSchedule
from orvex.server.identity import load_identity
from orvex.store.existing_lake import ExistingLake
from orvex.store.existing_lake_reads import LakeReads
from orvex.store.lake import Lake

from conformance.runners import corpus_execution as ce

SERVER = "CORPUS_POSTGRES"
PASSWORD = "CORPUS_POSTGRES_PASSWORD"
CONNECTION = "corpus_lake"
SCHEMA = "cat"
PROJECT = "corpus"
TIMEOUT_MS = 600_000


class NotConfigured(RuntimeError):
    """No PostgreSQL server is given for the existing lake's catalog."""


def settings() -> dict[str, str]:
    """The PostgreSQL server's libpq settings, without password."""
    text = os.environ.get(SERVER)
    if not text or not os.environ.get(PASSWORD):
        raise NotConfigured(f"{SERVER} and {PASSWORD} name the PostgreSQL server for the catalog")
    return dict(pair.split("=", 1) for pair in text.split())


def _libpq(pairs: Mapping[str, str]) -> str:
    return " ".join(f"{key}={value}" for key, value in pairs.items())


def _connect(*extensions: str) -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect()
    for name in extensions:
        connection.install_extension(name)
        connection.load_extension(name)
    quoted = os.environ[PASSWORD].replace("'", "''")
    connection.execute(f"CREATE TEMPORARY SECRET (TYPE postgres, PASSWORD '{quoted}')")
    return connection


class Catalog:
    """One case's PostgreSQL database: the owner's lake catalog in schema `cat`, and a reader
    that may read only that schema."""

    def __init__(self) -> None:
        self.settings = settings()
        suffix = uuid.uuid4().hex[:12]
        self.database = f"corpus_{suffix}"
        self.owner, self.reader = f"corpus_own_{suffix}", f"corpus_rd_{suffix}"
        self.data = Path(tempfile.mkdtemp(prefix="corpus-existing-ducklake-"))
        self._admin = _connect("postgres")
        self._admin.execute(f"ATTACH '{_libpq(self.settings)}' AS admin (TYPE postgres)")
        self._on("admin", f"CREATE DATABASE {self.database}")
        quoted = os.environ[PASSWORD].replace("'", "''")
        for role in (self.owner, self.reader):
            self._on("admin", f"CREATE ROLE {role} LOGIN PASSWORD '{quoted}'")
        target = _libpq({**self.settings, "dbname": self.database})
        self._admin.execute(f"ATTACH '{target}' AS db (TYPE postgres)")
        self._on(
            "db",
            "REVOKE ALL ON SCHEMA public FROM PUBLIC",
            f"CREATE SCHEMA {SCHEMA} AUTHORIZATION {self.owner}",
            f"GRANT USAGE ON SCHEMA {SCHEMA} TO {self.reader}",
        )

    def _on(self, database: str, *statements: str) -> None:
        for sql in statements:
            quoted = sql.replace("'", "''")
            self._admin.execute(
                f"CALL postgres_execute('{database}', '{quoted}', use_transaction := false)"
            )

    def _as(self, user: str) -> str:
        return _libpq({**self.settings, "dbname": self.database, "user": user})

    def write(self, managed: Lake) -> None:
        """Every table of `managed`'s current snapshot, written by the owner with its types."""
        snapshot = managed.current_snapshot().id
        found = managed.table_columns(TablesRequest(kind="tables", snapshot=snapshot))
        writer = _connect("ducklake", "postgres")
        writer.execute(
            f"ATTACH 'ducklake:postgres:{self._as(self.owner)}' AS l "
            f"(DATA_PATH '{self.data.as_posix()}/', METADATA_SCHEMA '{SCHEMA}')"
        )
        for table in found.tables:
            name = f'l."{table.schema_}"."{table.table}"'
            if table.schema_ != "main":
                writer.execute(f'CREATE SCHEMA IF NOT EXISTS l."{table.schema_}"')
            columns = ", ".join(f'"{c.name}" {c.type}' for c in table.columns)
            writer.execute(f"CREATE TABLE {name} ({columns})")
            rows = managed.read(
                ReadRequest(
                    kind="read",
                    sql=f'SELECT * FROM "{table.schema_}"."{table.table}"',
                    parameters=[],
                    snapshot=snapshot,
                    batch_size=10_000,
                )
            ).read_all()
            writer.register("copied", rows)
            writer.execute(f"INSERT INTO {name} SELECT * FROM copied")
            writer.unregister("copied")
        writer.close()
        self._on("db", f"GRANT SELECT ON ALL TABLES IN SCHEMA {SCHEMA} TO {self.reader}")

    def attachment(self) -> ExistingLakeAttachment:
        return ExistingLakeAttachment(
            kind="existing_lake_attachment",
            name=CONNECTION,
            location=PostgresLakeLocation(
                kind="lake_location",
                catalog="postgres",
                connection=self._as(self.reader),
                password_env=PASSWORD,
                metadata_schema=SCHEMA,
                data_path=self.data.as_posix() + "/",
            ),
            roles_in_use=[],
            protected_files=[],
            protected_locations=[],
        )

    def environment(self) -> str:
        return f"postgres://{self.settings.get('host')}/{self.database}/{SCHEMA}|{self.data}/"

    def close(self) -> None:
        self._admin.execute("DETACH db")
        self._on("admin", f"DROP DATABASE {self.database} WITH (FORCE)")
        for role in (self.owner, self.reader):
            self._on("admin", f"DROP ROLE {role}")
        self._admin.close()
        shutil.rmtree(self.data, ignore_errors=True)


@contextmanager
def catalog() -> Iterator[Catalog]:
    made = Catalog()
    try:
        yield made
    finally:
        made.close()


def _managed_read(request: object) -> pa.RecordBatchReader:
    raise AssertionError("a corpus question on the existing lake read the managed lake")


def existing_ducklake(
    query: str,
    compiled: CompiledModel,
    policies: PolicyModel,
    context: Mapping[str, Any],
    lake: Lake,
) -> ce.Answered:
    """The answer with every dataset on an existing DuckLake holding `lake`'s tables."""
    datasets: Any = compiled.ossie.document.get("datasets", ())
    with catalog() as made:
        made.write(lake)
        entry = EngineEntry(
            CONNECTION,
            "ducklake",
            "existing_ducklake",
            made.environment(),
            EXISTING_DUCKLAKE_CAPABILITIES,
        )
        routing = Routing({str(d["name"]): entry for d in datasets})
        with ExistingLake(made.attachment(), memory_limit="1GB", threads=2) as existing:
            reads = LakeReads(existing, timeout_ms=TIMEOUT_MS)

            def evidence(snapshot: Snapshot) -> str | None:
                return violated(
                    compiled,
                    routing,
                    CONNECTION,
                    lambda request: reads.read(request, snapshot.committed_at),
                    snapshot,
                    ce.INSTANT,
                )

            schedule = SnapshotSchedule(
                PROJECT,
                CONNECTION,
                reads,
                attachment=made.attachment,
                evidence=evidence,
                schedule=None,
                freshness=None,
            )
            schedule.check(dt.datetime.now(dt.UTC))
            adapters: dict[str, ExistingLakeAdapter] = {}
            if development_switch.existing_ducklake(load_identity()):
                adapter = ExistingLakeAdapter(reads, schedule, version("duckdb"))
                adapters = {CONNECTION: adapter.fixed()}
            found = answer(
                query,
                compiled,
                policies,
                dict(context),
                ce.engine_for(lake, read=_managed_read, routing=routing, adapters=adapters),
            )
            return ce.Answered(found.result.envelope(), ce.rows_of(found))


def run_case(
    case: str,
    folder: Path,
    model_path: Path,
    expected_code: str | None,
    schema: str | None = None,
) -> ce.CaseRun:
    """`corpus_execution.run_case` on an existing DuckLake."""
    return ce.run_case(case, folder, model_path, expected_code, schema, ask=existing_ducklake)
