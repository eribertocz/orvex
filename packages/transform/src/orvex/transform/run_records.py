"""Run records (contract `run-record` v1, P1-10): how they are stored and read in the lake.

Records live in `_meta.run_records` (model folders cannot start with `_`). They are written in
their own transaction after a run commits or rolls back, so failed runs are recorded too. Every
value travels as a bound parameter, never inside the SQL text.
"""

from __future__ import annotations

from collections.abc import Sequence

from orvex.contracts.run_record.v1 import RunRecord
from orvex.contracts.store.v1 import Parameter, ParameterType, ReadRequest, StatementRequest

TABLE = "_meta.run_records"

_COLUMNS: tuple[tuple[str, str], ...] = (
    ("run_id", "VARCHAR"),
    ("model", "VARCHAR"),
    ("trigger", "VARCHAR"),
    ("materialized", "VARCHAR"),
    ("project_commit", "VARCHAR"),
    ("project_dirty", "BOOLEAN"),
    ("versions", "VARCHAR"),
    ("since", "VARCHAR"),
    ("rows_read", "HUGEINT"),
    ("rows_written", "HUGEINT"),
    ("watermark", "VARCHAR"),
    ("tests", "VARCHAR"),
    ("reconciliation", "VARCHAR"),
    ("snapshot_id", "BIGINT"),
    ("started_at", "TIMESTAMPTZ"),
    ("finished_at", "TIMESTAMPTZ"),
    ("outcome", "VARCHAR"),
    ("error_code", "VARCHAR"),
    ("error_message", "VARCHAR"),
)


def _statement(sql: str, parameters: Sequence[Parameter] = ()) -> StatementRequest:
    return StatementRequest(kind="statement", sql=sql, parameters=list(parameters))


def _parameter(name: str, kind: str, value: str | None) -> Parameter:
    return Parameter(name=name, type=ParameterType(kind), value=value)


def _text(value: object) -> str | None:
    return None if value is None else str(value)


def record_statements(records: Sequence[RunRecord]) -> list[StatementRequest]:
    """Statements that create the table when missing and insert every record."""
    columns = ", ".join(f"{name} {kind}" for name, kind in _COLUMNS)
    statements = [
        _statement("CREATE SCHEMA IF NOT EXISTS _meta"),
        _statement(f"CREATE TABLE IF NOT EXISTS {TABLE} ({columns})"),
    ]
    # Parameters are `$p_<column>`: a bare column name can be a SQL keyword (`trigger`), which
    # the store does not accept as a parameter name.
    placeholders = ", ".join(f"$p_{name}" for name, _ in _COLUMNS)
    for record in records:
        values: dict[str, str | None] = {
            "run_id": record.run_id,
            "model": record.model,
            "trigger": record.trigger,
            "materialized": record.materialized,
            "project_commit": record.project_commit,
            "project_dirty": None
            if record.project_dirty is None
            else ("true" if record.project_dirty else "false"),
            "versions": record.versions.model_dump_json(),
            "since": record.since,
            "rows_read": None if record.rows_read is None else record.rows_read.root,
            "rows_written": None if record.rows_written is None else record.rows_written.root,
            "watermark": record.watermark,
            "tests": "[" + ",".join(item.model_dump_json() for item in record.tests) + "]",
            "reconciliation": "["
            + ",".join(item.model_dump_json() for item in record.reconciliation)
            + "]",
            "snapshot_id": _text(record.snapshot_id),
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "outcome": record.outcome,
            "error_code": None if record.error is None else record.error.code,
            "error_message": None if record.error is None else record.error.message,
        }
        statements.append(
            _statement(
                f"INSERT INTO {TABLE} VALUES ({placeholders})",
                [_parameter(f"p_{name}", kind, values[name]) for name, kind in _COLUMNS],
            )
        )
    return statements


def history_request(snapshot: int, time_zone: str, limit: int) -> ReadRequest:
    """Recent runs, newest first: id, start in `time_zone`, outcome, snapshot and models."""
    return ReadRequest(
        kind="read",
        sql=(
            "SELECT run_id, "
            "strftime(timezone($p_tz, min(started_at)), '%Y-%m-%d %H:%M:%S') AS started, "
            "CASE WHEN bool_and(outcome = 'success') THEN 'success' ELSE 'failed' END AS outcome, "
            "max(snapshot_id) AS snapshot, "
            "string_agg(model, ', ' ORDER BY model) AS models "
            f"FROM {TABLE} GROUP BY run_id "
            "ORDER BY min(started_at) DESC, run_id LIMIT $p_limit"
        ),
        parameters=[
            _parameter("p_tz", "VARCHAR", time_zone),
            _parameter("p_limit", "BIGINT", str(limit)),
        ],
        snapshot=snapshot,
        batch_size=1000,
    )
