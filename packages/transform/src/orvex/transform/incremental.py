"""Incremental models: checks and run steps for `append` and `merge` (P1-11).

The loaded rows (what the model's SELECT returns in this run) are first written to a staging
table inside the run's transaction; checks run on it; then the target is created, appended to or
merged, and the staging table is dropped — all in the same transaction, so a failure anywhere
leaves the target as it was. Merging is done as "delete the target rows whose key is loaded,
insert the loaded rows", which equals updating every column of matched keys and inserting the
rest, without relying on MERGE support in the lake format.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from orvex.contracts.model_config.v1 import ModelError
from orvex.contracts.store.v1 import StatementRequest
from orvex.transform.models import Model, problem

STAGE_SCHEMA = "_stage"
TEMPORAL_TYPES = frozenset({"DATE", "TIMESTAMP", "TIMESTAMP WITH TIME ZONE"})
RUNNABLE_STRATEGIES = frozenset({"append", "merge"})

type Columns = Sequence[tuple[str, str]]


@dataclass(frozen=True, slots=True)
class Check:
    """A count that must be zero; otherwise the run fails with `code`."""

    sql: str
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class Count:
    """The number of loaded rows, recorded as rows read (and written)."""

    sql: str


@dataclass(frozen=True, slots=True)
class Refuse:
    """The model cannot run as declared; the run fails with `code` before writing anything."""

    code: str
    message: str


type Step = StatementRequest | Check | Count | Refuse


def _identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _statement(sql: str) -> StatementRequest:
    return StatementRequest(kind="statement", sql=sql, parameters=[])


def config_problems(model: Model) -> list[ModelError]:
    """Rules that need only the sidecar."""
    config = model.config
    incremental = config.incremental
    if config.materialized != "incremental" or incremental is None:
        return []
    file = model.config_file or model.sql_file
    found: list[ModelError] = []
    strategy = incremental.strategy
    if strategy in {"merge", "delete_insert"} and not incremental.unique_key:
        found.append(
            problem(
                "MISSING_UNIQUE_KEY",
                f"{file}: strategy {strategy} needs unique_key.",
                file,
                model=model.name,
                path="/incremental",
            )
        )
    if strategy == "append" and (incremental.lookback or "0s") != "0s":
        found.append(
            problem(
                "LOOKBACK_REQUIRES_UNIQUE_KEY",
                f"{file}: append with lookback would load the re-read rows twice; use merge "
                "with a unique_key, or lookback: 0s.",
                file,
                model=model.name,
                path="/incremental/lookback",
            )
        )
    return found


def column_problems(model: Model, columns: Columns) -> list[ModelError]:
    """Rules that need the model's output columns (from the binder, nothing executed)."""
    incremental = model.config.incremental
    if model.config.materialized != "incremental" or incremental is None:
        return []
    file = model.config_file or model.sql_file
    types = {name.lower(): kind for name, kind in columns}
    found: list[ModelError] = []
    time_column = incremental.time_column.root
    kind = types.get(time_column.lower())
    if kind not in TEMPORAL_TYPES:
        what = "is not an output column" if kind is None else f"is {kind}"
        found.append(
            problem(
                "TIME_COLUMN_NOT_TEMPORAL",
                f"{file}: time_column {time_column} {what}; it must be DATE, TIMESTAMP or "
                "TIMESTAMP WITH TIME ZONE.",
                file,
                model=model.name,
                path="/incremental/time_column",
            )
        )
    for index, key in enumerate(incremental.unique_key or []):
        if key.root.lower() not in types:
            found.append(
                problem(
                    "MODEL_CONFIG_INVALID",
                    f"{file}: unique_key column {key.root} is not an output column.",
                    file,
                    model=model.name,
                    path=f"/incremental/unique_key/{index}",
                )
            )
    return found


def schema_change(model: Model, output: Columns, existing: Columns | None) -> str | None:
    """Why the output no longer matches the existing target, or None."""
    if existing is None:
        return None
    new = [(name.lower(), kind) for name, kind in output]
    old = [(name.lower(), kind) for name, kind in existing]
    if new == old:
        return None
    added = [name for name, _ in new if name not in dict(old)]
    removed = [name for name, _ in old if name not in dict(new)]
    retyped = [
        f"{name} {dict(old)[name]} → {kind}"
        for name, kind in new
        if name in dict(old) and dict(old)[name] != kind
    ]
    parts = []
    if added:
        parts.append("added " + ", ".join(added))
    if removed:
        parts.append("removed " + ", ".join(removed))
    if retyped:
        parts.append("changed " + ", ".join(retyped))
    return "; ".join(parts) or "the column order changed"


def steps(model: Model, sql: str, output: Columns, existing: Columns | None) -> list[Step]:
    """The run steps of one incremental model (`existing`: its table's columns, None if absent)."""
    incremental = model.config.incremental
    assert incremental is not None  # only incremental models get here
    strategy = incremental.strategy
    if strategy not in RUNNABLE_STRATEGIES:
        return [
            Refuse(
                "UNSUPPORTED_MODEL_KIND",
                f"{model.name}: strategy {strategy} runs from a later release (P1-12).",
            )
        ]
    changed = schema_change(model, output, existing)
    if changed is not None:
        return [
            Refuse(
                "SCHEMA_CHANGED",
                f"{model.name}: the output no longer matches the table ({changed}); with "
                "on_schema_change: fail nothing is written.",
            )
        ]
    target = f"{_identifier(model.schema)}.{_identifier(model.table)}"
    stage = f"{STAGE_SCHEMA}.{_identifier(f'{model.schema}__{model.table}')}"
    result: list[Step] = [
        _statement(f"CREATE SCHEMA IF NOT EXISTS {STAGE_SCHEMA}"),
        _statement(f"CREATE OR REPLACE TABLE {stage} AS {sql}"),
        Count(f"SELECT count(*) FROM {stage}"),
    ]
    keys = [_identifier(key.root) for key in incremental.unique_key or []]
    if strategy == "merge":
        any_null = " OR ".join(f"{key} IS NULL" for key in keys)
        listed = ", ".join(keys)
        result += [
            Check(
                f"SELECT count(*) FROM {stage} WHERE {any_null}",
                "UNIQUE_KEY_NULL",
                f"{model.name}: loaded rows have a NULL in unique_key ({listed}).",
            ),
            Check(
                f"SELECT count(*) FROM (SELECT {listed} FROM {stage} "
                f"GROUP BY {listed} HAVING count(*) > 1)",
                "UNIQUE_KEY_DUPLICATED",
                f"{model.name}: loaded rows repeat a unique_key ({listed}); the result would "
                "depend on row order.",
            ),
        ]
    if existing is not None:
        # The columns were compared at validation; compare again on the loaded rows, inside the
        # transaction, so a source that changed since then cannot be inserted by position.
        result.append(
            Check(
                _same_columns_sql(STAGE_SCHEMA, f"{model.schema}__{model.table}", model),
                "SCHEMA_CHANGED",
                f"{model.name}: the loaded rows' columns no longer match the table; nothing is "
                "written.",
            )
        )
    if existing is None:
        result += [
            _statement(f"CREATE SCHEMA IF NOT EXISTS {_identifier(model.schema)}"),
            _statement(f"CREATE TABLE {target} AS SELECT * FROM {stage}"),
        ]
    elif strategy == "append":
        result.append(_statement(f"INSERT INTO {target} SELECT * FROM {stage}"))
    else:
        matched = " AND ".join(f"{target}.{key} = loaded.{key}" for key in keys)
        result += [
            _statement(
                f"DELETE FROM {target} WHERE EXISTS "
                f"(SELECT 1 FROM {stage} AS loaded WHERE {matched})"
            ),
            _statement(f"INSERT INTO {target} SELECT * FROM {stage}"),
        ]
    result += [
        _statement(f"DROP TABLE {stage}"),
        _statement(f"DROP SCHEMA IF EXISTS {STAGE_SCHEMA}"),
    ]
    return result


def _literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _columns_sql(schema: str, table: str) -> str:
    return (
        "(SELECT string_agg(column_name || ' ' || data_type, ', ' ORDER BY ordinal_position) "
        "FROM information_schema.columns WHERE table_catalog = current_database() "
        f"AND table_schema = {_literal(schema)} AND table_name = {_literal(table)})"
    )


def _same_columns_sql(stage_schema: str, stage_table: str, model: Model) -> str:
    """0 only when the stage and the target both exist with the same columns, in order."""
    # Both sides must be found: two missing lookups would otherwise compare equal.
    return (
        f"SELECT CASE WHEN {_columns_sql(stage_schema, stage_table)} = "
        f"{_columns_sql(model.schema, model.table)} THEN 0 ELSE 1 END"
    )


def problems_for(models: Sequence[Model], columns: Mapping[str, Columns]) -> list[ModelError]:
    """Every incremental rule for the models whose output columns are known."""
    found: list[ModelError] = []
    for model in models:
        if model.name in columns:
            found += column_problems(model, columns[model.name])
    return found
