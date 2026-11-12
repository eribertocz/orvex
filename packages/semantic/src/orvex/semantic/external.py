"""Plans another engine runs (P2-29, #370; P2-31, #225; ADR-0027 §3 to §8): which engine a plan
runs on, its policies' mapping values read on the managed lake first, its pinned data version,
its own cost estimate and evidence, and the data reads its receipt names.

- **One engine, decided before any read.** Datasets on two engines are `CROSS_ENGINE_QUERY`;
  an engine with no adapter in this process is `UNSUPPORTED_OPERATION` (`route`), and one that
  lacks a capability the plan needs names it (`engines.refuse_unable`).
- **Policies.** Each mapping subquery is read on the managed lake at the question's snapshot,
  as exact text, under the caps on the values and bytes bound (`POLICY_NOT_SUPPORTED_ON_ENGINE`,
  `value_cap`, never the count).
- **Version.** Pinned once per read of a question: a snapshot of an existing DuckLake, or a
  live engine's read instant, which every detail page takes anew (its cursor names the managed
  snapshot its policies were read at, never the live engine's data).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pyarrow as pa
from orvex.semantic.adapters.duckdb import (
    MappingQuery,
    PhysicalQuery,
    PolicyValues,
    mapping_queries,
)
from orvex.semantic.compiler.compiled import CompiledModel
from orvex.semantic.engines import (
    EngineEntry,
    ExternalAdapter,
    Routing,
    dataset_label,
    refuse_mixed,
    refuse_unable,
)
from orvex.semantic.errors import SemanticError
from orvex.semantic.evidence import EvidenceRecord, Ledger
from orvex.semantic.policies import PolicyModel
from orvex.semantic.receipt import DataRead

Run = Callable[[str, Sequence[Mapping[str, str | None]]], pa.RecordBatchReader]
# A live engine's evidence records have no snapshot: each holds from its verification instant
# until the next one on the connection's schedule.
LIVE = 0


def _instant(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class External:
    """A plan another engine runs (P2-29): its connection, adapter and pinned data version,
    and the mapping subqueries of its policies, read on the managed lake first."""

    entry: EngineEntry
    adapter: ExternalAdapter
    version: Mapping[str, object]  # pinned by `_execute`, never by `prepare`, which reads nothing
    queries: tuple[MappingQuery, ...]
    policies: Mapping[str, int]  # the caps: `policy_values`, `policy_value_bytes`
    timeout_ms: int | None

    def runner(self, batch_size: int) -> Run:
        def run(
            statement: str, parameters: Sequence[Mapping[str, str | None]]
        ) -> pa.RecordBatchReader:
            return self.adapter.read(
                statement, parameters, self.version, batch_size, self.timeout_ms
            )

        return run

    def ledger(self, compiled: CompiledModel) -> Ledger:
        """The engine's own evidence records: a live engine's, verified on its schedule; none
        for an existing DuckLake, whose evidence holds back a violating snapshot instead."""
        records: Iterable[EvidenceRecord] = self.adapter.evidence()
        return Ledger(compiled, records, LIVE)

    def estimate(self, physical: PhysicalQuery) -> Mapping[str, int] | None:
        """The engine's own estimate of the read, recorded only; None without statistics."""
        return self.adapter.estimate(physical)

    def read(self, name: str, entry: EngineEntry | None) -> DataRead:
        """The dataset's data version on this engine, through its own connection (two names
        may share one environment): a snapshot, or a live read instant."""
        version = self.version
        connection = (entry or self.entry).connection
        if self.entry.route == "live":
            return DataRead(
                name=name,
                route="live",
                retained=False,
                read_at=_instant(datetime.fromisoformat(str(version["read_at"]))),
                connection=connection,
            )
        return DataRead(
            name=name,
            route=self.entry.route,
            retained=True,
            snapshot_id=int(str(version["id"])),
            committed_at=_instant(datetime.fromisoformat(str(version["committed_at"]))),
            project=str(version["project"]),
            connection=connection,
        )


# The types a mapping value keeps exactly as text, and is cast back from, on any engine.
_EXACT_TEXT = re.compile(
    r"(TINYINT|SMALLINT|INTEGER|BIGINT|HUGEINT|UTINYINT|USMALLINT|UINTEGER|UBIGINT|UHUGEINT"
    r"|VARCHAR|BOOLEAN|DATE|UUID|DECIMAL\(\d+,\d+\))"
)


def resolve(
    queries: Sequence[MappingQuery], run: Run, caps: Mapping[str, int]
) -> tuple[dict[int, PolicyValues], list[MappingQuery]]:
    """Each mapping subquery's values, read through `run` on the managed lake at the question's
    snapshot (ADR-0027 §6), as exact text, with the mapping column's type. Every value counts
    towards the caps on the values and bytes bound for another engine; past either, the
    question is refused with `POLICY_NOT_SUPPORTED_ON_ENGINE` (`value_cap`), naming the policy
    and never a size, and nothing more is read. A type whose text is not exact everywhere is
    refused."""
    count_cap, byte_cap = int(caps["policy_values"]), int(caps["policy_value_bytes"])
    counted = sized = 0
    values: dict[int, PolicyValues] = {}

    def refused(query: MappingQuery, reason: str) -> SemanticError:
        details = {"dataset": query.dataset, "policy": query.policy, "reason": reason}
        return SemanticError("POLICY_NOT_SUPPORTED_ON_ENGINE", details)

    for query in queries:
        # The column's declared type, with no row needed: a scalar subquery's type.
        typed = f'SELECT typeof((SELECT "value" FROM ({query.sql}) AS "#q" LIMIT 1)) AS "#type"'
        kind = str(run(typed, query.parameters).read_all().column("#type")[0].as_py())
        if not query.exists and not _EXACT_TEXT.fullmatch(kind):
            raise refused(query, "engine_unsupported")
        statement = (
            f'SELECT CAST("value" AS VARCHAR) AS "#value" FROM ({query.sql}) AS "#q" '
            f"LIMIT {count_cap - counted + 1}"
        )
        found: list[str | None] = []
        for batch in run(statement, query.parameters):
            for value in batch.column("#value").to_pylist():
                counted += 0 if query.exists else 1  # whether a row matched: one bound flag
                sized += len(value.encode()) if value is not None else 0
                if counted > count_cap or sized > byte_cap:
                    raise refused(query, "value_cap")
                found.append(value)
        values[query.key] = PolicyValues(kind, tuple(found))
    return values, list(queries)


def external_of(
    logical: Any,  # noqa: ANN401 - a `SemanticPlan`
    compiled: CompiledModel,
    policies: PolicyModel,
    routing: Routing,
    adapters: Mapping[str, ExternalAdapter],
    timeout_ms: int | None,
) -> External | None:
    """The other engine a plan runs on, or None for the managed lake; refused before any read
    when its datasets are on two engines (`CROSS_ENGINE_QUERY`), its engine is not enabled
    (`UNSUPPORTED_OPERATION`, `route`) or lacks a capability the plan needs, or a policy's
    mapping subquery cannot be evaluated apart (`POLICY_NOT_SUPPORTED_ON_ENGINE`)."""
    datasets = [source.dataset for source in logical.sources]
    refuse_mixed(compiled, routing, datasets)
    entry = routing.of(datasets[0]) if datasets else None
    if entry is None:
        return None
    adapter = adapters.get(entry.connection)
    if adapter is None:
        raise SemanticError("UNSUPPORTED_OPERATION", {"operation": "route"})
    refuse_unable(entry, logical)
    refuse_cells(logical, compiled)
    queries = tuple(mapping_queries(logical))
    caps = {k: int(str(v)) for k, v in policies.limits.items()}
    return External(entry, adapter, {}, queries, caps, timeout_ms)


def refuse_cells(logical: Any, compiled: CompiledModel) -> None:  # noqa: ANN401 - a plan
    """A detail page showing a `visible_when` member by its condition runs on the managed lake
    only (#442): another engine refuses it before any read (`POLICY_NOT_SUPPORTED_ON_ENGINE`,
    `engine_unsupported`), never shows the value without its condition, and never a value's text
    in another engine's form. The member is named; the caller already sees it as masked."""
    placement, masks = logical.placement, dict(logical.masks)
    cells = placement.cells if placement is not None else {}
    for row in logical.rows:
        cell = cells.get(row.member) if row.name in masks else None
        if cell is not None and cell.condition is not None:
            label = dataset_label(compiled, cell.dataset)
            details = {"dataset": label, "policy": row.name, "reason": "engine_unsupported"}
            raise SemanticError("POLICY_NOT_SUPPORTED_ON_ENGINE", details)
