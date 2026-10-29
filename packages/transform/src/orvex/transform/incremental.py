"""Incremental models: checks and run steps for every strategy (P1-11, P1-12).

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

A `partition_overwrite` run filtered by `$since` (for example `> $since`) can load only the newer
rows of a partition, and the delete would then lose its older rows. When a loaded partition has
older (or undated) rows in the table and none among the loaded rows, the run fails with
`PARTITION_INCOMPLETE` (#109, decision #95). A filter that reloads only some older rows
(`>= $since`, a hand-written lookback) is not detected; whole-partition filters are the rule.

Which rows are loaded (P1-14): the model's SQL uses `$since`, bound as a typed parameter to the
previous watermark minus `lookback` (`-infinity` on a first run or a full refresh). The new
watermark is the largest `time_column` of the loaded rows (never less than the previous one),
computed from the staging table and stored in `_meta.watermarks` inside the run transaction, so
it commits or rolls back with the rows — never the clock, and never a separate write.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import duckdb
from orvex.contracts.model_config.v1 import ModelError
from orvex.contracts.store.v1 import Parameter, ParameterType, ReadRequest, StatementRequest
from orvex.transform.models import Model, problem

STAGE_SCHEMA = "_stage"
TEMPORAL_TYPES = frozenset({"DATE", "TIMESTAMP", "TIMESTAMP WITH TIME ZONE"})
SINCE = "since"
WATERMARKS = "_meta.watermarks"
LOWEST = "-infinity"
# The store's parameter type for each temporal output type.
PARAMETER_TYPES = {
    "DATE": "DATE",
    "TIMESTAMP": "TIMESTAMP",
    "TIMESTAMP WITH TIME ZONE": "TIMESTAMPTZ",
}
_DURATION = re.compile(r"([0-9]+)([smhd])")
_UNIT_MICROSECONDS = {"s": 1_000_000, "m": 60_000_000, "h": 3_600_000_000, "d": 86_400_000_000}
KEYED_STRATEGIES = frozenset({"merge", "delete_insert"})

# Type changes that keep every existing value exactly (sync_all_columns); nothing else is applied.
_SIGNED_INTEGERS = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT")
_UNSIGNED_INTEGERS = ("UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT")
_DECIMAL = re.compile(r"DECIMAL\((\d+),(\d+)\)")

type Columns = Sequence[tuple[str, str]]


@dataclass(frozen=True, slots=True)
class Check:
    """A value that must be zero; otherwise the run fails with `code`.

    With `detail`, the SQL returns `'0'` or a text describing what failed, which replaces
    `{detail}` in `message`. `parameters` are bound to the SQL (never formatted into it).
    """

    sql: str
    code: str
    message: str
    parameters: tuple[Parameter, ...] = ()
    detail: bool = False

    def failure(self, value: str) -> str:
        """The message for a failing value."""
        return self.message.replace("{detail}", value) if self.detail else self.message


@dataclass(frozen=True, slots=True)
class Count:
    """The number of loaded rows, recorded as rows read (and written)."""

    sql: str


@dataclass(frozen=True, slots=True)
class Refuse:
    """The model cannot run as declared; the run fails with `code` before writing anything."""

    code: str
    message: str


@dataclass(frozen=True, slots=True)
class Watermark:
    """The model's new watermark, as exact text: the larger of the previous one and the loaded
    rows' largest `time_column` (None when there is neither). The runner stores it with
    `watermark_statements`, in the same transaction."""

    request: StatementRequest
    model: str
    reset: bool  # a full refresh: the previous watermark is dropped even if nothing loads


type Step = StatementRequest | Check | Count | Refuse | Watermark


def _identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _statement(sql: str, parameters: Sequence[Parameter] = ()) -> StatementRequest:
    return StatementRequest(kind="statement", sql=sql, parameters=list(parameters))


def uses_since(sql: str) -> bool:
    """Whether the SQL references the `$since` parameter (DuckDB's parser; text in strings and
    comments does not count)."""
    try:
        statements = duckdb.extract_statements(sql)
    except duckdb.Error:
        return False
    # DuckDB binds `$SINCE` and `$Since` to the same parameter: the name is matched without case.
    return any(
        name.lower() == SINCE for statement in statements for name in statement.named_parameters
    )


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
    # Extraction models are ClickHouse SQL: their `{since:Type}` is checked by
    # `clickhouse.since_problems`, never by DuckDB's parser.
    if not automatic and not model.extraction and not uses_since(model.sql):
        found.append(
            problem(
                "MISSING_SINCE_PARAMETER",
                f"{model.sql_file}: filter: manual needs $since in the SQL, where the "
                "incremental filter belongs (for example WHERE <time_column> > $since).",
                model.sql_file,
                model=model.name,
            )
        )
    elif automatic and model.extraction:
        found.append(
            problem(
                "INCREMENTAL_FILTER_NOT_PROVABLE",
                f"{file}: the engine does not read ClickHouse SQL, so it cannot place the "
                "filter (reason: extraction); set filter: manual and use {since:Type}.",
                file,
                model=model.name,
                path="/incremental/filter",
            )
        )
    elif automatic and not model.extraction and uses_since(model.sql):
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


def lookback_microseconds(lookback: str | None) -> int:
    match = _DURATION.fullmatch(lookback or "0s")
    assert match is not None  # the contract's Duration pattern
    return int(match.group(1)) * _UNIT_MICROSECONDS[match.group(2)]


def since_value(kind: str, watermark: str | None, lookback: str | None) -> str:
    """`$since` for a time column of DuckDB type `kind`: the previous watermark minus `lookback`
    (whole days for DATE, rounded down), or `-infinity` when there is no watermark.

    Integer arithmetic on dates and microseconds: exact, and no clock is read. A watermark this
    code cannot read (a year outside 1-9999) is returned unchanged; the store then refuses it as
    a parameter, so the run fails instead of loading the wrong rows.
    """
    if watermark is None:
        return LOWEST
    if watermark in {"infinity", "-infinity"}:
        return watermark
    back = lookback_microseconds(lookback)
    try:
        if kind == "DATE":
            day = dt.date.fromisoformat(watermark)
            return (day - dt.timedelta(days=back // _UNIT_MICROSECONDS["d"])).isoformat()
        moment = dt.datetime.fromisoformat(watermark) - dt.timedelta(microseconds=back)
    except OverflowError:
        return LOWEST  # before year 1: every row is newer
    except ValueError:
        return watermark
    if kind == "TIMESTAMP WITH TIME ZONE":
        if moment.tzinfo is None:
            return watermark  # no offset: never read in the machine's zone; the store refuses it
        moment = moment.astimezone(dt.UTC)
    return moment.isoformat(timespec="microseconds")


def _as_text(value: str, kind: str) -> str:
    """A temporal value as the store's parameter text (ISO 8601; TIMESTAMPTZ in UTC with
    `+00:00`; `infinity` and `-infinity` as such)."""
    if kind == "DATE":
        text = f"strftime({value}, '%Y-%m-%d')"
    elif kind == "TIMESTAMP":
        text = f"strftime({value}, '%Y-%m-%dT%H:%M:%S.%f')"
    else:
        text = f"strftime(timezone('UTC', {value}), '%Y-%m-%dT%H:%M:%S.%f') || '+00:00'"
    return f"CASE WHEN isinf({value}) THEN CAST({value} AS VARCHAR) ELSE {text} END"


def _watermark(model: Model, column: str, kind: str, stage: str, previous: str | None) -> Watermark:
    loaded = f"SELECT max({column}) AS m FROM {stage}"
    if previous is None:
        sql, parameters = f"SELECT {_as_text('m', kind)} FROM ({loaded})", []
    else:
        # Re-read rows (lookback) may all be older than the previous watermark: never go back.
        sql = (
            f"SELECT {_as_text('w', kind)} FROM (SELECT CASE WHEN m IS NULL OR m < $p_previous "
            f"THEN $p_previous ELSE m END AS w FROM ({loaded}))"
        )
        parameters = [
            Parameter(name="p_previous", type=ParameterType(PARAMETER_TYPES[kind]), value=previous)
        ]
    return Watermark(_statement(sql, parameters), model.name, previous is None)


def watermark_statements(step: Watermark, value: str | None) -> list[StatementRequest]:
    """Statements that store a model's new watermark (run in the run transaction)."""
    if value is None and not step.reset:
        return []
    model = Parameter(name="p_model", type=ParameterType("VARCHAR"), value=step.model)
    statements = [
        _statement("CREATE SCHEMA IF NOT EXISTS _meta"),
        _statement(f"CREATE TABLE IF NOT EXISTS {WATERMARKS} (model VARCHAR, watermark VARCHAR)"),
        _statement(f"DELETE FROM {WATERMARKS} WHERE model = $p_model", [model]),
    ]
    if value is not None:
        text = Parameter(name="p_watermark", type=ParameterType("VARCHAR"), value=value)
        statements.append(
            _statement(f"INSERT INTO {WATERMARKS} VALUES ($p_model, $p_watermark)", [model, text])
        )
    return statements


def watermark_request(snapshot: int, model: Model) -> ReadRequest:
    """The model's stored watermark (no row: none stored)."""
    return ReadRequest(
        kind="read",
        sql=f"SELECT watermark FROM {WATERMARKS} WHERE model = $p_model",
        parameters=[Parameter(name="p_model", type=ParameterType("VARCHAR"), value=model.name)],
        snapshot=snapshot,
        batch_size=1,
    )


def table_watermark_request(snapshot: int, model: Model, kind: str) -> ReadRequest:
    """The largest `time_column` already in the model's table: the watermark of a table written
    before watermarks were stored (so it is never reloaded from `-infinity`)."""
    incremental = model.config.incremental
    assert incremental is not None
    column = _identifier(incremental.time_column.root)
    return ReadRequest(
        kind="read",
        sql=(
            f"SELECT {_as_text('m', kind)} AS watermark FROM "
            f"(SELECT max({column}) AS m FROM {_target(model)})"
        ),
        parameters=[],
        snapshot=snapshot,
        batch_size=1,
    )


def widens(old: str, new: str) -> bool:
    """True when every value of type `old` casts to `new` exactly (and back)."""
    for family in (_SIGNED_INTEGERS, _UNSIGNED_INTEGERS):
        if old in family and new in family:
            return family.index(new) > family.index(old)
    old_decimal, new_decimal = _DECIMAL.fullmatch(old), _DECIMAL.fullmatch(new)
    if old_decimal and new_decimal:
        old_precision, old_scale = map(int, old_decimal.groups())
        new_precision, new_scale = map(int, new_decimal.groups())
        return new_scale == old_scale and new_precision > old_precision
    return old == "DATE" and new == "TIMESTAMP"


def schema_statements(model: Model, output: Columns, existing: Columns) -> list[str] | str:
    """The `ALTER TABLE` statements that make the target take the output's columns under the
    model's `on_schema_change`, or why it cannot (the run then fails with SCHEMA_CHANGED)."""
    changed = schema_change(model, output, existing)
    if changed is None:
        return []
    policy = model.config.on_schema_change or "fail"
    if policy == "fail":
        return changed
    target = _target(model)
    old = {name.lower(): (name, kind) for name, kind in existing}
    new = {name.lower(): (name, kind) for name, kind in output}
    added = [new[key] for key in new if key not in old]
    removed = [old[key] for key in old if key not in new]
    retyped = [(old[key], new[key][1]) for key in new if key in old and old[key][1] != new[key][1]]
    if policy == "append_new_columns" and (removed or retyped):
        return changed + "; append_new_columns only adds columns"
    refused = [f"{name} {kind} → {to}" for (name, kind), to in retyped if not widens(kind, to)]
    if refused:
        return "cannot change " + ", ".join(refused) + " without changing values"
    # Added first, dropped last: the table never has zero columns.
    return (
        [f"ALTER TABLE {target} ADD COLUMN {_identifier(name)} {kind}" for name, kind in added]
        + [
            f"ALTER TABLE {target} ALTER COLUMN {_identifier(name)} SET DATA TYPE {to}"
            for (name, _), to in retyped
        ]
        + [f"ALTER TABLE {target} DROP COLUMN {_identifier(name)}" for name, _ in removed]
    )


def _target(model: Model) -> str:
    return f"{_identifier(model.schema)}.{_identifier(model.table)}"


def steps(
    model: Model,
    sql: str,
    output: Columns,
    existing: Columns | None,
    since: str = LOWEST,
    full_refresh: bool = False,
    previous: str | None = None,
) -> list[Step]:
    """The run steps of one incremental model.

    `existing`: its table's columns (None if absent); `since`: the `$since` value (text of the
    time column's type); `full_refresh`: replace the table with every loaded row; `previous`:
    the stored watermark (ignored on a full refresh).
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
    target = _target(model)
    stage_table = f"{model.schema}__{model.table}"
    stage = f"{STAGE_SCHEMA}.{_identifier(stage_table)}"
    parameters = (
        [Parameter(name=SINCE, type=ParameterType(PARAMETER_TYPES[kind]), value=since)]
        if uses_since(sql)
        else []
    )
    result: list[Step] = [
        _statement(f"CREATE SCHEMA IF NOT EXISTS {STAGE_SCHEMA}"),
        _statement(f"CREATE OR REPLACE TABLE {stage} AS {sql}", parameters),
        Count(f"SELECT count(*) FROM {stage}"),
        _watermark(model, _identifier(time_column), kind, stage, previous),
    ]
    keys = [_identifier(key.root) for key in incremental.unique_key or []]
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
            _statement(f"CREATE SCHEMA IF NOT EXISTS {_identifier(model.schema)}"),
            # A full refresh replaces the table; a first run creates it.
            _statement(f"CREATE OR REPLACE TABLE {target} AS SELECT * FROM {stage}"),
        ]
    else:
        result += [_statement(statement) for statement in alterations]
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
        if strategy == "partition_overwrite" and parameters and since != LOWEST:
            result.append(_cut_partitions(model, target, stage, output, parameters[0]))
        replaced = _replaced(model, target)
        if replaced is not None:
            result.append(
                _statement(
                    f"DELETE FROM {target} WHERE EXISTS "
                    f"(SELECT 1 FROM {stage} AS loaded WHERE {replaced})"
                )
            )
        result.append(_statement(f"INSERT INTO {target} BY NAME SELECT * FROM {stage}"))
    result += [
        _statement(f"DROP TABLE {stage}"),
        _statement(f"DROP SCHEMA IF EXISTS {STAGE_SCHEMA}"),
    ]
    return result


_CUT_SHOWN = 5  # partitions named in the PARTITION_INCOMPLETE message


_LABEL_LENGTH = 60  # characters of one partition value shown in the message
_TEXT_TYPES = frozenset({"VARCHAR"})
_QUOTE = "''''"  # a SQL string literal holding one single quote


def _label(alias: str, kind: str) -> str:
    """A partition value as message text: `NULL` for SQL NULL, text values quoted (so the text
    `NULL` reads `'NULL'`), at most `_LABEL_LENGTH` characters."""
    text = f"CAST({alias} AS VARCHAR)"
    if kind in _TEXT_TYPES:
        text = f"{_QUOTE} || replace({text}, {_QUOTE}, '''''') || {_QUOTE}"
    shown = (
        f"CASE WHEN length({text}) > {_LABEL_LENGTH} "
        f"THEN left({text}, {_LABEL_LENGTH}) || '…' ELSE {text} END"
    )
    return f"coalesce({shown}, 'NULL')"


def _cut_partitions(
    model: Model, target: str, stage: str, output: Columns, since: Parameter
) -> Check:
    """Fails when the `$since` filter cut a loaded partition (#109, decision #95).

    A loaded partition is cut when the table holds rows of it at or before `$since` while the
    loaded rows of that partition have none, or holds rows of it with a `NULL` time column
    while the loaded rows have none (a `$since` filter never reloads those). Partitions compare
    with `IS NOT DISTINCT FROM`, as the delete does, so `NULL` is a partition.

    This detects a filter that loads only rows newer than `$since`. It does not detect a filter
    that reloads some but not all older rows of a partition (`>= $since`, or a hand-written
    lookback), and it also fails when the source itself no longer has a partition's older rows
    (then `--full-refresh` reloads it).

    One grouped pass over the loaded rows and one over the table's rows of loaded partitions.
    Partition columns are renamed `p0`, `p1`, … inside the query, so no column name can collide
    with the query's own names. `$since` is bound with the time column's type. The result is
    `'0'`, or up to five cut partitions, ordered by value, and their total.
    """
    incremental = model.config.incremental
    assert incremental is not None
    assert incremental.partition_by
    types = {name.lower(): kind for name, kind in output}
    columns = [_identifier(column.root) for column in incremental.partition_by]
    aliases = [f"p{index}" for index in range(len(columns))]
    pairs = list(zip(columns, aliases, strict=True))
    renamed = ", ".join(f"{column} AS {alias}" for column, alias in pairs)
    time = _identifier(incremental.time_column.root)

    def matches(left: str, left_names: list[str]) -> str:
        return " AND ".join(
            f"{left}.{name} IS NOT DISTINCT FROM loaded.{alias}"
            for name, alias in zip(left_names, aliases, strict=True)
        )

    counted = (
        f"count(*) FILTER (WHERE {time} <= $since) AS n_older, "
        f"count(*) FILTER (WHERE {time} IS NULL) AS n_undated"
    )
    labels = [
        _label(f"loaded.{alias}", types.get(column.root.lower(), ""))
        for column, alias in zip(incremental.partition_by, aliases, strict=True)
    ]
    label = labels[0] if len(labels) == 1 else "'(' || " + " || ', ' || ".join(labels) + " || ')'"
    order = ", ".join(f"loaded.{alias} NULLS LAST" for alias in aliases)
    sql = (
        f"WITH loaded AS (SELECT {renamed}, {counted} FROM {stage} GROUP BY ALL), "
        f"held AS (SELECT {renamed}, {counted} FROM {target} AS row_held "
        f"WHERE EXISTS (SELECT 1 FROM loaded WHERE {matches('row_held', columns)}) "
        "GROUP BY ALL), "
        f"cut AS (SELECT {label} AS cut_label, row_number() OVER (ORDER BY {order}) AS cut_rank "
        f"FROM loaded JOIN held ON {matches('held', aliases)} "
        "WHERE (held.n_older > 0 AND loaded.n_older = 0) "
        "OR (held.n_undated > 0 AND loaded.n_undated = 0)) "
        "SELECT CASE WHEN count(*) = 0 THEN '0' ELSE "
        f"string_agg(cut_label, ', ' ORDER BY cut_rank) FILTER (WHERE cut_rank <= {_CUT_SHOWN}) "
        "|| ' (' || count(*) || ' in total)' END FROM cut"
    )
    name = incremental.time_column.root
    partition_by = ", ".join(column.root for column in incremental.partition_by)
    return Check(
        sql,
        "PARTITION_INCOMPLETE",
        f"{model.name}: the $since filter loaded only part of these partitions ({partition_by}): "
        "{detail}. The table holds rows of them that this run did not reload (at or before "
        f"$since, or with a NULL {name}), and partition_overwrite would delete them; nothing is "
        "written. Load whole partitions, for example "
        f"WHERE {name} >= date_trunc('month', $since) for monthly partitions; if the source "
        "itself no longer has those rows, use --full-refresh.",
        (since,),
        detail=True,
    )


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
        f"{target}.{_identifier(column.root)} {match} loaded.{_identifier(column.root)}"
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


def problems_for(models: Sequence[Model], columns: Mapping[str, Columns]) -> list[ModelError]:
    """Every incremental rule for the models whose output columns are known."""
    found: list[ModelError] = []
    for model in models:
        if model.name in columns:
            found += column_problems(model, columns[model.name])
    return found
