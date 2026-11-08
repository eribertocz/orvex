"""The validation rules of incremental models: from the sidecar, and from the output columns."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from orvex.contracts.model_config.v1 import ModelError
from orvex.transform.incremental_append import append_rows_problem
from orvex.transform.incremental_since import since_bound_problem, uses_since
from orvex.transform.incremental_types import KEYED_STRATEGIES, TEMPORAL_TYPES, Columns
from orvex.transform.incremental_watermarks import UNIT_MICROSECONDS, lookback_microseconds
from orvex.transform.models import Model, problem


def config_problems(model: Model) -> list[ModelError]:
    """Rules that need only the sidecar."""
    config = model.config
    incremental = config.incremental
    if config.materialized != "incremental" or incremental is None:
        return []
    file = model.config_file or model.sql_file
    found: list[ModelError] = []
    strategy = incremental.strategy
    if strategy in KEYED_STRATEGIES and not incremental.unique_key:
        found.append(
            problem(
                "MISSING_UNIQUE_KEY",
                f"{file}: strategy {strategy} needs unique_key.",
                file,
                model=model.name,
                path="/incremental",
            )
        )
    if strategy == "partition_overwrite" and not incremental.partition_by:
        found.append(
            problem(
                "MISSING_PARTITION_BY",
                f"{file}: strategy partition_overwrite needs partition_by.",
                file,
                model=model.name,
                path="/incremental",
            )
        )
    automatic = (incremental.filter or "auto") == "auto"
    if not automatic and not uses_since(model.sql):
        found.append(
            problem(
                "MISSING_SINCE_PARAMETER",
                f"{model.sql_file}: filter: manual needs $since in the SQL, where the "
                "incremental filter belongs (for example WHERE <time_column> > $since).",
                model.sql_file,
                model=model.name,
            )
        )
    elif (rows := append_rows_problem(model)) is not None:
        found.append(rows)
    elif (bounded := since_bound_problem(model)) is not None:
        found.append(bounded)
    elif automatic and uses_since(model.sql):
        found.append(
            problem(
                "MODEL_CONFIG_INVALID",
                f"{file}: the SQL already uses $since: with filter: auto the engine places the "
                "filter itself; set filter: manual to keep yours.",
                file,
                model=model.name,
                path="/incremental/filter",
            )
        )
    if strategy not in KEYED_STRATEGIES and lookback_microseconds(incremental.lookback) > 0:
        found.append(
            problem(
                "LOOKBACK_REQUIRES_UNIQUE_KEY",
                f"{file}: {strategy} with lookback would not replace the re-read rows by key; use "
                "merge or delete_insert with a unique_key, or lookback: 0s.",
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
    back = lookback_microseconds(incremental.lookback)
    day = UNIT_MICROSECONDS["d"]
    if kind == "DATE" and back % day:
        # `$since` on a DATE moves by whole days: anything else would be cut silently (#254).
        below, above = back // day, back // day + 1
        found.append(
            problem(
                "MODEL_CONFIG_INVALID",
                f"{file}: time_column {time_column} is a DATE, so lookback counts whole days; "
                f"{incremental.lookback} is not a whole number of days (use "
                f"{f'{below}d or ' if below else '0s or '}{above}d).",
                file,
                model=model.name,
                path="/incremental/lookback",
            )
        )
    for field, listed in (
        ("unique_key", incremental.unique_key),
        ("partition_by", incremental.partition_by),
    ):
        for index, column in enumerate(listed or []):
            if column.root.lower() not in types:
                found.append(
                    problem(
                        "MODEL_CONFIG_INVALID",
                        f"{file}: {field} column {column.root} is not an output column.",
                        file,
                        model=model.name,
                        path=f"/incremental/{field}/{index}",
                    )
                )
    return found


def problems_for(models: Sequence[Model], columns: Mapping[str, Columns]) -> list[ModelError]:
    """Every incremental rule for the models whose output columns are known."""
    found: list[ModelError] = []
    for model in models:
        if model.name in columns:
            found += column_problems(model, columns[model.name])
    return found
