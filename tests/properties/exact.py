"""Generated exact expressions and their oracle (#426): a field over exact columns, the model
that sums it, the rows it reads, and the value a pure-Python `Decimal` evaluation gives.

The expressions use every operator the engine admits on exact values: `+`, `-`, `*`, `%`, unary
minus, `ABS`, `COALESCE`, `NULLIF`, `CASE` over a comparison, exact literals, and `/`, which
the engine must refuse (an exact quotient is only C2's declared division). `NULL`, zeros,
negatives and wide values come from adversarial pools. The oracle runs at 80 digits, above the
76 of the engine's widened arithmetic, so it never rounds where the engine must not.
"""

from __future__ import annotations

import decimal
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

from hypothesis import settings
from hypothesis import strategies as st
from orvex.semantic.compiler.catalog import SourceColumn
from orvex.semantic.compiler.compiled import CompiledModel, compile_model
from orvex.semantic.ossie.reader import read
from orvex.store.lake import Lake

from conformance.runners import corpus_execution as ce
from tests.properties.world import NIGHTLY, PROFILE
from tests.support.policies import VENDORS, extension

# More examples than the shared profile: each generated tree is a new model, so the shapes need
# room; still deterministic in CI and wide in the nightly run.
EXACT = settings(PROFILE, max_examples=400 if NIGHTLY else 30)

ORACLE = decimal.Context(
    prec=80, rounding=decimal.ROUND_HALF_EVEN, traps=[decimal.InvalidOperation]
)
CATALOG: Mapping[str, Mapping[str, SourceColumn]] = {
    "t": {
        "id": SourceColumn("INTEGER", nullable=False),
        "x": SourceColumn("DECIMAL(18,6)"),
        "y": SourceColumn("DECIMAL(18,6)"),
        "i": SourceColumn("INTEGER"),
        "j": SourceColumn("INTEGER"),
    }
}
COLUMNS = ("x", "y", "i", "j")
DECIMALS = ("0.000000", "1.000000", "-1.000000", "0.333333", "-2.500000", "999999999999.999999")
INTEGERS = ("0", "1", "-1", "2", "-7", "2147483647", "-2147483648")
LITERALS = ("0", "2", "-3", "0.5", "1.000001", "0.333333333333333333")
Value = Decimal | None


# The generated tree is kept as nested tuples and rendered twice, once to SQL and once to the
# oracle, so the two can never disagree about what was generated.
Node = tuple[Any, ...]


def leaves() -> st.SearchStrategy[Node]:
    return st.one_of(
        st.sampled_from(COLUMNS).map(lambda c: ("col", c)),
        st.sampled_from(LITERALS).map(lambda v: ("lit", v)),
    )


def extend(children: st.SearchStrategy[Node]) -> st.SearchStrategy[Node]:
    pair = st.tuples(children, children)
    return st.one_of(
        st.tuples(st.sampled_from(("+", "-", "*", "%", "/")), children, children).map(
            lambda t: ("bin", *t)
        ),
        children.map(lambda c: ("neg", c)),
        children.map(lambda c: ("abs", c)),
        st.tuples(children, st.sampled_from(LITERALS)).map(lambda t: ("coalesce", t[0], t[1])),
        pair.map(lambda t: ("nullif", *t)),
        st.tuples(st.sampled_from(("<", "=", ">")), children, children, children, children).map(
            lambda t: ("case", *t)
        ),
    )


def trees() -> st.SearchStrategy[Node]:
    return st.recursive(leaves(), extend, max_leaves=5)


def sql_of(node: Node) -> str:
    kind = node[0]
    if kind == "col":
        return str(node[1])
    if kind == "lit":
        return str(node[1])
    if kind == "bin":
        return f"({sql_of(node[2])} {node[1]} {sql_of(node[3])})"
    if kind == "neg":
        return f"(-{sql_of(node[1])})"
    if kind == "abs":
        return f"ABS({sql_of(node[1])})"
    if kind == "coalesce":
        return f"COALESCE({sql_of(node[1])}, {node[2]})"
    if kind == "nullif":
        return f"NULLIF({sql_of(node[1])}, {sql_of(node[2])})"
    if kind == "case":
        _, op, left, right, then, otherwise = node
        condition = f"{sql_of(left)} {op} {sql_of(right)}"
        return f"(CASE WHEN {condition} THEN {sql_of(then)} ELSE {sql_of(otherwise)} END)"
    raise AssertionError(kind)


def divides(node: Node) -> bool:
    return (node[0] == "bin" and node[1] == "/") or any(
        divides(child) for child in node[1:] if isinstance(child, tuple)
    )


def shape(node: Node) -> tuple[int, int]:
    """(precision, scale) of the engine's exact type for `node`, by DuckDB's rules: an
    `INTEGER` column or a small literal is `DECIMAL(10,0)` wide, a `DECIMAL(18,6)` column is
    itself; `+`/`-` widen by one digit, `*` adds precisions and scales, every result capped at
    38 digits. A value outside its type is an engine error, never a wrapped answer."""
    kind = node[0]
    if kind == "col":
        return (18, 6) if node[1] in ("x", "y") else (10, 0)
    if kind == "lit":
        _, digits, exponent = Decimal(str(node[1])).as_tuple()
        assert isinstance(exponent, int)
        places = max(-exponent, 0)
        return (max(len(digits), places, 10 if places == 0 else 1), places)
    shapes = [shape(c) for c in node[1:] if isinstance(c, tuple)]
    if kind == "coalesce":
        shapes.append(shape(("lit", node[2])))
    if kind == "case":
        shapes = shapes[2:]  # the branches; the comparison's sides are not the result
    places = max(s for _, s in shapes)
    whole = max(p - s for p, s in shapes)
    if kind == "bin" and node[1] == "*":
        places = sum(s for _, s in shapes)
        return (min(sum(p for p, _ in shapes), 38), places)
    if kind == "bin" and node[1] in ("+", "-"):
        whole += 1
    return (min(whole + places, 38), places)


def bits(node: Node) -> int | None:
    """The width of an integer-only node: DuckDB keeps `INTEGER` arithmetic in 32 bits and
    stops past them; None when a decimal takes part."""
    kind = node[0]
    if kind == "col":
        return None if node[1] in ("x", "y") else 32
    if kind == "lit":
        value = Decimal(str(node[1]))
        return None if value != value.to_integral_value() or "." in str(node[1]) else 32
    children = [c for c in node[1:] if isinstance(c, tuple)]
    if kind == "coalesce":
        children.append(("lit", node[2]))
    if kind == "case":
        children = children[2:]
    widths = [bits(c) for c in children]
    return None if any(w is None for w in widths) else max(w for w in widths if w is not None)


def fits(node: Node, value: Value) -> bool:
    if value is None:
        return True
    width = bits(node)
    if width is not None:
        return bool(-(2 ** (width - 1)) <= value < 2 ** (width - 1))
    precision, places = shape(node)
    return abs(value) < Decimal(10) ** (precision - places)


class Undefined(ArithmeticError):
    """A value the data makes undefined: a zero divisor, or a value past 38 digits at its
    scale. The engine must stop with an error, never answer."""


def evaluate(node: Node, row: Mapping[str, Value]) -> Value:
    """The oracle: SQL semantics in `Decimal` at 80 digits (NULL propagates; `%` truncates
    toward zero, as both engines do). An undefined value raises `Undefined`."""
    value = _value(node, row)
    if not fits(node, value):
        raise Undefined(node)
    return value


def _value(node: Node, row: Mapping[str, Value]) -> Value:
    kind = node[0]
    if kind == "col":
        return row[str(node[1])]
    if kind == "lit":
        return Decimal(str(node[1]))
    if kind == "neg":
        value = evaluate(node[1], row)
        return None if value is None else ORACLE.minus(value)
    if kind == "abs":
        value = evaluate(node[1], row)
        return None if value is None else ORACLE.abs(value)
    if kind == "coalesce":
        value = evaluate(node[1], row)
        return Decimal(str(node[2])) if value is None else value
    if kind == "nullif":
        left, right = evaluate(node[1], row), evaluate(node[2], row)
        return None if left is not None and right is not None and left == right else left
    if kind == "case":
        _, op, left, right, then, otherwise = node
        a, b = evaluate(left, row), evaluate(right, row)
        holds = a is not None and b is not None and {"<": a < b, "=": a == b, ">": a > b}[op]
        return evaluate(then if holds else otherwise, row)
    if kind == "bin":
        a, b = evaluate(node[2], row), evaluate(node[3], row)
        if a is None or b is None:
            return None
        if node[1] == "+":
            return ORACLE.add(a, b)
        if node[1] == "-":
            return ORACLE.subtract(a, b)
        if node[1] == "*":
            return ORACLE.multiply(a, b)
        if node[1] == "%":
            if b == 0:
                raise Undefined(node)
            return ORACLE.remainder(a, b)  # the sign of the dividend, as SQL's `%`
        raise AssertionError("an exact `/` is never evaluated: the engine refuses it")
    raise AssertionError(kind)


def total(node: Node, rows: list[dict[str, Value]]) -> Value:
    """`SUM` of the expression over the rows: NULL over no non-NULL value."""
    values = [v for v in (evaluate(node, r) for r in rows) if v is not None]
    if not values:
        return None
    found = Decimal(0)
    for value in values:
        found = ORACLE.add(found, value)
    if abs(found) >= Decimal(10) ** (38 - shape(node)[1]):
        raise Undefined(node)  # past the sum's DECIMAL(38, s)
    return found


def rows() -> st.SearchStrategy[list[dict[str, str | None]]]:
    decimals = st.one_of(st.none(), st.sampled_from(DECIMALS))
    integers = st.one_of(st.none(), st.sampled_from(INTEGERS))
    row = st.fixed_dictionaries({"x": decimals, "y": decimals, "i": integers, "j": integers})
    return st.lists(row, min_size=1, max_size=6)


def model(node: Node) -> CompiledModel:
    """The model summing the generated field; compiling it may refuse it (`SemanticError`)."""
    document = {
        "version": "0.2.0.dev0",
        "name": "generated",
        "datasets": [
            {
                "name": "t",
                "source": "t",
                "primary_key": ["id"],
                "fields": [{"name": c, "expression": {"sql": c}} for c in ("id", *COLUMNS)]
                + [{"name": "e", "expression": {"sql": sql_of(node)}}],
            }
        ],
        "metrics": [
            {
                "name": "total",
                "expression": {"sql": "SUM(t.e)"},
                "custom_extensions": [extension({"element": "metric", "additivity": "additive"})],
            }
        ],
        "custom_extensions": [extension({"element": "model", "default_time_zone": "UTC"})],
    }
    loaded = read(json.dumps(document).encode(), "semantic/generated.ossie.json", VENDORS)
    return compile_model(loaded, CATALOG, VENDORS)


def typed(generated: list[dict[str, str | None]]) -> list[dict[str, Value]]:
    return [{k: None if v is None else Decimal(v) for k, v in r.items()} for r in generated]


@contextmanager
def lake(folder: Path, generated: list[dict[str, str | None]]) -> Iterator[Lake]:
    path = folder / "t.csv"
    lines = [",".join(CATALOG["t"])]
    lines += [
        ",".join([str(n), *("" if r[c] is None else str(r[c]) for c in COLUMNS)])
        for n, r in enumerate(generated, 1)
    ]
    path.write_text("\n".join(lines) + "\n", "utf-8")
    with ce.case_lake([path], CATALOG) as opened:
        yield opened


QUESTION = json.dumps(
    {
        "kind": "semantic_query",
        "version": 1,
        "mode": "aggregate",
        "metrics": [{"metric": "total"}],
    }
)
