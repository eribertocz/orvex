"""Typed statement parameters: each value checked against its declared type, bound as text and
converted by DuckDB (`CAST($name AS <type>)`), never through a Python float or zone library.
"""

from __future__ import annotations

import datetime as dt
import math
import re

import duckdb
from orvex.contracts.store.v1 import Parameter
from orvex.store.errors import StoreError
from orvex.store.sql_parser import _tokens

INTEGER_RANGES = {
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


_INTEGER = re.compile(r"-?[0-9]+")
_DECIMAL = re.compile(r"-?[0-9]+(\.[0-9]+)?")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_MOMENT = r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?"
_TIMESTAMP = re.compile(_MOMENT)
_TIMESTAMPTZ = re.compile(_MOMENT + r"(Z|[+-][0-9]{2}:[0-9]{2})")
_DOUBLE = re.compile(r"-?[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?")


def _invalid(parameter: Parameter, reason: str) -> StoreError:
    return StoreError(
        "STORE_PARAMETER_INVALID",
        f"Parameter {parameter.name} is not a valid {parameter.type.root}.",
        {"parameter": parameter.name, "reason": reason},
    )


def _converted(parameter: Parameter) -> str | None:
    """The parameter's text after a strict check against its type.

    The text is bound as is and converted by `CAST(... AS <type>)` inside DuckDB (`_typed`), so
    no value passes through a Python float or a time-zone library.
    """
    value = parameter.value
    kind = parameter.type.root
    if value is None:
        return None
    if kind.startswith("DECIMAL"):
        precision, scale = (int(part) for part in kind[8:-1].split(","))
        if not 1 <= precision <= 38 or scale > precision:
            raise _invalid(parameter, "precision must be 1-38 and scale at most the precision")
        if not _DECIMAL.fullmatch(value):
            raise _invalid(parameter, "expected digits with an optional fraction")
        fraction = len(value.partition(".")[2])
        whole = len(value.lstrip("-").partition(".")[0].lstrip("0"))
        if fraction > scale or whole > precision - scale:
            raise _invalid(parameter, f"does not fit DECIMAL({precision},{scale})")
        return value
    if kind in INTEGER_RANGES:
        if not _INTEGER.fullmatch(value):
            raise _invalid(parameter, "expected digits")
        number = int(value, 10)
        low, high = INTEGER_RANGES[kind]
        if not low <= number <= high:
            raise _invalid(parameter, f"out of range for {kind}")
        return value
    if kind == "BOOLEAN":
        if value not in {"true", "false"}:
            raise _invalid(parameter, "expected true or false")
        return value
    if kind == "DOUBLE":
        if not _DOUBLE.fullmatch(value):
            raise _invalid(parameter, "expected a finite decimal number")
        if not math.isfinite(float(value)):
            raise _invalid(parameter, "out of range for DOUBLE")
        return value
    if kind in {"DATE", "TIMESTAMP", "TIMESTAMPTZ"} and value in {"infinity", "-infinity"}:
        return value  # DuckDB's own infinite dates and times (the lowest value is `-infinity`)
    try:
        if kind == "DATE":
            if not _DATE.fullmatch(value):
                raise ValueError("expected YYYY-MM-DD")
            dt.date.fromisoformat(value)
            return value
        if kind == "TIMESTAMP":
            if not _TIMESTAMP.fullmatch(value):
                raise ValueError("expected YYYY-MM-DDTHH:MM:SS[.ffffff] without a zone")
            dt.datetime.fromisoformat(value)
            return value
        if kind == "TIMESTAMPTZ":
            if not _TIMESTAMPTZ.fullmatch(value):
                raise ValueError("expected YYYY-MM-DDTHH:MM:SS[.ffffff] with Z or an offset")
            dt.datetime.fromisoformat(value)
            return value
    except ValueError as error:
        raise _invalid(parameter, str(error)) from error
    return value


def _bound(statement: duckdb.Statement, parameters: list[Parameter]) -> dict[str, str | None]:
    names = [parameter.name for parameter in parameters]
    expected = set(statement.named_parameters)
    if len(names) != len(set(names)) or set(names) != expected:
        raise StoreError(
            "STORE_PARAMETER_INVALID",
            "The parameters must match the $names used in the SQL exactly once.",
            {
                "missing": ",".join(sorted(expected - set(names))),
                "extra": ",".join(sorted(set(names) - expected)),
            },
        )
    return {parameter.name: _converted(parameter) for parameter in parameters}


def _typed(sql: str, parameters: list[Parameter]) -> str:
    """The SQL with every `$name` wrapped in `CAST($name AS <declared type>)`.

    DuckDB's tokenizer finds the parameters, so text inside strings and comments is untouched.
    The declared type is validated by the contract pattern before it reaches the SQL.
    """
    types = {parameter.name: parameter.type.root for parameter in parameters}
    tokens = _tokens(sql)
    edits: list[tuple[int, int, str]] = []
    for index, (position, kind) in enumerate(tokens[:-1]):
        following, following_kind = tokens[index + 1]
        if (
            kind == duckdb.token_type.operator
            and sql[position] == "$"
            and following == position + 1
            and following_kind == duckdb.token_type.identifier
        ):
            end = tokens[index + 2][0] if index + 2 < len(tokens) else len(sql)
            name = sql[following:end].rstrip()
            if name in types:
                stop = following + len(name)
                edits.append((position, stop, f"CAST(${name} AS {types[name]})"))
    wrapped = {sql[start + 1 : stop] for start, stop, _ in edits}
    if wrapped != set(types):
        raise StoreError(
            "STORE_PARAMETER_INVALID",
            "Every parameter must appear in the SQL as $name.",
            {"unplaced": ",".join(sorted(set(types) - wrapped))},
        )
    for start, stop, replacement in reversed(edits):
        sql = sql[:start] + replacement + sql[stop:]
    return sql
