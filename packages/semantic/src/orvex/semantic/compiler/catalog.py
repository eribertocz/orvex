"""The source catalog and the logical types (P2-04c, #289; #280 is the source of truth).

`SourceCatalog` gives each source column's store type, as DuckDB names it (`contracts/store/v1`),
keyed by the dataset's Ossie `source` and then by column. `server` supplies it from the store;
tests use fixtures. Column names match as DuckDB matches them: ignoring the case of ASCII
letters only.

A store type maps to Ossie's logical vocabulary (spec §Data types) with the exact shape Ossie
leaves unspecified: precision and scale of a `Decimal`, width and signedness of an `Integer`.
A type outside that vocabulary is `Opaque`: carried, never computed with.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

Datatype = Literal[
    "String",
    "Integer",
    "Decimal",
    "Float",
    "Boolean",
    "Date",
    "Time",
    "DateTime",
    "DateTimeTz",
    "Opaque",
]
MAX_PRECISION = 38


@dataclass(frozen=True, slots=True)
class LogicalType:
    """`datatype` None means unknown: an engine-dialect expression without a declared
    `datatype`, or the NULL literal. `precision` and `scale` belong to a `Decimal` (None while
    undecided, such as a division without `rounding`); `width` and `signed` to an `Integer`;
    `zone` is the named zone of a `DateTimeTz` or the civil zone of a `DateTime`."""

    datatype: Datatype | None
    precision: int | None = None
    scale: int | None = None
    width: int | None = None
    signed: bool | None = None
    zone: str | None = None


@dataclass(frozen=True, slots=True)
class SourceColumn:
    type: str  # the DuckDB type name, e.g. `DECIMAL(18,2)`
    nullable: bool = True


SourceCatalog = Mapping[str, Mapping[str, SourceColumn]]

_INTEGERS: dict[str, tuple[int, bool]] = {
    "TINYINT": (8, True),
    "SMALLINT": (16, True),
    "INTEGER": (32, True),
    "BIGINT": (64, True),
    "HUGEINT": (128, True),
    "UTINYINT": (8, False),
    "USMALLINT": (16, False),
    "UINTEGER": (32, False),
    "UBIGINT": (64, False),
    "UHUGEINT": (128, False),
}
_SIMPLE: dict[str, Datatype] = {
    "VARCHAR": "String",
    "BOOLEAN": "Boolean",
    "FLOAT": "Float",
    "DOUBLE": "Float",
    "DATE": "Date",
    "TIME": "Time",
    "TIMESTAMP": "DateTime",
    "TIMESTAMP_S": "DateTime",
    "TIMESTAMP_MS": "DateTime",
    "TIMESTAMP_NS": "DateTime",
    "TIMESTAMP WITH TIME ZONE": "DateTimeTz",
    "TIMESTAMPTZ": "DateTimeTz",
}
_DECIMAL = re.compile(r"DECIMAL\(\s*([0-9]+)\s*,\s*([0-9]+)\s*\)")


def logical_of(store_type: str) -> LogicalType:
    """The logical type of a DuckDB type name; anything else is `Opaque`."""
    name = " ".join(store_type.upper().split())
    if name in _INTEGERS:
        width, signed = _INTEGERS[name]
        return LogicalType("Integer", width=width, signed=signed)
    if name in _SIMPLE:
        return LogicalType(_SIMPLE[name])
    decimal = _DECIMAL.fullmatch(name)
    if decimal is not None:
        precision, scale = int(decimal.group(1)), int(decimal.group(2))
        if 1 <= precision <= MAX_PRECISION and scale <= precision:
            return LogicalType("Decimal", precision=precision, scale=scale)
    return LogicalType("Opaque")


def column_key(name: str) -> str:
    """DuckDB's column identity: ASCII letters compared without case."""
    return name.translate(_ASCII_UPPER)


_ASCII_UPPER = str.maketrans("abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ")


def integer_digits(width: int, signed: bool) -> int:
    """Decimal digits of the largest magnitude an integer of that shape holds."""
    return len(str(2 ** (width - 1) if signed else 2**width - 1))
