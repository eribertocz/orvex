"""Run records (contract `run-record` v1, P1-10): how they are stored and read in the lake.

Records live in `_meta` (model folders cannot start with `_`). A successful run inserts its
records in its own transaction, so they commit with its data (#394); a run that rolls back is
recorded afterwards, in a transaction of their own. New records go to `_meta.run_records_v2`,
which has no `snapshot_id` column: a success record's snapshot is DuckLake's virtual column
`snapshot_id`, the snapshot that inserted the row, known only once it commits. The table is only
ever appended to, so maintenance never rewrites its files and the snapshot stays the run's.
`_meta.run_records`, the earlier layout, is read only: its stored `snapshot_id` stays the truth
for its rows. Every value travels as a bound parameter, never inside the SQL text.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence

from orvex.contracts.run_record.v1 import RunRecord
from orvex.contracts.store.v1 import Parameter, ParameterType, ReadRequest, StatementRequest

TABLE = "_meta.run_records_v2"
LEGACY_TABLE = "_meta.run_records"  # frozen (#394): read, never written

_LEGACY_COLUMNS: tuple[tuple[str, str], ...] = (
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
_COLUMNS = tuple(column for column in _LEGACY_COLUMNS if column[0] != "snapshot_id")


def _statement(sql: str, parameters: Sequence[Parameter] = ()) -> StatementRequest:
    return StatementRequest(kind="statement", sql=sql, parameters=list(parameters))


def _parameter(name: str, kind: str, value: str | None) -> Parameter:
    return Parameter(name=name, type=ParameterType(kind), value=value)


def record_statements(records: Sequence[RunRecord]) -> list[StatementRequest]:
    """Statements that create both record tables when missing (so both exist once any record
    does) and insert every record into `_meta.run_records_v2`. A record's `snapshot_id` is not
    stored: the readers resolve it from the row's own snapshot."""

    def ddl(columns: Sequence[tuple[str, str]]) -> str:
        return ", ".join(f"{name} {kind}" for name, kind in columns)

    statements = [
        _statement("CREATE SCHEMA IF NOT EXISTS _meta"),
        _statement(f"CREATE TABLE IF NOT EXISTS {LEGACY_TABLE} ({ddl(_LEGACY_COLUMNS)})"),
        _statement(f"CREATE TABLE IF NOT EXISTS {TABLE} ({ddl(_COLUMNS)})"),
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


def _records(tables: Collection[tuple[str, str]]) -> str | None:
    """`records`: every record of both tables, `snapshot_id` resolved per table: stored in the
    earlier layout; the row's own snapshot for a success in the new one, NULL otherwise. None
    when the lake has no record table. `tables` are the lake's (schema, table), lower-cased."""
    names = ", ".join(name for name, _ in _COLUMNS)
    selects = []
    if ("_meta", "run_records") in tables:
        selects.append(f"SELECT {names}, snapshot_id FROM {LEGACY_TABLE}")
    if ("_meta", "run_records_v2") in tables:
        selects.append(
            f"SELECT {names}, CASE WHEN outcome = 'success' THEN snapshot_id END AS snapshot_id "
            f"FROM {TABLE}"
        )
    return None if not selects else f"records AS ({' UNION ALL '.join(selects)})"


def history_request(
    snapshot: int, time_zone: str, limit: int, tables: Collection[tuple[str, str]]
) -> ReadRequest | None:
    """Recent runs, newest first: id, start in `time_zone`, outcome, snapshot and models. None
    when the lake has no record table (`tables`: its tables at `snapshot` or before)."""
    records = _records(tables)
    if records is None:
        return None
    return ReadRequest(
        kind="read",
        sql=(
            f"WITH {records} SELECT run_id, "
            "strftime(timezone($p_tz, min(started_at)), '%Y-%m-%d %H:%M:%S') AS started, "
            "CASE WHEN bool_and(outcome = 'success') THEN 'success' ELSE 'failed' END AS outcome, "
            "max(snapshot_id) AS snapshot, "
            "string_agg(model, ', ' ORDER BY model) AS models "
            "FROM records GROUP BY run_id "
            "ORDER BY min(started_at) DESC, run_id LIMIT $p_limit"
        ),
        parameters=[
            _parameter("p_tz", "VARCHAR", time_zone),
            _parameter("p_limit", "BIGINT", str(limit)),
        ],
        snapshot=snapshot,
        batch_size=1000,
    )


_ISO = "'%Y-%m-%dT%H:%M:%S.%f+00:00'"


def _iso(column: str) -> str:
    return f"strftime(timezone('UTC', {column}), {_ISO})"


def model_history_request(snapshot: int, tables: Collection[tuple[str, str]]) -> ReadRequest | None:
    """Per model: the start of the run of its last success (the whole run, upstream models
    included), the end of that success, and the start of its last successful full refresh
    (UTC, ISO 8601 text). None when the lake has no record table."""
    records = _records(tables)
    if records is None:
        return None
    return ReadRequest(
        kind="read",
        sql=(
            f"WITH {records}, ok AS (SELECT * FROM records WHERE outcome = 'success'), "
            "last AS (SELECT model, arg_max(run_id, finished_at) AS run_id, "
            "max(finished_at) AS finished, "
            "max(started_at) FILTER (WHERE trigger = 'full_refresh') AS refreshed "
            "FROM ok GROUP BY model), "
            "runs AS (SELECT run_id, min(started_at) AS run_start FROM records GROUP BY run_id) "
            f"SELECT last.model, {_iso('runs.run_start')} AS success_start, "
            f"{_iso('last.finished')} AS success_end, {_iso('last.refreshed')} AS full_refresh "
            "FROM last JOIN runs USING (run_id)"
        ),
        parameters=[],
        snapshot=snapshot,
        batch_size=1000,
    )


def last_run_request(snapshot: int, tables: Collection[tuple[str, str]]) -> ReadRequest | None:
    """The project's last run: its start (UTC, ISO 8601 text) and the models whose own step
    failed in it (comma-separated; empty when none did). None when the lake has no record
    table."""
    records = _records(tables)
    if records is None:
        return None
    return ReadRequest(
        kind="read",
        sql=(
            f"WITH {records}, latest AS (SELECT arg_max(run_id, started_at) AS run_id "
            "FROM records "
            "WHERE outcome <> 'not_run') "
            f"SELECT {_iso('min(started_at)')} AS started, "
            "string_agg(model, ',' ORDER BY model) FILTER (WHERE error_code IS NOT NULL "
            "AND outcome = 'failed') AS failed "
            "FROM records WHERE run_id = (SELECT run_id FROM latest) AND outcome <> 'not_run'"
        ),
        parameters=[],
        snapshot=snapshot,
        batch_size=1,
    )


def committed_request(snapshot: int, run_id: str) -> ReadRequest:
    """The snapshot that committed run `run_id`'s success records, or NULL when none did: how a
    run stopped after its block ran learns whether its commit happened (#394). Only for a lake
    that has `_meta.run_records_v2`."""
    return ReadRequest(
        kind="read",
        sql=(
            f"SELECT max(snapshot_id) AS snapshot FROM {TABLE} "
            "WHERE run_id = $p_run_id AND outcome = 'success'"
        ),
        parameters=[_parameter("p_run_id", "VARCHAR", run_id)],
        snapshot=snapshot,
        batch_size=1,
    )
