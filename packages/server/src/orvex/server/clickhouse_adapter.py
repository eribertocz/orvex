"""The ClickHouse engine as the semantic engine uses it (P2-31 PR 4, #225; ADR-0027 §5, §7, §8):
`orvex.semantic.engines.ExternalAdapter` for one enabled, verified ClickHouse connection.

- `pin`: the read instant, from the server's clock (`now64(6, 'UTC')`), taken once per read of
  a question; a live read is never repeatable, and its receipt is `explainable`.
- `compile`: the semantic engine's ClickHouse adapter, with the exact column names the engine's
  catalog holds; every statement it writes is remembered with its result columns, so a read is
  only ever of a statement this adapter wrote (or the zone check, C6).
- `estimate`: `EXPLAIN ESTIMATE` of the statement through the store, checked against the
  rows-scanned budget before the read (law 7); a table of an engine without an estimate gives
  none ("no statistics", `semantic-engine.md` §8).
- `read`: the store's read of that statement in its exact store types, under the question's
  time limit.
- `evidence`: the records the connection's evidence schedule verified (`clickhouse_evidence`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

import pyarrow as pa
from orvex.contracts.store.v1 import Parameter
from orvex.semantic.adapters import clickhouse as ch
from orvex.semantic.adapters.duckdb import PhysicalQuery, PolicyValues
from orvex.semantic.errors import PlanError
from orvex.semantic.evidence import EvidenceRecord
from orvex.store.clickhouse import ClickHouse

ENGINE = "clickhouse"
# The most rows one question may scan on ClickHouse, by the engine's own estimate (law 7).
ROWS_SCANNED_BUDGET = 1_000_000_000
_ZONE_CHECK = (("#different", "BIGINT"),)


def result_columns(query: PhysicalQuery) -> tuple[tuple[str, str], ...]:
    """The (name, store type) of each column the statement returns, in order."""
    found = [(c.name, c.encoding) for c in query.columns]
    found += [(n, "BOOLEAN") for n, _ in query.checks]
    found += [(n, "BIGINT") for n, _ in [*query.weights, *query.as_of]]
    found += [(query.level, "INTEGER")] if query.level else []
    found += list(query.hidden)
    return tuple(found)


class ClickHouseAdapter:
    """One enabled ClickHouse connection."""

    engine = ENGINE

    def __init__(
        self,
        connection: str,
        engine: ClickHouse,
        columns: Mapping[str, Sequence[str]],
        *,
        evidence: Callable[[], Iterable[EvidenceRecord]] = tuple,
        rows_scanned: int = ROWS_SCANNED_BUDGET,
    ) -> None:
        self.connection = connection
        self.engine_version = engine.version
        self._engine = engine
        self._columns = {table: tuple(names) for table, names in columns.items()}
        self._evidence = evidence
        self._rows_scanned = rows_scanned
        self._written: dict[str, tuple[tuple[str, str], ...]] = {ch.ZONE_CHECK_SQL: _ZONE_CHECK}

    def pin(self) -> Mapping[str, object]:
        return {"read_at": self._engine.now().isoformat()}

    def compile(self, plan: Any, values: Mapping[int, PolicyValues]) -> PhysicalQuery:  # noqa: ANN401 - a `SemanticPlan`
        query = ch.compile_plan(plan, self._columns, values)
        self._written[query.sql] = result_columns(query)
        return query

    def estimate(self, query: PhysicalQuery, restricted: bool) -> Mapping[str, int] | None:
        rows = self._engine.estimate(query.sql, _parameters(query.parameters))
        found = ch.rows_scanned(query, rows, self._engine.database)
        ch.refuse_over_budget(found, self._rows_scanned, restricted)
        return None if found is None else {"rows_scanned": found}

    def read(
        self,
        statement: str,
        parameters: Sequence[Mapping[str, str | None]],
        version: Mapping[str, object],
        batch_size: int,
        timeout_ms: int | None,
    ) -> pa.RecordBatchReader:
        columns = self._written.get(statement)
        if columns is None:
            raise PlanError("a statement this adapter did not write is never sent")
        return self._engine.read(statement, _parameters(parameters), columns, timeout_ms)

    def evidence(self) -> Iterable[EvidenceRecord]:
        return tuple(self._evidence())


def _parameters(parameters: Sequence[Mapping[str, str | None]]) -> list[Parameter]:
    return [Parameter.model_validate(dict(p)) for p in parameters]
