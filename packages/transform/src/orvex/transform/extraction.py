"""Running extraction models (P1-17, #98): ClickHouse rows into the lake, never through Python.

An extraction model's run starts with one `Extract` step. The runner opens the model's
ClickHouse connection, checks that the output columns are still those validated, binds
`{since:…}` server-side, and streams the result as Arrow record batches into a table of the run's
DuckDB transaction (`Transaction.load_arrow`), each column cast to its mapped exact type. From
that table on, the model runs like any other: a `Check` refuses text that was not UTF-8, then the
`table` materialization or the incremental strategy reads the extracted rows, and the extracted
table is dropped in the same transaction.
"""

from __future__ import annotations

from dataclasses import dataclass

from orvex.contracts.store.v1 import StatementRequest
from orvex.transform import incremental
from orvex.transform.clickhouse import Column, SinceType, declared_since, helper_names, scan
from orvex.transform.incremental import Check, Columns, Step
from orvex.transform.models import Model

SCHEMA = "_extract"


@dataclass(frozen=True, slots=True)
class Extract:
    """Load an extraction model's rows from ClickHouse into `schema.table`.

    `since` is the declared `{since:…}` type (None: the model binds no parameter);
    `watermark` the previous watermark as stored (None: a first run or a full refresh, which
    bind the type's lowest value); `lookback` is subtracted from it.
    """

    model: str
    connection: str
    text: str
    columns: tuple[Column, ...]
    since: SinceType | None
    watermark: str | None
    lookback: str | None
    schema: str
    table: str


def _identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _statement(sql: str) -> StatementRequest:
    return StatementRequest(kind="statement", sql=sql, parameters=[])


def steps(
    model: Model,
    columns: tuple[Column, ...],
    output: Columns,
    existing: Columns | None,
    since: str,
    full_refresh: bool,
    previous: str | None,
) -> list[Step | Extract]:
    """The run steps of one extraction model, before its tests.

    `columns`: the validated ClickHouse output; `output`: the same as DuckDB types; for an
    incremental model, `existing`, `since`, `full_refresh` and `previous` mean what they mean
    for `incremental.steps`.
    """
    assert model.config.connection is not None
    incremental_config = model.config.incremental
    table = f"{model.schema}__{model.table}"
    extracted = f"{_identifier(SCHEMA)}.{_identifier(table)}"
    row, invalid = helper_names(columns)
    watermark = None if full_refresh else previous
    extract = Extract(
        model=model.name,
        connection=model.config.connection,
        text=scan(model.sql).text,
        columns=columns,
        since=declared_since(model),
        watermark=watermark,
        lookback=incremental_config.lookback if incremental_config is not None else None,
        schema=SCHEMA,
        table=table,
    )
    result: list[Step | Extract] = [
        extract,
        Check(
            f"SELECT CASE WHEN count(*) = 0 THEN '0' ELSE "
            f"arg_min({_identifier(invalid)}, {_identifier(row)}) || ' (row ' || "
            f"(min({_identifier(row)}) + 1) || ' of the stream)' END "
            f"FROM {extracted} WHERE {_identifier(invalid)} IS NOT NULL",
            "UNSUPPORTED_SOURCE_VALUE",
            f"{model.name}: ClickHouse sent text that is not valid UTF-8 in column {{detail}}; "
            "it is never stored altered or dropped. Fix the value at the source, or convert it "
            "in the SQL (for example base64Encode(column)).",
            detail=True,
        ),
    ]
    selected = ", ".join(_identifier(name) for name, _ in output)
    rows = f"SELECT {selected} FROM {extracted}"
    if model.config.materialized == "incremental":
        # Rows were selected by `{since:…}` on ClickHouse when a watermark was bound.
        filtered = extract.since is not None and watermark is not None
        result += incremental.steps(
            model, rows, output, existing, since, full_refresh, previous, filtered=filtered
        )
    else:
        schema, target = _identifier(model.schema), _identifier(model.table)
        result += [
            _statement(f"CREATE SCHEMA IF NOT EXISTS {schema}"),
            _statement(f"CREATE OR REPLACE TABLE {schema}.{target} AS {rows}"),
        ]
    result += [
        _statement(f"DROP TABLE {extracted}"),
        _statement(f"DROP SCHEMA IF EXISTS {_identifier(SCHEMA)}"),
    ]
    return result
