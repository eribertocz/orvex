"""The Arrow form of a lake read (law 11): `UHUGEINT` as exact decimal text, each column's DuckDB
type in its field metadata, and zoned timestamps named `UTC`.
"""

from __future__ import annotations

import pyarrow as pa
from orvex.store.sql_text import identifier as _identifier


def exact_select(sql: str, names: list[str], types: list[str]) -> str | None:
    """`sql` with every UHUGEINT column cast to decimal text, or None when there is none.

    Arrow has no exact 128-bit unsigned type: DuckDB would wrap large values into
    decimal128(38,0) (the maximum becomes -1). Columns are addressed by position (`#n`), so
    duplicate names are kept as they are and nothing else changes.
    """
    if "UHUGEINT" not in types:
        return None
    columns = ", ".join(
        (f"CAST(#{index} AS VARCHAR)" if kind == "UHUGEINT" else f"#{index}")
        + f" AS {_identifier(name)}"
        for index, (name, kind) in enumerate(zip(names, types, strict=True), start=1)
    )
    return f"SELECT {columns} FROM ({sql})"


def _with_duckdb_types(schema: pa.Schema, types: list[str]) -> pa.Schema:
    """Record each column's DuckDB type in its field metadata (`duckdb_type`).

    Arrow alone cannot tell `HUGEINT` from `DECIMAL(38,0)`; consumers (P1-03) need the type.
    """
    fields = [
        field.with_metadata({**(field.metadata or {}), b"duckdb_type": kind.encode()})
        for field, kind in zip(schema, types, strict=True)
    ]
    return pa.schema(fields, metadata=schema.metadata)


def _utc_schema(schema: pa.Schema) -> pa.Schema:
    """Name every zoned timestamp's zone `UTC` (DuckDB may say `Etc/UTC`); values are unchanged."""
    fields = [
        field.with_type(pa.timestamp(field.type.unit, tz="UTC"))
        if pa.types.is_timestamp(field.type) and field.type.tz is not None
        else field
        for field in schema
    ]
    return pa.schema(fields, metadata=schema.metadata)
