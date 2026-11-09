"""The served project's ClickHouse connections in the `serve` process (P2-31 PR 4, #225;
ADR-0027 §4, §5, §7, §8).

With the development switch on (`development_switch.clickhouse`), every `clickhouse` connection
of the served project is connected and verified when the server starts (`orvex.store.clickhouse`:
a tested version, a read-only profile, the settings that decide exact meaning, limits within
the budget below). A connection that fails verification is that error for every question on
it, never a weaker connection. With the switch off, none is, and a dataset on one is
`UNSUPPORTED_OPERATION` (`operation: route`).

- **Questions** (`pinned`): each connection's tables as its catalog lists them now, with its
  adapter (`clickhouse_adapter`), so the model is compiled against the columns it reads.
- **Evidence** (`clickhouse_evidence`): checked once before the server answers, then whenever a
  connection's schedule is due, by one background thread (`checking`), holding the project's
  turn so the model is compiled as questions see it.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass

from orvex.contracts.project.v1 import ClickhouseConnection
from orvex.contracts.store.v1 import LakeTable, TablesResult
from orvex.semantic.evidence import EvidenceRecord
from orvex.server.clickhouse_adapter import ClickHouseAdapter
from orvex.server.clickhouse_evidence import EvidenceSchedule, checked
from orvex.server.existing_lakes import memory_share
from orvex.server.project import ProjectLoadError
from orvex.server.semantic_http import ProjectBusy, ProjectSource, Refused
from orvex.server.semantic_model import ExternalTables
from orvex.store.clickhouse import Budget, ClickHouse, ClickHouseTarget
from orvex.store.errors import StoreError

log = logging.getLogger(__name__)
POLL_SECONDS = 60.0  # how often the thread looks for a due schedule (each is at least 1h)
SCHEMA = "main"  # a connection's database, as a dataset's unqualified `source` names it


def memory_bytes(limit: str) -> int:
    """A memory budget (`<n>MB`, `<n>GB` or `<n>TB`) in bytes."""
    return int(memory_share(limit, 1).removesuffix("MB")) * 1024 * 1024


def clickhouse_connections(connections: Mapping[str, object]) -> dict[str, ClickhouseConnection]:
    return {n: c for n, c in connections.items() if isinstance(c, ClickhouseConnection)}


def _variable(reference: str) -> str:
    """`NAME` of `${env:NAME}`."""
    return reference[len("${env:") : -1]


def target(connection: ClickhouseConnection) -> ClickHouseTarget:
    """Where the server is and who connects; the password stays in its variable."""
    root = connection.tls_root_cert or "system"
    ca_file = None if root == "system" else os.environ.get(_variable(root))
    return ClickHouseTarget(
        host=connection.host,
        port=connection.port,
        database=connection.database,
        user=connection.user,
        password_env=_variable(connection.password.root),
        tls=connection.tls is not False,
        ca_file=ca_file,
    )


def tables(engine: ClickHouse) -> tuple[TablesResult, dict[str, StoreError], dict[str, list[str]]]:
    """The database's tables as a dataset reads them, the tables refused, and each table's
    exact column names."""
    found, refused = engine.catalog()
    listed = [
        LakeTable.model_validate(
            {
                "schema": SCHEMA,
                "table": table,
                "columns": [
                    {"name": c.name, "type": c.type, "nullable": _nullable(c.engine_type)}
                    for c in columns
                ],
            }
        )
        for table, columns in sorted(found.items())
    ]
    names = {table: [c.name for c in columns] for table, columns in found.items()}
    keyed = {f"{SCHEMA}.{table}".casefold(): error for table, error in refused.items()}
    return TablesResult(kind="tables_result", snapshot=0, tables=listed), keyed, names


def _nullable(engine_type: str) -> bool:
    inner = engine_type.strip()
    if inner.startswith("LowCardinality(") and inner.endswith(")"):
        inner = inner[len("LowCardinality(") : -1].strip()
    return inner.startswith("Nullable(")


@dataclass(slots=True)
class _Connection:
    engine: ClickHouse | StoreError
    schedule: EvidenceSchedule


class ClickHouseEngines:
    """The verified ClickHouse connections of one served project, by name."""

    def __init__(self, now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC)) -> None:
        self._now = now
        self._connections: dict[str, _Connection] = {}
        self._source: ProjectSource | None = None

    def __len__(self) -> int:
        return len(self._connections)

    def add(self, name: str, engine: ClickHouse | StoreError, schedule: str | None) -> None:
        """`engine` (or why it could not be verified) and its evidence schedule."""
        made = EvidenceSchedule(name, schedule, lambda now: self._verified(name, now))
        self._connections[name] = _Connection(engine, made)

    def schedule(self, name: str) -> EvidenceSchedule:
        return self._connections[name].schedule

    def pinned(self) -> dict[str, ExternalTables | StoreError]:
        """For one question: every connection's tables now, with its adapter, or its error."""
        found: dict[str, ExternalTables | StoreError] = {}
        for name, entry in self._connections.items():
            engine = entry.engine
            if isinstance(engine, StoreError):
                found[name] = engine
                continue
            try:
                listed, refused, names = tables(engine)
            except StoreError as error:
                found[name] = error
                continue
            adapter = ClickHouseAdapter(name, engine, names, evidence=entry.schedule.records)
            found[name] = ExternalTables(listed, frozenset(), adapter, refused)
        return found

    # --- the evidence schedule ---------------------------------------------------------------

    def check(self, source: ProjectSource) -> None:
        """Every due connection's evidence verified, holding the project's turn."""
        now = self._now()
        due = [(n, c) for n, c in self._connections.items() if c.schedule.due(now)]
        if not due:
            return
        self._source = source
        try:
            for name, entry in due:
                try:
                    entry.schedule.check(now)
                except (StoreError, ProjectBusy, Refused, ProjectLoadError) as error:
                    log.warning(
                        "clickhouse evidence not verified", extra={"connection": name, "why": error}
                    )
        finally:
            self._source = None

    def _verified(self, name: str, now: dt.datetime) -> tuple[EvidenceRecord, ...]:
        """The evidence records of connection `name` now; none without a model."""
        source, engine = self._source, self._connections[name].engine
        if source is None or isinstance(engine, StoreError):
            return ()
        with source.session() as lake:
            loaded = source.model(lake, lake.current_snapshot().id)
        model = loaded.model
        if model is None:
            return ()
        _, _, names = tables(engine)
        return checked(model.compiled, model.routing, name, engine, names, now)

    @contextmanager
    def checking(self, source: ProjectSource, poll: float = POLL_SECONDS) -> Iterator[None]:
        """Every connection's evidence checked now, then whenever due, until the block ends."""
        self.check(source)
        stop = threading.Event()

        def loop() -> None:
            while not stop.wait(poll):
                try:
                    self.check(source)
                except Exception:  # the thread must outlive one failed check, which is logged
                    log.exception("clickhouse evidence check failed")

        thread = threading.Thread(target=loop, name="clickhouse-evidence", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join()


@contextmanager
def opened(
    connections: Mapping[str, object], *, timeout_ms: int, memory_limit: str
) -> Iterator[ClickHouseEngines]:
    """Every `clickhouse` connection of the project connected and verified (each refusal kept
    as its error), with its evidence schedule. `timeout_ms` and `memory_limit` are the managed
    lake's per-question budgets (`QUERY_TIMEOUT_MS`, `commands.memory_limit`)."""
    engines = ClickHouseEngines()
    budget = Budget(timeout_ms=timeout_ms, memory_bytes=memory_bytes(memory_limit))
    for name, connection in sorted(clickhouse_connections(connections).items()):
        try:
            engine: ClickHouse | StoreError = ClickHouse.connect(target(connection), budget)
        except StoreError as error:
            engine = error
        engines.add(name, engine, connection.evidence_schedule)
    yield engines
