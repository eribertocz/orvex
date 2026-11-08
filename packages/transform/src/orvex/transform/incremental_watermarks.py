"""Watermarks of incremental models: the `$since` value, the new watermark, its storage (P1-14)."""

from __future__ import annotations

import datetime as dt
import re

from orvex.contracts.store.v1 import Parameter, ParameterType, ReadRequest, StatementRequest
from orvex.transform.incremental_types import (
    LOWEST,
    PARAMETER_TYPES,
    WATERMARKS,
    Watermark,
    quote_identifier,
    statement_request,
    target_table,
)
from orvex.transform.models import Model

_DURATION = re.compile(r"([0-9]+)([smhd])")
UNIT_MICROSECONDS = {"s": 1_000_000, "m": 60_000_000, "h": 3_600_000_000, "d": 86_400_000_000}


def lookback_microseconds(lookback: str | None) -> int:
    match = _DURATION.fullmatch(lookback or "0s")
    assert match is not None  # the contract's Duration pattern
    return int(match.group(1)) * UNIT_MICROSECONDS[match.group(2)]


def since_value(kind: str, watermark: str | None, lookback: str | None) -> str:
    """`$since` for a time column of DuckDB type `kind`: the previous watermark minus `lookback`
    (whole days for DATE: validation refuses any other lookback there, #254), or `-infinity` when
    there is no watermark.

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
            return (day - dt.timedelta(days=back // UNIT_MICROSECONDS["d"])).isoformat()
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


def watermark_step(
    model: Model, column: str, kind: str, stage: str, previous: str | None
) -> Watermark:
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
    return Watermark(statement_request(sql, parameters), model.name, previous is None)


def watermark_statements(step: Watermark, value: str | None) -> list[StatementRequest]:
    """Statements that store a model's new watermark (run in the run transaction)."""
    if value is None and not step.reset:
        return []
    model = Parameter(name="p_model", type=ParameterType("VARCHAR"), value=step.model)
    statements = [
        statement_request("CREATE SCHEMA IF NOT EXISTS _meta"),
        statement_request(
            f"CREATE TABLE IF NOT EXISTS {WATERMARKS} (model VARCHAR, watermark VARCHAR)"
        ),
        statement_request(f"DELETE FROM {WATERMARKS} WHERE model = $p_model", [model]),
    ]
    if value is not None:
        text = Parameter(name="p_watermark", type=ParameterType("VARCHAR"), value=value)
        statements.append(
            statement_request(
                f"INSERT INTO {WATERMARKS} VALUES ($p_model, $p_watermark)", [model, text]
            )
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
    column = quote_identifier(incremental.time_column.root)
    return ReadRequest(
        kind="read",
        sql=(
            f"SELECT {_as_text('m', kind)} AS watermark FROM "
            f"(SELECT max({column}) AS m FROM {target_table(model)})"
        ),
        parameters=[],
        snapshot=snapshot,
        batch_size=1,
    )
