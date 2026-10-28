"""Model tests: write, audit, publish inside the run transaction (P1-15).

Each test is one count of failing rows, computed in DuckDB on the model's whole table after it
is written, inside the run's transaction (rows never reach Python, ADR-0025). Every value a test
compares against travels as a bound parameter of the column's own type, converted exactly from
the sidecar's text: a bound never passes through a float, and a comparison never makes DuckDB
fall back to `DOUBLE`.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext

from orvex.contracts.model_config.v1 import (
    AcceptedRange,
    AcceptedValues,
    ModelError,
    Relationship,
    Test,
)
from orvex.contracts.store.v1 import Parameter, ParameterType, StatementRequest
from orvex.transform.models import Model, problem

type Columns = Sequence[tuple[str, str]]

INTEGER_RANGES: Mapping[str, tuple[int, int]] = {
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
FLOATS = frozenset({"FLOAT", "DOUBLE"})
TEMPORAL = {"DATE": "DATE", "TIMESTAMP": "TIMESTAMP", "TIMESTAMP WITH TIME ZONE": "TIMESTAMPTZ"}
_DECIMAL_TYPE = re.compile(r"DECIMAL\((\d+),(\d+)\)")
# No leading zeros: YAML 1.1 reads an unquoted `017` as the octal 15.
_NUMBER = re.compile(r"[+-]?(0|[1-9][0-9]*)(\.[0-9]+)?")
MAX_DIGITS = 38
# Enough digits for any 38-digit bound against any DECIMAL(38,s) limit: no rounding happens.
_PRECISION = 80


@dataclass(frozen=True, slots=True)
class ModelTest:
    """One test of a model: `request` counts the failing rows (0 passes)."""

    name: str
    request: StatementRequest


def _identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _parameter(name: str, kind: str, value: str) -> Parameter:
    return Parameter(name=name, type=ParameterType(kind), value=value)


def kind_of(test: Test) -> str:
    """The test's kind: unique, not_null, accepted_range, accepted_values or relationship."""
    (kind,) = test.root.model_fields_set
    return kind


def name_of(test: Test) -> str:
    """The name a run record and the output use for a test."""
    body = getattr(test.root, kind_of(test))
    if isinstance(body, list):
        return f"{kind_of(test)}({', '.join(column.root for column in body)})"
    if isinstance(body, Relationship):
        return f"relationship({body.column.root} → {body.to}.{body.field.root})"
    assert isinstance(body, AcceptedRange | AcceptedValues)
    return f"{kind_of(test)}({body.column.root})"


def _bound_text(model: Model, pointer: str, value: int | float | str) -> str | None:
    """A bound as exact decimal text: the YAML text of a number, or a quoted string."""
    text = model.numbers.get(pointer, value) if not isinstance(value, str) else value
    if not isinstance(text, str) or not _NUMBER.fullmatch(text):
        return None
    if len(text.lstrip("+-").replace(".", "").lstrip("0")) > MAX_DIGITS:
        return None
    return text


def _moment_problem(text: str, kind: str) -> str | None:
    """Why a quoted date or time bound cannot be read exactly, or None."""
    try:
        if kind == "DATE":
            dt.date.fromisoformat(text)
            return None
        moment = dt.datetime.fromisoformat(text)
    except ValueError:
        return f"{text!r} is not an ISO 8601 {kind.lower()}."
    if kind == "TIMESTAMP WITH TIME ZONE" and moment.tzinfo is None:
        return "give the offset (for example +00:00): a time without one is ambiguous."
    if kind == "TIMESTAMP" and moment.tzinfo is not None:
        return "a TIMESTAMP column has no time zone; leave the offset out."
    return None


def _family(kind: str) -> str:
    if kind in INTEGER_RANGES:
        return "integer"
    if _DECIMAL_TYPE.fullmatch(kind):
        return "decimal"
    if kind in FLOATS:
        return "float"
    if kind in TEMPORAL:
        return "temporal"
    if kind == "VARCHAR":
        return "text"
    if kind == "BOOLEAN":
        return "boolean"
    return "other"


def problems(
    model: Model, columns: Columns, outputs: Mapping[str, Columns], models: Mapping[str, Model]
) -> list[ModelError]:
    """Rules of the model's tests that need its output columns (and the related models')."""
    file = model.config_file or model.sql_file
    types = {name.lower(): kind for name, kind in columns}
    found: list[ModelError] = []

    def invalid(pointer: str, message: str) -> None:
        found.append(
            problem(
                "MODEL_CONFIG_INVALID",
                f"{file}: {pointer}: {message}",
                file,
                model=model.name,
                path=pointer,
            )
        )

    for index, test in enumerate(model.config.tests or []):
        kind = kind_of(test)
        base = f"/tests/{index}/{kind}"
        body = getattr(test.root, kind)
        if isinstance(body, list):
            for position, column in enumerate(body):
                if column.root.lower() not in types:
                    invalid(f"{base}/{position}", f"{column.root} is not an output column.")
            continue
        column_type = types.get(body.column.root.lower())
        if column_type is None:
            invalid(f"{base}/column", f"{body.column.root} is not an output column.")
            continue
        family = _family(column_type)
        if isinstance(body, AcceptedRange):
            if body.min is None and body.max is None:
                invalid(base, "give min, max or both.")
            elif family not in {"integer", "decimal", "float", "temporal"}:
                invalid(f"{base}/column", f"{column_type} is neither a number nor a date or time.")
            for side in ("min", "max"):
                bound = getattr(body, side)
                if bound is None or family not in {"integer", "decimal", "float", "temporal"}:
                    continue
                if family == "temporal":
                    if not isinstance(bound.root, str):
                        invalid(f"{base}/{side}", "a date or time bound is written in quotes.")
                    elif (reason := _moment_problem(bound.root, column_type)) is not None:
                        invalid(f"{base}/{side}", reason)
                elif _bound_text(model, f"{base}/{side}", bound.root) is None:
                    invalid(
                        f"{base}/{side}",
                        "write the bound as a decimal number of at most 38 digits (quote it to "
                        "keep every digit: YAML reads long decimals as floats).",
                    )
        elif isinstance(body, AcceptedValues):
            accepted = {"text": str, "integer": int, "boolean": bool}.get(family)
            if accepted is None:
                invalid(
                    f"{base}/column", "accepted_values needs a VARCHAR, integer or BOOLEAN column."
                )
                continue
            for position, value in enumerate(body.values):
                if type(value) is not accepted:  # a YAML `no` is a bool, `7` an int
                    invalid(
                        f"{base}/values/{position}",
                        f"{value!r} is not a {family} value for a {column_type} column "
                        "(quote text that YAML reads as a boolean or a number).",
                    )
        else:
            if body.to not in models:
                continue  # MODEL_REFERENCE_UNKNOWN, from the dependency graph
            if body.to not in outputs:
                invalid(f"{base}/to", f"{body.to} cannot be read by a test (an extraction model).")
                continue
            related = {name.lower() for name, _ in outputs[body.to]}
            if body.field.root.lower() not in related:
                invalid(f"{base}/field", f"{body.field.root} is not a column of {body.to}.")
    return found


def _integer_bound(text: str, side: str, kind: str) -> tuple[str | None, bool]:
    """(the bound in the column's integer type, or None; whether every value is outside)."""
    with localcontext(prec=_PRECISION):
        return _integer_bound_exact(text, side, kind)


def _integer_bound_exact(text: str, side: str, kind: str) -> tuple[str | None, bool]:
    exact = Decimal(text)
    whole = int(exact.to_integral_value(ROUND_CEILING if side == "min" else ROUND_FLOOR))
    low, high = INTEGER_RANGES[kind]
    if side == "min":
        return (
            (None, False) if whole <= low else (None, True) if whole > high else (str(whole), False)
        )
    return (None, False) if whole >= high else (None, True) if whole < low else (str(whole), False)


def _decimal_bound(text: str, side: str, kind: str) -> tuple[str | None, bool]:
    with localcontext(prec=_PRECISION):
        return _decimal_bound_exact(text, side, kind)


def _decimal_bound_exact(text: str, side: str, kind: str) -> tuple[str | None, bool]:
    match = _DECIMAL_TYPE.fullmatch(kind)
    assert match is not None
    precision, scale = int(match.group(1)), int(match.group(2))
    step = Decimal(1).scaleb(-scale)
    exact = Decimal(text).quantize(step, ROUND_CEILING if side == "min" else ROUND_FLOOR)
    limit = Decimal(10) ** (precision - scale) - step
    if side == "min":
        if exact <= -limit:
            return None, False
        return (None, True) if exact > limit else (f"{exact:f}", False)
    if exact >= limit:
        return None, False
    return (None, True) if exact < -limit else (f"{exact:f}", False)


def _range_sql(
    model: Model, index: int, body: AcceptedRange, column: str, kind: str
) -> tuple[str, list[Parameter]]:
    """The condition a failing row meets, and its parameters."""
    family = _family(kind)
    conditions: list[str] = []
    parameters: list[Parameter] = []
    for side, operator in (("min", "<"), ("max", ">")):
        bound = getattr(body, side)
        if bound is None:
            continue
        name = f"p_{side}"
        if family == "temporal":
            parameters.append(_parameter(name, "VARCHAR", str(bound.root)))
            conditions.append(f"{column} {operator} CAST(${name} AS {kind})")
            continue
        text = _bound_text(model, f"/tests/{index}/accepted_range/{side}", bound.root)
        assert text is not None  # checked at validation
        if family == "float":
            parameters.append(_parameter(name, "DOUBLE", text))
            conditions.append(f"{column} {operator} ${name}")
            continue
        converter = _integer_bound if family == "integer" else _decimal_bound
        value, everything = converter(text, side, kind)
        if everything:
            conditions.append("TRUE")
        elif value is not None:
            parameters.append(_parameter(name, kind, value))
            conditions.append(f"{column} {operator} ${name}")
    return " OR ".join(conditions) or "FALSE", parameters


def steps(
    model: Model,
    relation: str,
    columns: Columns,
    relations: Mapping[str, str],
) -> list[ModelTest]:
    """The tests of a model whose rows are `relation` (its table, or a view's SQL in
    parentheses); `relations` gives the same for every model a relationship test can name."""
    types = {name.lower(): kind for name, kind in columns}
    found: list[ModelTest] = []
    for index, test in enumerate(model.config.tests or []):
        kind = kind_of(test)
        body = getattr(test.root, kind)
        parameters: list[Parameter] = []
        if isinstance(body, list):
            listed = ", ".join(_identifier(column.root) for column in body)
            if kind == "unique":
                sql = (
                    f"SELECT coalesce(sum(n), 0) FROM (SELECT count(*) AS n FROM {relation} AS t "
                    f"GROUP BY {listed} HAVING count(*) > 1)"
                )
            else:
                nulls = " OR ".join(f"{_identifier(column.root)} IS NULL" for column in body)
                sql = f"SELECT count(*) FROM {relation} AS t WHERE {nulls}"
        elif isinstance(body, AcceptedRange):
            column = _identifier(body.column.root)
            condition, parameters = _range_sql(
                model, index, body, column, types[body.column.root.lower()]
            )
            sql = (
                f"SELECT count(*) FROM {relation} AS t WHERE {column} IS NOT NULL AND ({condition})"
            )
        elif isinstance(body, AcceptedValues):
            column = _identifier(body.column.root)
            column_type = types[body.column.root.lower()]
            names: list[str] = []
            for value in body.values:
                if isinstance(value, bool):
                    text = "true" if value else "false"
                elif isinstance(value, int):
                    low, high = INTEGER_RANGES[column_type]
                    if not low <= value <= high:
                        continue  # a value the column cannot hold never matches
                    text = str(value)
                else:
                    text = value
                name = f"p_value_{len(parameters)}"
                names.append(f"${name}")
                parameters.append(_parameter(name, column_type, text))
            accepted = f"{column} NOT IN ({', '.join(names)})" if names else "TRUE"
            sql = f"SELECT count(*) FROM {relation} AS t WHERE {column} IS NOT NULL AND {accepted}"
        else:
            child, parent = _identifier(body.column.root), _identifier(body.field.root)
            sql = (
                f"SELECT count(*) FROM {relation} AS child WHERE child.{child} IS NOT NULL "
                f"AND NOT EXISTS (SELECT 1 FROM {relations[body.to]} AS parent "
                f"WHERE parent.{parent} = child.{child})"
            )
        found.append(
            ModelTest(
                name_of(test),
                StatementRequest(kind="statement", sql=sql, parameters=parameters),
            )
        )
    return found
