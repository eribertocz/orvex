"""Column descriptors of `semantic-result` v1 and the `logical` <-> `encoding` mapping (P2-06).

The `logical` type is the engine-independent type of P2-04's type system; the `encoding` is
the `store/v1` transport type, which fixes the Arrow type and the `ResultJson` encoding. The
pairs below are the README's mapping table, and the only ones allowed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

import pyarrow as pa

KNOWN_STATES = frozenset({"undefined_at_total", "others"})
"""The cell states of contract v1: the only ones this library emits."""

STATE_NAME = re.compile(r"[a-z][a-z0-9_]{0,62}")
DECIMAL = re.compile(r"DECIMAL\(([0-9]{1,2}),([0-9]{1,2})\)")

INTEGERS = {
    "TINYINT": (8, True, pa.int8()),
    "SMALLINT": (16, True, pa.int16()),
    "INTEGER": (32, True, pa.int32()),
    "BIGINT": (64, True, pa.int64()),
    "HUGEINT": (128, True, pa.decimal128(38, 0)),
    "UTINYINT": (8, False, pa.uint8()),
    "USMALLINT": (16, False, pa.uint16()),
    "UINTEGER": (32, False, pa.uint32()),
    "UBIGINT": (64, False, pa.uint64()),
    "UHUGEINT": (128, False, pa.string()),
}
"""Integer encodings: width, signedness and Arrow type (`store/v1`: `HUGEINT` travels as
`decimal128(38,0)`, `UHUGEINT` as decimal text, since Arrow has no exact unsigned 128-bit type)."""

OTHERS = {
    "VARCHAR": ("String", pa.string()),
    "BOOLEAN": ("Boolean", pa.bool_()),
    "FLOAT": ("Float", pa.float32()),
    "DOUBLE": ("Float", pa.float64()),
    "DATE": ("Date", pa.date32()),
    "TIMESTAMP": ("DateTime", pa.timestamp("us")),
    "TIMESTAMP WITH TIME ZONE": ("DateTimeTz", pa.timestamp("us", tz="UTC")),
    "UUID": ("Opaque", pa.string()),
    "BLOB": ("Opaque", pa.binary()),
    "INTERVAL": ("Opaque", pa.month_day_nano_interval()),
}

FAMILIES = frozenset(
    {"String", "Integer", "Decimal", "Float", "Boolean", "Date", "DateTime", "DateTimeTz", "Opaque"}
)
ROLES = {"aggregate": frozenset({"dimension", "metric"}), "detail": frozenset({"field"})}
STRING_ENCODINGS = frozenset(
    {"VARCHAR", "UUID", "BLOB", "DATE", "TIMESTAMP", "TIMESTAMP WITH TIME ZONE", "INTERVAL"}
)
WIDE_INTEGERS = frozenset({"BIGINT", "UBIGINT", "HUGEINT", "UHUGEINT"})


class InvalidResult(ValueError):
    """A result that breaks `semantic-result` v1: an internal error, never a user's.

    `reason` is a stable slug naming the broken rule (the tests and reader fixtures use it).
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason


def decimal_parameters(encoding: str) -> tuple[int, int] | None:
    """`(precision, scale)` of a `DECIMAL(p,s)` encoding with 1 <= p <= 38 and s <= p."""
    match = DECIMAL.fullmatch(encoding)
    if match is None:
        return None
    precision, scale = int(match[1]), int(match[2])
    return (precision, scale) if 1 <= precision <= 38 and scale <= precision else None


def known_encoding(encoding: str) -> bool:
    return encoding in INTEGERS or encoding in OTHERS or decimal_parameters(encoding) is not None


def arrow_type(encoding: str) -> pa.DataType:
    """The `store/v1` Arrow type of a known `encoding`."""
    if encoding in INTEGERS:
        return INTEGERS[encoding][2]
    if encoding in OTHERS:
        return OTHERS[encoding][1]
    parameters = decimal_parameters(encoding)
    if parameters is None:
        raise InvalidResult("encoding", f"{encoding!r} is not a store/v1 transport type")
    return pa.decimal128(*parameters)


def pair_allowed(logical: Mapping[str, object], encoding: str) -> bool:
    """Whether `logical` and `encoding` form a pair of the README's mapping table."""
    family = logical.get("family")
    if encoding in INTEGERS:
        width, signed, _ = INTEGERS[encoding]
        return (
            family == "Integer"
            and logical.get("width") == width
            and logical.get("signed") is signed
            and set(logical) == {"family", "width", "signed"}
        )
    if encoding in OTHERS:
        allowed = {"family", "zone"} if family in {"DateTime", "DateTimeTz"} else {"family"}
        return family == OTHERS[encoding][0] and set(logical) <= allowed
    parameters = decimal_parameters(encoding)
    return (
        parameters is not None
        and family == "Decimal"
        and set(logical) == {"family", "precision", "scale"}
        and (logical.get("precision"), logical.get("scale")) == parameters
    )


def value_kind_fits(encoding: str, value: object) -> bool:
    """Whether a value cell's JSON kind is the `ResultJson` encoding of `encoding`: exact types
    (decimals, 64- and 128-bit integers) as strings, never numbers."""
    if encoding == "BOOLEAN":
        return isinstance(value, bool)
    if isinstance(value, bool):
        return False
    if encoding in {"FLOAT", "DOUBLE"}:
        return isinstance(value, float) or value in {"NaN", "Infinity", "-Infinity"}
    if encoding in INTEGERS and encoding not in WIDE_INTEGERS:
        return isinstance(value, int)
    return isinstance(value, str)


@dataclass(frozen=True, slots=True)
class ColumnSpec:
    """What the plan knows of one output column, before it becomes a contract descriptor.

    A masked member (`mask` set) keeps here what the compiled member says of it, but
    `build_result` leaves all of it out: its descriptor is `VARCHAR`, not nullable, with the
    mask, and no logical type, semantic type, unit, currency or exactness (security §3.3).
    """

    name: str
    member: Mapping[str, str]
    role: str
    encoding: str
    nullable: bool
    logical: Mapping[str, object] | None = None
    semantic_type: str | None = None
    unit: str | None = None
    currency: str | None = None
    exactness: str | None = None
    context: str | None = None
    apply: tuple[str, ...] | None = None
    bucket: Mapping[str, str] | None = None
    mask: str | None = None


def descriptor(spec: ColumnSpec) -> dict[str, object]:
    """The contract column descriptor of `spec`, with members in a fixed order."""
    column: dict[str, object] = {"name": spec.name, "member": dict(spec.member), "role": spec.role}
    if spec.context is not None:
        column["context"] = spec.context
    if spec.apply is not None:
        column["apply"] = list(spec.apply)
    if spec.bucket is not None:
        column["bucket"] = dict(spec.bucket)
    if spec.mask is not None:
        column.update(encoding="VARCHAR", nullable=False, masked=True, mask=spec.mask)
        return column
    if spec.logical is not None:
        column["logical"] = dict(spec.logical)
    column["encoding"] = spec.encoding
    for key in ("semantic_type", "unit", "currency", "exactness"):
        value = getattr(spec, key)
        if value is not None:
            column[key] = value
    column["nullable"] = spec.nullable
    return column


def check_descriptor(column: Mapping[str, object], mode: str) -> None:
    """The rules a descriptor obeys beyond the schema: role and mode agree; a masked column
    appears only in detail mode; any other column states its logical type and exactness, in a
    pair of the mapping table; a `Float` is approximate and only a `Float` travels as one."""
    name = column.get("name")
    if column.get("role") not in ROLES.get(mode, frozenset()):
        raise InvalidResult("role", f"column {name!r}: role {column.get('role')!r} in {mode} mode")
    if column.get("masked") is True:
        if mode != "detail":
            raise InvalidResult("masked_in_aggregate", f"column {name!r}: masked in {mode} mode")
        if column.get("encoding") != "VARCHAR" or column.get("nullable") is not False:
            raise InvalidResult("masked_shape", f"column {name!r}: a mask is VARCHAR, not null")
        disclosed = {"logical", "semantic_type", "unit", "currency", "exactness"} & set(column)
        if disclosed or not isinstance(column.get("mask"), str) or not column["mask"]:
            raise InvalidResult("masked_shape", f"column {name!r}: discloses {sorted(disclosed)}")
        return
    if "mask" in column:
        raise InvalidResult("masked_shape", f"column {name!r}: a mask without masked")
    logical = column.get("logical")
    encoding = column.get("encoding")
    if not isinstance(logical, Mapping):
        raise InvalidResult("logical_missing", f"column {name!r} states no logical type")
    if logical.get("family") == "Time":
        raise InvalidResult(
            "time_column",
            f"column {name!r}: store/v1 cannot transport Time; refuse the query upstream with "
            "UNSUPPORTED_OPERATION (operation: time_column)",
        )
    if logical.get("family") not in FAMILIES:
        raise InvalidResult("logical_family", f"column {name!r}: {logical.get('family')!r}")
    if not isinstance(encoding, str) or not pair_allowed(logical, encoding):
        raise InvalidResult(
            "logical_encoding", f"column {name!r}: {dict(logical)} with {encoding!r}"
        )
    exactness = column.get("exactness")
    if exactness not in {"exact", "approximate"}:
        raise InvalidResult("exactness_missing", f"column {name!r} states no exactness")
    if logical["family"] == "Float" and exactness != "approximate":
        raise InvalidResult("float_exact", f"column {name!r}: a Float is approximate")
    if "unit" in column and "currency" in column:
        raise InvalidResult("unit_and_currency", f"column {name!r}: a unit or a currency")
