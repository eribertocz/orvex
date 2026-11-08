"""Incremental models: the run steps for every strategy (P1-11, P1-12, P1-14).

Its checks, watermarks and schema changes live in the `incremental_*` modules; their public
names are importable from here as well.

The loaded rows (what the model's SELECT returns in this run) are first written to a staging
table inside the run's transaction; checks run on it; the target's columns are changed when the
model's `on_schema_change` allows it; then the target is created or written, and the staging
table is dropped — all in the same transaction, so a failure anywhere leaves the target (data and
columns) as it was.

Every strategy is "delete the target rows the loaded rows replace, insert the loaded rows":
- `append` deletes nothing;
- `merge` deletes the rows whose key is loaded (keys must be unique in the loaded rows), which
  equals updating every column of matched keys and inserting the rest, without relying on MERGE
  support in the lake format;
- `delete_insert` deletes the rows whose key is loaded (a key's rows are replaced as a set);
- `partition_overwrite` deletes the rows whose partition is loaded (`NULL` is a partition).
Rows are inserted by column name, so a target whose columns were added at the end still matches.

A `partition_overwrite` run filtered by `$since` can load only part of a partition, and the
delete would then lose the rest. When the table holds rows of a loaded partition older than its
oldest loaded row, or undated rows the run does not reload, the run fails with
`PARTITION_INCOMPLETE` (#155, decision #128 option B). Its `$since` filter must compare
`time_column` itself, as a lower bound, which validation checks (`since_is_a_lower_bound`).

Which rows are loaded (P1-14): the model's SQL uses `$since`, bound as a typed parameter to the
previous watermark minus `lookback` (`-infinity` on a first run or a full refresh). The new
watermark is the largest `time_column` of the loaded rows (never less than the previous one),
computed from the staging table and stored in `_meta.watermarks` inside the run transaction, so
it commits or rolls back with the rows — never the clock, and never a separate write.
"""

from __future__ import annotations

from orvex.contracts.store.v1 import Parameter, ParameterType
from orvex.transform.incremental_append import append_rows_problem
from orvex.transform.incremental_partitions import cut_partitions_check
from orvex.transform.incremental_rules import column_problems, config_problems, problems_for
from orvex.transform.incremental_schema import schema_change, schema_statements, widens
from orvex.transform.incremental_since import (
    MAX_DEPTH,
    ColumnsOf,
    since_bound_problem,
    since_is_a_lower_bound,
    sql_depth,
    tree_depth,
    uses_since,
)
from orvex.transform.incremental_types import (
    KEYED_STRATEGIES,
    LOWEST,
    PARAMETER_TYPES,
    SINCE,
    STAGE_SCHEMA,
    TEMPORAL_TYPES,
    WATERMARKS,
    Check,
    Columns,
    Count,
    Land,
    Refuse,
    Step,
    Watermark,
    quote_identifier,
    statement_request,
    target_table,
)
from orvex.transform.incremental_watermarks import (
    lookback_microseconds,
    since_value,
    table_watermark_request,
    watermark_request,
    watermark_statements,
    watermark_step,
)
from orvex.transform.models import Model

__all__ = [
    "KEYED_STRATEGIES",
    "LOWEST",
    "MAX_DEPTH",
    "PARAMETER_TYPES",
    "SINCE",
    "STAGE_SCHEMA",
    "TEMPORAL_TYPES",
    "WATERMARKS",
    "Check",
    "Columns",
    "ColumnsOf",
    "Count",
    "Land",
    "Refuse",
    "Step",
    "Watermark",
    "append_rows_problem",
    "column_problems",
    "config_problems",
    "lookback_microseconds",
    "problems_for",
    "schema_change",
    "schema_statements",
    "since_bound_problem",
    "since_is_a_lower_bound",
    "since_value",
    "sql_depth",
    "steps",
    "table_watermark_request",
    "tree_depth",
    "uses_since",
    "watermark_request",
    "watermark_statements",
    "widens",
]


def steps(
    model: Model,
    sql: str,
    output: Columns,
    existing: Columns | None,
    since: str = LOWEST,
    full_refresh: bool = False,
    previous: str | None = None,
    land: str | None = None,
) -> list[Step]:
    """The run steps of one incremental model.

    `existing`: its table's columns (None if absent); `since`: the `$since` value (text of the
    time column's type); `full_refresh`: replace the table with every loaded row; `previous`:
    the stored watermark (ignored on a full refresh); `land`: the database source the model
    reads, whose rows (`$since` bound there) are landed in the stage by its worker.
    """
    incremental = model.config.incremental
    assert incremental is not None  # only incremental models get here
    strategy = incremental.strategy
    policy = model.config.on_schema_change or "fail"
    time_column = incremental.time_column.root
    kind = {name.lower(): kind for name, kind in output}.get(time_column.lower(), "")
    if kind not in PARAMETER_TYPES:
        return [
            Refuse(
                "TIME_COLUMN_NOT_TEMPORAL",
                f"{model.name}: time_column {time_column} must be DATE, TIMESTAMP or "
                "TIMESTAMP WITH TIME ZONE.",
            )
        ]
    if full_refresh:
        existing, previous = None, None
    alterations: list[str] = []
    if existing is not None:
        planned = schema_statements(model, output, existing)
        if isinstance(planned, str):
            return [
                Refuse(
                    "SCHEMA_CHANGED",
                    f"{model.name}: the output no longer matches the table ({planned}); with "
                    f"on_schema_change: {policy} nothing is written.",
                )
            ]
        alterations = planned
    target = target_table(model)
    stage_table = f"{model.schema}__{model.table}"
    stage = f"{STAGE_SCHEMA}.{quote_identifier(stage_table)}"
    parameters = (
        [Parameter(name=SINCE, type=ParameterType(PARAMETER_TYPES[kind]), value=since)]
        if uses_since(sql)
        else []
    )
    result: list[Step] = [
        statement_request(f"CREATE SCHEMA IF NOT EXISTS {STAGE_SCHEMA}"),
        Land(land, sql, tuple(parameters), STAGE_SCHEMA, stage_table)
        if land is not None
        else statement_request(f"CREATE OR REPLACE TABLE {stage} AS {sql}", parameters),
        Count(f"SELECT count(*) FROM {stage}"),
        watermark_step(model, quote_identifier(time_column), kind, stage, previous),
    ]
    keys = [quote_identifier(key.root) for key in incremental.unique_key or []]
    listed = ", ".join(keys)
    if strategy in KEYED_STRATEGIES:
        any_null = " OR ".join(f"{key} IS NULL" for key in keys)
        result.append(
            Check(
                f"SELECT count(*) FROM {stage} WHERE {any_null}",
                "UNIQUE_KEY_NULL",
                f"{model.name}: loaded rows have a NULL in unique_key ({listed}).",
            )
        )
    if strategy == "merge":
        result.append(
            Check(
                f"SELECT count(*) FROM (SELECT {listed} FROM {stage} "
                f"GROUP BY {listed} HAVING count(*) > 1)",
                "UNIQUE_KEY_DUPLICATED",
                f"{model.name}: loaded rows repeat a unique_key ({listed}); the result would "
                "depend on row order.",
            )
        )
    if existing is None:
        result += [
            statement_request(f"CREATE SCHEMA IF NOT EXISTS {quote_identifier(model.schema)}"),
            # A full refresh replaces the table; a first run creates it.
            statement_request(f"CREATE OR REPLACE TABLE {target} AS SELECT * FROM {stage}"),
        ]
    else:
        result += [statement_request(statement) for statement in alterations]
        # The columns were compared at validation; compare again on the loaded rows, inside the
        # transaction, after any column change, so a source that changed since validation cannot
        # be written. Under `fail` the order must match too; otherwise columns are compared by
        # name, since added columns go to the end of the table (rows are inserted by name).
        result.append(
            Check(
                _same_columns_sql(stage_table, model, ordered=policy == "fail"),
                "SCHEMA_CHANGED",
                f"{model.name}: the loaded rows' columns no longer match the table; nothing is "
                "written.",
            )
        )
        # Only on an existing table (this branch): with no watermark yet `$since` is -infinity,
        # no row is older, but undated rows the filter never reloads are still protected.
        if strategy == "partition_overwrite" and parameters:
            result.append(cut_partitions_check(model, target, stage, output))
        replaced = _replaced(model, target)
        if replaced is not None:
            result.append(
                statement_request(
                    f"DELETE FROM {target} WHERE EXISTS "
                    f"(SELECT 1 FROM {stage} AS loaded WHERE {replaced})"
                )
            )
        result.append(statement_request(f"INSERT INTO {target} BY NAME SELECT * FROM {stage}"))
    result += [
        statement_request(f"DROP TABLE {stage}"),
        statement_request(f"DROP SCHEMA IF EXISTS {STAGE_SCHEMA}"),
    ]
    return result


def _replaced(model: Model, target: str) -> str | None:
    """How a target row matches a loaded row it is replaced by (None: nothing is replaced)."""
    incremental = model.config.incremental
    assert incremental is not None
    if incremental.strategy in KEYED_STRATEGIES:
        # Keys were checked to have no NULL part, so `=` is exact.
        columns, match = incremental.unique_key or [], "="
    elif incremental.strategy == "partition_overwrite":
        columns, match = incremental.partition_by or [], "IS NOT DISTINCT FROM"
    else:
        return None
    return " AND ".join(
        f"{target}.{quote_identifier(column.root)} {match} loaded.{quote_identifier(column.root)}"
        for column in columns
    )


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _columns_sql(schema: str, table: str, ordered: bool) -> str:
    order = "ordinal_position" if ordered else "lower(column_name)"
    return (
        "(SELECT string_agg(lower(column_name) || ' ' || data_type, ', ' "
        f"ORDER BY {order}) "
        "FROM information_schema.columns WHERE table_catalog = current_database() "
        f"AND table_schema = {_literal(schema)} AND table_name = {_literal(table)})"
    )


def _same_columns_sql(stage_table: str, model: Model, ordered: bool) -> str:
    """0 only when the stage and the target both exist with the same columns (in order, when
    `ordered`; otherwise by name)."""
    # Both sides must be found: two missing lookups would otherwise compare equal.
    return (
        f"SELECT CASE WHEN {_columns_sql(STAGE_SCHEMA, stage_table, ordered)} = "
        f"{_columns_sql(model.schema, model.table, ordered)} THEN 0 ELSE 1 END"
    )
