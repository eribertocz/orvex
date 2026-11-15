"""The transform interface contract (#486 step 3, ADR-0033): the operations its consumers call,
with their inputs, outputs and capabilities, at version 1.0.

Transform turns a project's models into checked plans and requests; it never opens the lake.
Every operation is a pure function of its arguments: the consumer reads the store, gives
transform what it read, and executes the requests transform answers with. No operation changes
state, so none carries an idempotency key; a model's problems are `ModelError` values of the
`model-config` contract, never raised. `TransformError` is the contract's error type: no
operation declares a code, so anything an implementation raises reaches a consumer only as
INTERFACE_ERROR_UNDECLARED (fail closed); the consumer's columns exchange raises it as
TRANSFORM_COLUMNS_REPEATED when an implementation breaks that exchange (`columns.py`).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Protocol

from orvex.contracts.envelope import CallEnvelope
from orvex.contracts.interface import InterfaceContract, Operation
from orvex.contracts.model_config.v1 import ModelError
from orvex.contracts.project.v1 import (
    CsvConnection,
    DuckdbConnection,
    ParquetConnection,
    PostgresConnection,
    SqliteConnection,
)
from orvex.contracts.run_record.v1 import RunRecord
from orvex.contracts.store.v1 import (
    FileSourceAttachment,
    PostgresSourceAttachment,
    ReadRequest,
    SqliteSourceAttachment,
    StatementRequest,
)
from orvex.contracts.transform.analysis import (
    ColumnsNeeded,
    LineageRun,
    LineageScope,
    MeasureDifference,
    ReconciliationStage,
    Refusal,
    RelationColumns,
)
from orvex.contracts.transform.models import Columns, Discovery, Graph, Model
from orvex.contracts.transform.scheduling import History, Plan
from orvex.contracts.transform.steps import Comparison, ModelTest, PlannedStep, Watermark
from orvex.contracts.version import Version

Connection = (
    ParquetConnection | CsvConnection | DuckdbConnection | SqliteConnection | PostgresConnection
)
Attachment = FileSourceAttachment | SqliteSourceAttachment | PostgresSourceAttachment
Tables = tuple[tuple[str, str], ...]
Known = tuple[RelationColumns, ...]


class TransformError(Exception):
    """The transform contract's error type: a stable code, a message and details."""

    def __init__(self, code: str, message: str, details: Mapping[str, str] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = dict(details or {})


class Transform(Protocol):
    # models: discovery, the dependency graph and every problem of a model.
    def discover_models(
        self, envelope: CallEnvelope, folder: str, connections: dict[str, Connection]
    ) -> Discovery: ...

    def build_graph(
        self,
        envelope: CallEnvelope,
        models: tuple[Model, ...],
        sources: tuple[str, ...],
        databases: tuple[str, ...],
        direct: tuple[str, ...],
    ) -> Graph: ...

    def known_output_names(self, envelope: CallEnvelope, sql: str) -> tuple[str, ...] | None: ...

    def config_problems(
        self,
        envelope: CallEnvelope,
        model: Model,
        project_zone: str,
        connection_types: dict[str, str],
    ) -> tuple[ModelError, ...]: ...

    def column_problems(
        self, envelope: CallEnvelope, models: tuple[Model, ...], columns: dict[str, Columns]
    ) -> tuple[ModelError, ...]: ...

    def test_problems(
        self,
        envelope: CallEnvelope,
        model: Model,
        columns: dict[str, Columns],
        models: tuple[Model, ...],
    ) -> tuple[ModelError, ...]: ...

    def since_bound_problem(
        self, envelope: CallEnvelope, model: Model, known: Known
    ) -> ModelError | ColumnsNeeded | None: ...

    def uses_since(self, envelope: CallEnvelope, sql: str) -> bool: ...

    def filtered_sql(
        self, envelope: CallEnvelope, sql: str, time_column: str, known: Known
    ) -> str | Refusal | ColumnsNeeded: ...

    # compile: the SQL a model runs, its tests and its reconciliation checks.
    def compiled_sql(
        self, envelope: CallEnvelope, model: Model, models: tuple[Model, ...], sql: str | None
    ) -> str: ...

    def test_relations(
        self, envelope: CallEnvelope, models: tuple[Model, ...]
    ) -> dict[str, str]: ...

    def test_steps(
        self,
        envelope: CallEnvelope,
        model: Model,
        relation: str,
        columns: Columns,
        relations: dict[str, str],
    ) -> tuple[ModelTest, ...]: ...

    def reconciliation_stage(
        self, envelope: CallEnvelope, model: Model, index: int, databases: tuple[str, ...]
    ) -> ReconciliationStage | None: ...

    def measures_sql(
        self, envelope: CallEnvelope, model: Model, index: int, relation: str, staged: str | None
    ) -> str: ...

    def measure_difference(
        self, envelope: CallEnvelope, model_kind: str, source_kind: str
    ) -> MeasureDifference: ...

    # run: the run plan, its source attachments, watermarks and leftover tables.
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
    ) -> tuple[tuple[Model, tuple[PlannedStep, ...]], ...]: ...

    def source_attachments(
        self, envelope: CallEnvelope, connections: dict[str, Connection]
    ) -> tuple[Attachment, ...]: ...

    def watermark_request(
        self, envelope: CallEnvelope, snapshot: int, model: Model
    ) -> ReadRequest: ...

    def table_watermark_request(
        self, envelope: CallEnvelope, snapshot: int, model: Model, kind: str
    ) -> ReadRequest: ...

    def since_value(
        self, envelope: CallEnvelope, kind: str, watermark: str | None, lookback: str | None
    ) -> str: ...

    def watermark_statements(
        self, envelope: CallEnvelope, step: Watermark, value: str | None
    ) -> tuple[StatementRequest, ...]: ...

    def leftovers(
        self, envelope: CallEnvelope, tables: Tables, models: tuple[Model, ...]
    ) -> Tables: ...

    def leftover_statements(
        self, envelope: CallEnvelope, leftovers: Tables
    ) -> tuple[StatementRequest, ...]: ...

    # records: the run records written with a run, and the requests that read them back.
    def record_statements(
        self, envelope: CallEnvelope, records: tuple[RunRecord, ...]
    ) -> tuple[StatementRequest, ...]: ...

    def history_request(
        self, envelope: CallEnvelope, snapshot: int, time_zone: str, limit: int, tables: Tables
    ) -> ReadRequest | None: ...

    def model_history_request(
        self, envelope: CallEnvelope, snapshot: int, tables: Tables
    ) -> ReadRequest | None: ...

    def last_run_request(
        self, envelope: CallEnvelope, snapshot: int, tables: Tables
    ) -> ReadRequest | None: ...

    def committed_request(
        self, envelope: CallEnvelope, snapshot: int, run_id: str
    ) -> ReadRequest: ...

    # schedule: what is due, when, and what a model's run reads upstream.
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
    ) -> Plan: ...

    def next_due(
        self, envelope: CallEnvelope, model: Model, now: dt.datetime, project_zone: str
    ) -> dt.datetime: ...

    def closure(
        self,
        envelope: CallEnvelope,
        name: str,
        dependencies: dict[str, tuple[str, ...]],
        known: tuple[str, ...],
    ) -> tuple[str, ...]: ...

    # lineage: a run's OpenLineage events.
    def lineage_project_key(self, envelope: CallEnvelope, name: str) -> str: ...

    def lineage_events(
        self,
        envelope: CallEnvelope,
        scope: LineageScope,
        parent_run_id: str,
        runs: tuple[LineageRun, ...],
        models: tuple[Model, ...],
        connections: dict[str, Connection],
        known: Known,
    ) -> tuple[str, ...] | ColumnsNeeded: ...


def _operations(capability: str, *names: str) -> tuple[Operation, ...]:
    return tuple(Operation(name, capability) for name in names)


TRANSFORM = InterfaceContract(
    name="transform",
    version=Version(1, 0),
    protocol=Transform,
    error_type=TransformError,
    capabilities=("models", "compile", "run", "records", "schedule", "lineage"),
    errors=(),
    operations=(
        *_operations(
            "models",
            "discover_models",
            "build_graph",
            "known_output_names",
            "config_problems",
            "column_problems",
            "test_problems",
            "since_bound_problem",
            "uses_since",
            "filtered_sql",
        ),
        *_operations(
            "compile",
            "compiled_sql",
            "test_relations",
            "test_steps",
            "reconciliation_stage",
            "measures_sql",
            "measure_difference",
        ),
        *_operations(
            "run",
            "plan_run",
            "source_attachments",
            "watermark_request",
            "table_watermark_request",
            "since_value",
            "watermark_statements",
            "leftovers",
            "leftover_statements",
        ),
        *_operations(
            "records",
            "record_statements",
            "history_request",
            "model_history_request",
            "last_run_request",
            "committed_request",
        ),
        *_operations("schedule", "schedule_plan", "next_due", "closure"),
        *_operations("lineage", "lineage_project_key", "lineage_events"),
    ),
)
