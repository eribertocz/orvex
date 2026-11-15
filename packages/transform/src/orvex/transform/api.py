"""The public face of transform (#486 §2.1): `Transform`, the one implementation of the transform
interface contract (`orvex.contracts.transform.interface`); everything else here is internal.

Each operation calls the module's own functions with the arguments it is given and answers
with contract values. An operation that reads the columns of relations is given those it knows
as data: a relation it is not given is collected while the call runs (as if it could not be
described), and when any was missing the answer is `ColumnsNeeded` with all of them, so only a
call that had every relation it read answers (`orvex.contracts.transform.analysis`).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from collections.abc import Callable, Iterable

from orvex.contracts.envelope import CallEnvelope
from orvex.contracts.model_config.v1 import ModelError
from orvex.contracts.run_record.v1 import RunRecord
from orvex.contracts.store.v1 import ReadRequest, StatementRequest
from orvex.contracts.transform.analysis import (
    ColumnsNeeded,
    LineageRun,
    LineageScope,
    MeasureDifference,
    ReconciliationStage,
    Refusal,
)
from orvex.contracts.transform.interface import Attachment, Connection, Known, Tables
from orvex.contracts.transform.models import Columns, Discovery, Graph, Model
from orvex.contracts.transform.scheduling import History, Plan
from orvex.contracts.transform.steps import Comparison, ModelTest, PlannedStep, Watermark
from orvex.transform import (
    auto_filter,
    incremental,
    lineage,
    model_tests,
    reconciliation,
    run_records,
    schedule,
)
from orvex.transform import graph as model_graph
from orvex.transform import leftovers as leftover_tables
from orvex.transform import models as model_files
from orvex.transform import sources as source_connections


class _Lookup:
    """The columns of the relations an operation was given; the ones it was not, collected."""

    def __init__(self, known: Known) -> None:
        self._known = {entry.relation: entry.columns for entry in known}
        self.missing: list[tuple[str, ...]] = []

    def columns(self, relation: Iterable[str]) -> tuple[str, ...] | None:
        parts = tuple(relation)
        if parts in self._known:
            return self._known[parts]
        if parts not in self.missing:
            self.missing.append(parts)
        return None

    def answer[Answer](self, run: Callable[[], Answer]) -> Answer | ColumnsNeeded:
        try:
            found = run()
        except Exception:
            if self.missing:
                return ColumnsNeeded(tuple(self.missing))
            raise
        return ColumnsNeeded(tuple(self.missing)) if self.missing else found


def _lowered(columns: tuple[str, ...] | None) -> frozenset[str] | None:
    return None if columns is None else frozenset(name.lower() for name in columns)


def _listed(columns: tuple[str, ...] | None) -> list[str] | None:
    return None if columns is None else list(columns)


def _by_name(found: Iterable[Model]) -> dict[str, Model]:
    return {model.name: model for model in found}


class Transform:
    """Transform, reached through its interface contract; it holds no state."""

    def discover_models(
        self, envelope: CallEnvelope, folder: str, connections: dict[str, Connection]
    ) -> Discovery:
        return model_files.discover_models(folder, connections)  # type: ignore[arg-type]

    def build_graph(
        self,
        envelope: CallEnvelope,
        models: tuple[Model, ...],
        sources: tuple[str, ...],
        databases: tuple[str, ...],
        direct: tuple[str, ...],
    ) -> Graph:
        return model_graph.build_graph(
            models, set(sources), frozenset(databases), frozenset(direct)
        )

    def known_output_names(self, envelope: CallEnvelope, sql: str) -> tuple[str, ...] | None:
        names = model_graph.known_output_names(sql)
        return None if names is None else tuple(names)

    def config_problems(
        self,
        envelope: CallEnvelope,
        model: Model,
        project_zone: str,
        connection_types: dict[str, str],
    ) -> tuple[ModelError, ...]:
        return (
            *schedule.config_problems(model, project_zone),
            *incremental.config_problems(model),
            *reconciliation.problems(model, connection_types),
        )

    def column_problems(
        self, envelope: CallEnvelope, models: tuple[Model, ...], columns: dict[str, Columns]
    ) -> tuple[ModelError, ...]:
        return tuple(incremental.problems_for(models, columns))

    def test_problems(
        self,
        envelope: CallEnvelope,
        model: Model,
        columns: dict[str, Columns],
        models: tuple[Model, ...],
    ) -> tuple[ModelError, ...]:
        found = model_tests.problems(model, columns[model.name], columns, _by_name(models))
        return tuple(found)

    def since_bound_problem(
        self, envelope: CallEnvelope, model: Model, known: Known
    ) -> ModelError | ColumnsNeeded | None:
        lookup = _Lookup(known)
        return lookup.answer(
            lambda: incremental.since_bound_problem(
                model, lambda parts: _lowered(lookup.columns(parts))
            )
        )

    def uses_since(self, envelope: CallEnvelope, sql: str) -> bool:
        return incremental.uses_since(sql)

    def filtered_sql(
        self, envelope: CallEnvelope, sql: str, time_column: str, known: Known
    ) -> str | Refusal | ColumnsNeeded:
        lookup = _Lookup(known)
        found = lookup.answer(
            lambda: auto_filter.filtered_sql(
                sql, time_column, lambda path: _listed(lookup.columns(path))
            )
        )
        if isinstance(found, Refusal):
            return dataclasses.replace(found, line=auto_filter.line_of(sql, found.location))
        return found

    def compiled_sql(
        self, envelope: CallEnvelope, model: Model, models: tuple[Model, ...], sql: str | None
    ) -> str:
        return model_graph.compiled_sql(model, _by_name(models), sql)

    def test_relations(self, envelope: CallEnvelope, models: tuple[Model, ...]) -> dict[str, str]:
        return model_graph.test_relations(models)

    def test_steps(
        self,
        envelope: CallEnvelope,
        model: Model,
        relation: str,
        columns: Columns,
        relations: dict[str, str],
    ) -> tuple[ModelTest, ...]:
        return tuple(model_tests.steps(model, relation, columns, relations))

    def reconciliation_stage(
        self, envelope: CallEnvelope, model: Model, index: int, databases: tuple[str, ...]
    ) -> ReconciliationStage | None:
        check = (model.config.reconciliation or [])[index]
        database = reconciliation.database_of(check, frozenset(databases))
        if database is None:
            return None
        return ReconciliationStage(
            database,
            reconciliation.source_side(check),
            reconciliation.STAGE_SCHEMA,
            reconciliation.staged_name(model, index),
        )

    def measures_sql(
        self, envelope: CallEnvelope, model: Model, index: int, relation: str, staged: str | None
    ) -> str:
        check = (model.config.reconciliation or [])[index]
        return reconciliation.measures_sql(check, relation, staged)

    def measure_difference(
        self, envelope: CallEnvelope, model_kind: str, source_kind: str
    ) -> MeasureDifference:
        inexact = tuple(
            kind for kind in (model_kind, source_kind) if not reconciliation.exact_difference(kind)
        )
        return MeasureDifference(reconciliation.common_type(model_kind, source_kind), inexact)

    def plan_run(
        self,
        envelope: CallEnvelope,
        models: tuple[Model, ...],
        graph: Graph,
        outputs: dict[str, Columns],
        existing: dict[str, Columns | None],
        since: dict[str, str],
        full_refresh: tuple[str, ...],
        previous: dict[str, str | None],
        differences: dict[str, tuple[Comparison, ...]],
        filtered: dict[str, str],
        databases: tuple[str, ...],
    ) -> tuple[tuple[Model, tuple[PlannedStep, ...]], ...]:
        planned = model_graph.plan_run(
            models,
            graph,
            outputs,
            existing,
            since,
            frozenset(full_refresh),
            previous,
            differences,
            filtered,
            frozenset(databases),
        )
        return tuple((model, tuple(steps)) for model, steps in planned)

    def source_attachments(
        self, envelope: CallEnvelope, connections: dict[str, Connection]
    ) -> tuple[Attachment, ...]:
        return tuple(source_connections.source_attachments(connections))

    def watermark_request(self, envelope: CallEnvelope, snapshot: int, model: Model) -> ReadRequest:
        return incremental.watermark_request(snapshot, model)

    def table_watermark_request(
        self, envelope: CallEnvelope, snapshot: int, model: Model, kind: str
    ) -> ReadRequest:
        return incremental.table_watermark_request(snapshot, model, kind)

    def since_value(
        self, envelope: CallEnvelope, kind: str, watermark: str | None, lookback: str | None
    ) -> str:
        return incremental.since_value(kind, watermark, lookback)

    def watermark_statements(
        self, envelope: CallEnvelope, step: Watermark, value: str | None
    ) -> tuple[StatementRequest, ...]:
        return tuple(incremental.watermark_statements(step, value))

    def leftovers(
        self, envelope: CallEnvelope, tables: Tables, models: tuple[Model, ...]
    ) -> Tables:
        return tuple(leftover_tables.leftovers(tables, models))

    def leftover_statements(
        self, envelope: CallEnvelope, leftovers: Tables
    ) -> tuple[StatementRequest, ...]:
        return tuple(leftover_tables.drop_statements(leftovers))

    def record_statements(
        self, envelope: CallEnvelope, records: tuple[RunRecord, ...]
    ) -> tuple[StatementRequest, ...]:
        return tuple(run_records.record_statements(records))

    def history_request(
        self, envelope: CallEnvelope, snapshot: int, time_zone: str, limit: int, tables: Tables
    ) -> ReadRequest | None:
        return run_records.history_request(snapshot, time_zone, limit, tables)

    def model_history_request(
        self, envelope: CallEnvelope, snapshot: int, tables: Tables
    ) -> ReadRequest | None:
        return run_records.model_history_request(snapshot, tables)

    def last_run_request(
        self, envelope: CallEnvelope, snapshot: int, tables: Tables
    ) -> ReadRequest | None:
        return run_records.last_run_request(snapshot, tables)

    def committed_request(self, envelope: CallEnvelope, snapshot: int, run_id: str) -> ReadRequest:
        return run_records.committed_request(snapshot, run_id)

    def schedule_plan(
        self,
        envelope: CallEnvelope,
        models: tuple[Model, ...],
        order: tuple[str, ...],
        dependencies: dict[str, tuple[str, ...]],
        histories: dict[str, History],
        now: dt.datetime,
        project_zone: str,
        started: dt.datetime,
        last_run_start: dt.datetime | None,
        held: dict[str, dt.datetime],
    ) -> Plan:
        return schedule.plan(
            models, order, dependencies, histories, now, project_zone, started, last_run_start, held
        )

    def next_due(
        self, envelope: CallEnvelope, model: Model, now: dt.datetime, project_zone: str
    ) -> dt.datetime:
        return schedule.next_due(model, now, project_zone)

    def closure(
        self,
        envelope: CallEnvelope,
        name: str,
        dependencies: dict[str, tuple[str, ...]],
        known: tuple[str, ...],
    ) -> tuple[str, ...]:
        return tuple(sorted(schedule.closure(name, dependencies, set(known))))

    def lineage_project_key(self, envelope: CallEnvelope, name: str) -> str:
        return lineage.project_key(name)

    def lineage_events(
        self,
        envelope: CallEnvelope,
        scope: LineageScope,
        parent_run_id: str,
        runs: tuple[LineageRun, ...],
        models: tuple[Model, ...],
        connections: dict[str, Connection],
        known: Known,
    ) -> tuple[str, ...] | ColumnsNeeded:
        lookup = _Lookup(known)
        by_name = _by_name(models)

        def built() -> tuple[str, ...]:
            found = []
            for run in runs:
                model = by_name[run.model]
                found.append(
                    lineage.ModelRun(
                        model=model,
                        run_id=run.run_id,
                        started_at=run.started_at,
                        finished_at=run.finished_at,
                        outcome=run.outcome,
                        output=run.output,
                        inputs=lineage.inputs(model, by_name, connections, scope.lake_namespace),
                        column_lineage=lineage.column_lineage(
                            model,
                            run.selected or run.output,
                            by_name,
                            connections,
                            scope.lake_namespace,
                            lambda path: _listed(lookup.columns(path)),
                        ),
                        rows_written=run.rows_written,
                        error=run.error,
                    )
                )
            events = lineage.events(scope, parent_run_id, found)
            return tuple(json.dumps(event, ensure_ascii=False) for event in events)

        return lookup.answer(built)
