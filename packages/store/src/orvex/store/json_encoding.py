"""Exact JSON encoding of read results (contract `store` v1, `ResultJson`, P1-03).

Values that JSON numbers cannot carry exactly travel as strings: 64- and 128-bit integers and
decimals (with exactly their scale's fraction digits). Conversions are done by Arrow on whole
columns; decimals and wide integers never pass through a Python float.
"""

from __future__ import annotations

import base64
import decimal
import json
import math
from collections.abc import Callable, Iterator

import pyarrow as pa
import pyarrow.compute as pc
from orvex.store.errors import StoreError

type JsonValue = str | int | float | bool | None

SMALL_INTEGERS = frozenset({"TINYINT", "SMALLINT", "INTEGER", "UTINYINT", "USMALLINT", "UINTEGER"})
WIDE_INTEGERS = frozenset({"BIGINT", "UBIGINT", "HUGEINT", "UHUGEINT"})


def duckdb_types(schema: pa.Schema) -> list[str]:
    """The DuckDB type of each column, from the `duckdb_type` field metadata set by `read`."""
    found = []
    for field in schema:
        metadata = field.metadata or {}
        kind = metadata.get(b"duckdb_type")
        if kind is None:
            raise StoreError(
                "STORE_TYPE_UNSUPPORTED",
                "The column carries no DuckDB type; results must come from Lake.read.",
                {"column": field.name},
            )
        found.append(kind.decode())
    return found


def _strings(column: pa.Array) -> list[JsonValue]:
    texts: list[JsonValue] = pc.cast(column, pa.string()).to_pylist()
    return texts


def _floats(column: pa.Array) -> list[JsonValue]:
    values: list[JsonValue] = []
    for value in column.to_pylist():
        if value is None or math.isfinite(value):
            values.append(value)
        elif math.isnan(value):
            values.append("NaN")
        else:
            values.append("Infinity" if value > 0 else "-Infinity")
    return values


def _timestamps(column: pa.Array) -> list[JsonValue]:
    # Arrow writes `YYYY-MM-DD HH:MM:SS.ffffff` (with `Z` when zoned in UTC).
    texts: list[JsonValue] = pc.replace_substring(
        pc.cast(column, pa.string()), " ", "T", max_replacements=1
    ).to_pylist()
    return texts


def _intervals(column: pa.Array) -> list[JsonValue]:
    values: list[JsonValue] = []
    for value in column.to_pylist():
        if value is None:
            values.append(None)
            continue
        seconds = decimal.Decimal(value.nanoseconds).scaleb(-9).normalize()
        text = format(seconds, "f")
        values.append(f"P{value.months}M{value.days}DT{text}S")
    return values


def _blobs(column: pa.Array) -> list[JsonValue]:
    return [
        None if value is None else base64.b64encode(value).decode("ascii")
        for value in column.to_pylist()
    ]


def _plain(column: pa.Array) -> list[JsonValue]:
    values: list[JsonValue] = column.to_pylist()
    return values


def _converter(kind: str, column: str) -> Callable[[pa.Array], list[JsonValue]]:
    if kind in {"BOOLEAN", "VARCHAR", "UUID"} or kind in SMALL_INTEGERS:
        return _strings if kind == "UUID" else _plain
    if kind in WIDE_INTEGERS or kind.startswith("DECIMAL("):
        return _strings
    if kind in {"FLOAT", "DOUBLE"}:
        return _floats
    if kind == "DATE":
        return _strings
    if kind in {"TIMESTAMP", "TIMESTAMP WITH TIME ZONE"}:
        return _timestamps
    if kind == "INTERVAL":
        return _intervals
    if kind == "BLOB":
        return _blobs
    raise StoreError(
        "STORE_TYPE_UNSUPPORTED",
        f"Column {column} has type {kind}, which has no JSON encoding yet.",
        {"column": column, "type": kind},
    )


def arrow_to_json(reader: pa.RecordBatchReader) -> Iterator[str]:
    """The `ResultJson` document as text fragments, one batch of rows at a time.

    Unsupported types are reported before any row is produced.
    """
    schema = reader.schema
    kinds = duckdb_types(schema)
    converters = [_converter(kind, field.name) for kind, field in zip(kinds, schema, strict=True)]
    columns = [
        {"name": field.name, "type": kind} for field, kind in zip(schema, kinds, strict=True)
    ]
    yield '{"kind":"result_json","columns":' + json.dumps(columns, ensure_ascii=False)
    yield ',"rows":['
    first = True
    for batch in reader:
        encoded = [convert(batch.column(index)) for index, convert in enumerate(converters)]
        for row in zip(*encoded, strict=True):
            prefix = "" if first else ","
            first = False
            yield prefix + json.dumps(list(row), ensure_ascii=False, allow_nan=False)
    yield "]}"
