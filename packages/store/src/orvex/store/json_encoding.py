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


DATE_INFINITY = 2**31 - 1
TIMESTAMP_INFINITY = 2**63 - 1
MICROSECONDS_PER_DAY = 86_400_000_000


def _civil(days: int) -> str:
    """`YYYY-MM-DD` of a day count since 1970-01-01, for any year (proleptic Gregorian).

    Integer arithmetic only (H. Hinnant's civil-from-days); years before 1 use astronomical
    numbering with a sign (`-0043` is 44 BC), years above 9999 use more digits.
    """
    shifted = days + 719468
    era = shifted // 146097
    day_of_era = shifted - era * 146097
    year_of_era = (
        day_of_era - day_of_era // 1460 + day_of_era // 36524 - day_of_era // 146096
    ) // 365
    year = year_of_era + era * 400
    day_of_year = day_of_era - (365 * year_of_era + year_of_era // 4 - year_of_era // 100)
    shifted_month = (5 * day_of_year + 2) // 153
    day = day_of_year - (153 * shifted_month + 2) // 5 + 1
    month = shifted_month + 3 if shifted_month < 10 else shifted_month - 9
    year += 1 if month <= 2 else 0
    sign = "-" if year < 0 else ""
    return f"{sign}{abs(year):04d}-{month:02d}-{day:02d}"


def _dates(column: pa.Array) -> list[JsonValue]:
    values: list[JsonValue] = []
    for days in pc.cast(column, pa.int32()).to_pylist():
        if days is None:
            values.append(None)
        elif days == DATE_INFINITY:
            values.append("infinity")
        elif days == -DATE_INFINITY:
            values.append("-infinity")
        else:
            values.append(_civil(days))
    return values


def _timestamp_text(microseconds: int, zoned: bool) -> str:
    days, rest = divmod(microseconds, MICROSECONDS_PER_DAY)
    seconds, fraction = divmod(rest, 1_000_000)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    text = f"{_civil(days)}T{hours:02d}:{minutes:02d}:{seconds:02d}.{fraction:06d}"
    return text + "Z" if zoned else text


def _timestamps(column: pa.Array) -> list[JsonValue]:
    zoned = column.type.tz is not None
    if column.type.unit != "us":
        raise StoreError(
            "STORE_TYPE_UNSUPPORTED",
            "Only microsecond timestamps have a JSON encoding.",
            {"type": str(column.type)},
        )
    values: list[JsonValue] = []
    for raw in column.cast(pa.int64()).to_pylist():
        if raw is None:
            values.append(None)
        elif raw == TIMESTAMP_INFINITY:
            values.append("infinity")
        elif raw == -TIMESTAMP_INFINITY:
            values.append("-infinity")
        else:
            values.append(_timestamp_text(raw, zoned))
    return values


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


def _decimals(column: pa.Array) -> list[JsonValue]:
    """Plain notation with exactly the column's scale; negative zero becomes zero."""
    values: list[JsonValue] = []
    for value in column.to_pylist():
        if value is None:
            values.append(None)
        else:
            values.append(format(value.copy_abs() if value.is_zero() else value, "f"))
    return values


def _float32(column: pa.Array) -> list[JsonValue]:
    """The shortest decimal that reads back as the same 32-bit float (0.1, not 0.1000000015)."""
    texts = pc.cast(column, pa.string()).to_pylist()
    values: list[JsonValue] = []
    for text in texts:
        if text is None:
            values.append(None)
        elif text in {"nan", "NaN"}:
            values.append("NaN")
        elif text in {"inf", "Infinity"}:
            values.append("Infinity")
        elif text in {"-inf", "-Infinity"}:
            values.append("-Infinity")
        else:
            values.append(float(text))
    return values


def _plain(column: pa.Array) -> list[JsonValue]:
    values: list[JsonValue] = column.to_pylist()
    return values


def _converter(kind: str, column: str) -> Callable[[pa.Array], list[JsonValue]]:
    if kind in {"BOOLEAN", "VARCHAR", "UUID"} or kind in SMALL_INTEGERS:
        return _strings if kind == "UUID" else _plain
    if kind in WIDE_INTEGERS:
        return _strings
    if kind.startswith("DECIMAL("):
        return _decimals
    if kind == "FLOAT":
        return _float32
    if kind == "DOUBLE":
        return _floats
    if kind == "DATE":
        return _dates
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
