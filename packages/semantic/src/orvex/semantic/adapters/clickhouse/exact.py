"""Exact store types in ClickHouse (law 11): each store type's one spelling, its ClickHouse
type, and the checked conversion that keeps a value inside its declared type.

ClickHouse's exact arithmetic does not stop at a type's edge: `Int128` and `Decimal128` sums
and products wrap around, `CAST` of a too-large text to `Int128` wraps, and no conversion to
`Decimal(P, S)` tests the precision `P`. So an exact value is carried in 76 digits
(`Decimal256`, `wide`), which holds every value the plan may compute before it is checked,
and `checked` returns it to its declared type, stopping the statement with `throwIf` past the
type's range. The reference raises an overflow at the same points.
"""

from __future__ import annotations

import re

from orvex.semantic.adapters.dialect import _unsupported

_TYPES = {
    "BOOLEAN": "Bool",
    "TINYINT": "Int8",
    "SMALLINT": "Int16",
    "INTEGER": "Int32",
    "BIGINT": "Int64",
    "HUGEINT": "Int128",
    "UTINYINT": "UInt8",
    "USMALLINT": "UInt16",
    "UINTEGER": "UInt32",
    "UBIGINT": "UInt64",
    "UHUGEINT": "UInt128",
    "FLOAT": "Float32",
    "DOUBLE": "Float64",
    "VARCHAR": "String",
    "UUID": "UUID",
    "DATE": "Date32",
    "TIMESTAMP WITH TIME ZONE": "DateTime64(6, 'UTC')",
}
# Each other spelling of a store type, by its canonical name (`canonical`).
_SPELLINGS = {"TIMESTAMPTZ": "TIMESTAMP WITH TIME ZONE"}
DECIMAL = re.compile(r"DECIMAL\((\d+),(\d+)\)")
# The range of each integer store type.
_INTEGERS: dict[str, tuple[int, int]] = {
    "TINYINT": (-(2**7), 2**7 - 1),
    "SMALLINT": (-(2**15), 2**15 - 1),
    "INTEGER": (-(2**31), 2**31 - 1),
    "BIGINT": (-(2**63), 2**63 - 1),
    "HUGEINT": (-(2**127), 2**127 - 1),
    "UTINYINT": (0, 2**8 - 1),
    "USMALLINT": (0, 2**16 - 1),
    "UINTEGER": (0, 2**32 - 1),
    "UBIGINT": (0, 2**64 - 1),
    "UHUGEINT": (0, 2**128 - 1),
}
# The magnitude a `Decimal256` holds without wrapping: a product of two exact values fits only
# when their magnitudes, in bits, add up to at most this.
WIDE_BITS = 255
OUT_OF_RANGE = "a value is outside its declared type"


def canonical(kind: str) -> str:
    """A store type's one spelling: upper case, no space inside `DECIMAL(p,s)`, single spaces
    elsewhere, and `TIMESTAMPTZ` as `TIMESTAMP WITH TIME ZONE`."""
    name = re.sub(r"\s*([(),])\s*", r"\1", " ".join(kind.upper().split()))
    return _SPELLINGS.get(name, name)


def engine_type(kind: str) -> str:
    """The ClickHouse type of a store type (`contracts/store/v1`, "ClickHouse Direct Query")."""
    kind = canonical(kind)
    found = DECIMAL.fullmatch(kind)
    if found is not None:
        return f"Decimal({found.group(1)}, {found.group(2)})"
    if kind not in _TYPES:
        raise _unsupported()
    return _TYPES[kind]


def magnitude_bits(kind: str) -> int:
    """The bits of the largest magnitude an exact store type holds."""
    kind = canonical(kind)
    found = DECIMAL.fullmatch(kind)
    if found is not None:
        return int(10 ** int(found.group(1)) - 1).bit_length()
    if kind not in _INTEGERS:
        raise _unsupported()
    low, high = _INTEGERS[kind]
    return max(-low, high).bit_length()


def wide(expression: str, scale: int) -> str:
    """An exact value in 76 digits at `scale`, where no step of the plan wraps."""
    return f"toDecimal256({expression}, {scale})"


def constant(value: int) -> str:
    """An exact integer constant in 76 digits, written from the model, never a caller's."""
    return f"toDecimal256('{value}', 0)"


def checked(expression: str, kind: str) -> str:
    """`expression` as the store type `kind`. An exact value outside the type's range, or past
    a `DECIMAL`'s precision, stops the statement; any other type is a plain `CAST`."""
    kind = canonical(kind)
    found = DECIMAL.fullmatch(kind)
    if found is not None:
        precision, scale = int(found.group(1)), int(found.group(2))
        value = wide(expression, scale)
        outside = f"abs({value}) >= {constant(10 ** (precision - scale))}"
    elif kind in _INTEGERS:
        low, high = _INTEGERS[kind]
        value = wide(expression, 0)
        outside = f"{value} < {constant(low)} OR {value} > {constant(high)}"
    else:
        return f"CAST({expression} AS {engine_type(kind)})"
    guarded = f"{value} + throwIf(coalesce({outside}, false), '{OUT_OF_RANGE}')"
    return f"CAST({guarded} AS {engine_type(kind)})"


def exact_kind(kind: str) -> bool:
    """Whether a store type is exact: an integer or a `DECIMAL`."""
    kind = canonical(kind)
    return kind in _INTEGERS or DECIMAL.fullmatch(kind) is not None
