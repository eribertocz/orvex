"""The ClickHouse statement of a plan (P2-31, #225; ADR-0027 §8): the statement the DuckDB
adapter writes, with the same shape, and every difference of meaning encoded explicitly. It
never reads data and never transpiles.

- **Identifiers.** ClickHouse matches names with their case: each source column is written
  with its exact name in the engine's catalog, never the model's normalized one.
- **Values.** Every value is a typed placeholder `{pN:Type}` (`Nullable(Type)` for a `NULL`).
  A model's decimal constant is a `Decimal`, never a float (ClickHouse reads `0.5` as one).
- **Exact arithmetic.** Every exact operand of `+`, `-` and `*` is carried in 76 digits
  (`exact.wide`), and every value that may leave its declared type (the type checker's
  overflow obligation: arithmetic, `SUM`, a narrowing `CAST`) is returned to it by
  `exact.checked`, which stops the statement past the type's range, as the reference does. A
  sum accumulates in 256 bits. A product whose operands could exceed 76 digits together is
  refused. `/` between exact values is never written: a ratio is C2's exact division, with
  `intDiv`, the remainder, and each step checked against `HUGEINT`.
- **`NULL`.** A `sum` over no row or only `NULL`s is `NULL`, written as such (ClickHouse gives
  `0` for no row). `IN` and `NOT IN` of an expression are written with SQL's three-valued
  meaning: as `=` comparisons for a list, and, for a bound set, `NULL` membership explicit
  (ClickHouse's `2 NOT IN (1, NULL)` is true). A filter's values are never `NULL`, and its
  `NULL` rows are written apart, so its `IN` keeps the reference's form.
- **Order.** `NULLS FIRST` or `NULLS LAST` is always written.
- **Dates.** A date bucket is computed in `Date32` (`toStartOf*` answers a 16-bit `Date`).
- **The result.** Each column is converted last to its exact store form, exact types checked:
  `HUGEINT`, `UHUGEINT` and `UUID` as text, `TIMESTAMP WITH TIME ZONE` with microseconds in
  UTC, every other type by an explicit `CAST`; the order is that of the unconverted values.
- **Capabilities.** `CAPABILITIES`; a plan needing any other is refused before anything is
  written (`UNSUPPORTED_OPERATION`, naming it), as P2-29 defines. `LOWER` and `UPPER` are
  refused: ClickHouse's Unicode case mapping is not DuckDB's (`ß`, a final sigma).

The engine's profile is verified at connection by the store (`join_use_nulls = 1`,
`transform_null_in = 0`, `cast_keep_nullable = 1`, and the rest of its table); this adapter
relies on those values and writes no `SETTINGS`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from orvex.semantic.adapters.clickhouse.allowlist import check_statement
from orvex.semantic.adapters.clickhouse.exact import (
    DECIMAL,
    WIDE_BITS,
    canonical,
    checked,
    constant,
    engine_type,
    exact_kind,
    magnitude_bits,
    wide,
)
from orvex.semantic.adapters.clickhouse.names import quote_name
from orvex.semantic.adapters.dialect import _rounded, _unsupported, quote
from orvex.semantic.adapters.duckdb import (
    LEVEL,
    PhysicalQuery,
    PolicyValues,
    _Writer,
    encoding,
    number_parameter,
)
from orvex.semantic.catalog.building import _logical
from orvex.semantic.compiler.catalog import LogicalType, column_key
from orvex.semantic.compiler.parsing import MemberId
from orvex.semantic.compiler.typecheck import NodeType, TypedNode
from orvex.semantic.engines import required
from orvex.semantic.errors import PlanError, SemanticError
from orvex.semantic.ossie import sql
from orvex.semantic.planning import ISSUED_PLAN, SemanticPlan
from orvex.semantic.result import ColumnSpec

NAME = "clickhouse"
VERSION = "1"
# What this adapter runs (`engines.CAPABILITIES`); the rest lands with #225 PR 3.
CAPABILITIES = frozenset({"count_distinct", "exact_decimal"})

_EXACT = frozenset({"Integer", "Decimal"})
_DATE_BUCKETS = {
    "day": "{d}",
    "week": "({d} - (toDayOfWeek({d}) - 1))",  # Monday weeks
    "month": "makeDate32(toYear({d}), toMonth({d}), 1)",
    "quarter": "makeDate32(toYear({d}), intDiv(toMonth({d}) - 1, 3) * 3 + 1, 1)",
    "year": "makeDate32(toYear({d}), 1, 1)",
}
_HUGEINT_DIGITS = 38  # 10^38 is the largest power of ten a `HUGEINT` holds


def _scale(node: NodeType | None) -> int | None:
    """The scale an exact value keeps, or None when it is not exact."""
    if node is None or node.logical.datatype not in _EXACT:
        return None
    return int(node.logical.scale or 0) if node.logical.datatype == "Decimal" else 0


def _kind(node: NodeType) -> str:
    """The store type of a typed node (`semantic-result`'s mapping table)."""
    return encoding(dict(_logical(node.logical)))


class _ClickHouse(_Writer):
    def __init__(
        self,
        plan: SemanticPlan,
        columns: Mapping[str, Iterable[str]],
        values: Mapping[int, PolicyValues],
    ) -> None:
        super().__init__(plan, values)
        # Each table's columns, by DuckDB's identity of a name (the compiler's), exact.
        self.names: dict[str, dict[str, str]] = {}
        for source, names in columns.items():
            exact: dict[str, str] = {}
            for name in names:
                if column_key(name) in exact:
                    raise PlanError("two columns of one ClickHouse table differ only in case")
                exact[column_key(name)] = name
            self.names[source] = exact
        # The type of every expression node of the model, by identity (the compiled trees).
        self.types: dict[int, NodeType] = {}
        for member in plan.compiled.members.values():
            trees = [member.typed.tree, *(v for _, v in member.typed.payload)]
            for tree in trees:
                if isinstance(tree, TypedNode):
                    self.index(tree)

    def index(self, tree: TypedNode) -> None:
        self.types[id(tree.node)] = tree.type
        for child in tree.children:
            self.index(child)

    # hooks

    def bind(self, kind: str, value: str | None) -> str:
        name = f"p{len(self.parameters)}"
        written = engine_type(kind)
        self.parameters.append({"name": name, "type": kind, "value": value})
        return "{" + name + ":" + (f"Nullable({written})" if value is None else written) + "}"

    def column_ref(self, alias: str, dataset: str, name: str) -> str:
        source = self.tables.get(dataset)
        exact = self.names.get(source, {}).get(column_key(name)) if source else None
        if exact is None:
            raise PlanError("a column the ClickHouse catalog does not hold")
        return f"{quote(alias)}.{quote_name(exact)}"

    def table_sql(self, source: str) -> str:
        return ".".join(quote_name(part) for part in source.split("."))

    def cast_to(self, expression: str, kind: str) -> str:
        return checked(expression, kind)

    def sum_of(self, expression: str, member: MemberId) -> str:
        logical = self.compiled.require(member).typed.type.logical
        return self.total(self.accumulated(expression, logical.datatype, logical.scale), None)

    def accumulated(self, value: str, datatype: str | None, scale: int | None) -> str:
        """A summed value in 256 bits: no sum of the plan's sizes wraps there."""
        if datatype == "Integer":
            return f"toInt256({value})"
        if datatype == "Decimal":
            return wide(value, int(scale or 0))
        return value

    def total(self, value: str, where: str | None) -> str:
        """`sum`, `NULL` over no row or only `NULL`s: ClickHouse's own gives `0` for no row."""
        if where is None:
            return f"if(count({value}) = 0, NULL, sum({value}))"
        return f"if(countIf({value}, {where}) = 0, NULL, sumIf({value}, {where}))"

    def latest_first(self, axis: str) -> str:
        return f"{axis} DESC NULLS LAST"

    def list_text(self, values: Any) -> str:  # noqa: ANN401
        return json.dumps([None if v is None else str(v) for v in values])

    def bound_list(self, listed: str, kind: str) -> str:
        return (
            f"CAST(JSONExtract({listed}, 'Array(Nullable(String))') AS "
            f"Array(Nullable({engine_type(kind)})))"
        )

    def membership(self, field: str, values: str, negated: bool) -> str:
        """`field IN values` with SQL's meaning: false for an empty set, `NULL` for a `NULL`
        field, or for a set holding `NULL` without the field."""
        test = (
            f"multiIf(empty({values}), false, {field} IS NULL, NULL, has({values}, {field}), "
            f"true, has({values}, NULL), NULL, false)"
        )
        return f"(NOT {test})" if negated else f"({test})"

    def power(self, exponent: int) -> str:
        if exponent > _HUGEINT_DIGITS:  # past `HUGEINT`, as the reference's cast fails
            return checked(constant(10**exponent), "HUGEINT")
        return f"CAST('1{'0' * exponent}' AS Int128)"

    def unscaled(self, value: str, scale: int) -> str:
        return checked(f"({wide(value, scale)} * {constant(10**scale)})", "HUGEINT")

    def scaled(self, value: str, exponent: int) -> str:
        return checked(f"(toInt256({value}) * toInt256({self.power(exponent)}))", "HUGEINT")

    def rounded(self, a: str, b: str, mode: str) -> str:
        return _rounded(a, b, mode, f"intDiv({a}, {b})")

    def shifted(self, value: str, scale: int) -> str:
        if not scale:
            return value
        return f"{value} * CAST('0.{'0' * (scale - 1)}1' AS Decimal(38, {scale}))"

    def ordered(
        self,
        text: str,
        columns: list[ColumnSpec],
        checks: list[tuple[str, str]],
        order: Any,  # noqa: ANN401
        limit: int | None,
    ) -> str:
        """Every column in its exact store form, over the rows ordered by their own values."""
        names = [(c.name, c.encoding) for c in columns] + [(n, "BOOLEAN") for n, _ in checks]
        names += [(n, "BIGINT") for n, _ in [*self.weights, *self.as_of]]
        names += [(LEVEL, "INTEGER")] if self.plan.totals is not None else []
        row = quote("#r")
        for name, _ in names:
            if "\\" in name:
                raise _unsupported()
        converted = ", ".join(
            f"{_converted(f'{row}.{quote(name)}', kind)} AS {quote(name)}" for name, kind in names
        )
        text = f"SELECT {converted} FROM ({text}) AS {row}"
        if order:
            text += " ORDER BY " + ", ".join(
                f"{row}.{quote(s.name)} {'DESC' if s.descending else 'ASC'} "
                f"NULLS {'FIRST' if s.nulls_first else 'LAST'}"
                for s in order
            )
        if limit is not None:
            text += f" LIMIT {int(limit) + 1}"
        return text

    # expressions

    def node(self, node: sql.Node, alias: str, dataset: str, context: str) -> str:
        def again(child: sql.Node) -> str:
            return self.node(child, alias, dataset, context)

        if isinstance(node, sql.Number):
            if not re.fullmatch(r"[0-9]+(\.[0-9]+)?", node.text):
                raise _unsupported()
            if "." not in node.text:
                return node.text
            return f"CAST('{node.text}' AS {engine_type(number_parameter(node.text))})"
        if isinstance(node, sql.Unary) and node.op in ("-", "+"):
            return self.declared(node, f"({node.op}{self.exact(node.operand, again)})")
        if isinstance(node, sql.Binary) and node.op in ("+", "-", "*"):
            left, right = self.types.get(id(node.left)), self.types.get(id(node.right))
            if (
                left is not None
                and right is not None
                and _scale(left) is not None
                and (_scale(right) is not None)
            ):
                if node.op == "*" and _bits(left) + _bits(right) > WIDE_BITS:
                    raise _unsupported()  # a product past 76 digits would wrap
                written = (self.exact(node.left, again), self.exact(node.right, again))
            elif left is not None and right is not None:
                written = (again(node.left), again(node.right))  # a float's own arithmetic
            else:
                raise _unsupported()
            return self.declared(node, f"({written[0]} {node.op} {written[1]})")
        if isinstance(node, sql.Binary) and node.op == "/":
            sides = (self.types.get(id(node.left)), self.types.get(id(node.right)))
            if not all(s is not None and s.logical.datatype == "Float" for s in sides):
                raise _unsupported()  # an exact quotient is C2's division, never `/`
            return f"({again(node.left)} / {again(node.right)})"
        if isinstance(node, sql.InList):
            operand = again(node.operand)
            equal = " OR ".join(f"{operand} = {again(i)}" for i in node.items) or "false"
            return f"(NOT ({equal}))" if node.negated else f"({equal})"
        return super().node(node, alias, dataset, context)

    def declared(self, node: sql.Node, written: str) -> str:
        """An exact value the type checker says may leave its type, checked back into it; an
        integer the reference computes in a narrower type is checked against that type."""
        typed = self.types.get(id(node))
        if typed is None or _scale(typed) is None:
            return written
        reference = self.integer_type(node)
        if reference is not None and _narrower(reference, typed.logical):
            return checked(written, encoding(dict(_logical(reference))))
        if not typed.overflow_check:
            return written
        return checked(written, _kind(typed))

    def integer_type(self, node: sql.Node) -> LogicalType | None:
        """The integer type the reference computes `node` in. DuckDB gives an integer literal
        the type of the integer it meets when it fits there (`TINYINT * 2` is a `TINYINT`),
        and otherwise the narrowest of `INTEGER`, `BIGINT` and `HUGEINT` that holds it; the
        result of `+`, `-` and `*` is the wider operand's type. The type checker's own type
        when the rule does not decide (mixed signs, or no integer)."""
        typed = self.types.get(id(node))
        if typed is None or typed.logical.datatype != "Integer":
            return None
        if isinstance(node, sql.Number):
            return _literal(int(node.text)) or typed.logical
        if isinstance(node, sql.Unary) and node.op in ("-", "+"):
            return self.integer_type(node.operand) or typed.logical
        if not (isinstance(node, sql.Binary) and node.op in ("+", "-", "*")):
            return typed.logical
        left, right = self.integer_type(node.left), self.integer_type(node.right)
        if left is None or right is None:
            return typed.logical
        if isinstance(node.left, sql.Number) and not isinstance(node.right, sql.Number):
            left = _adapted(int(node.left.text), right)
        elif isinstance(node.right, sql.Number) and not isinstance(node.left, sql.Number):
            right = _adapted(int(node.right.text), left)
        if left.signed != right.signed or left.width is None or right.width is None:
            return typed.logical
        return left if left.width >= right.width else right

    def cast(self, node: sql.Cast, again: Any, dataset: str, context: str) -> str:  # noqa: ANN401
        super().cast(node, again, dataset, context)  # the one cast with an exact meaning
        return self.cast_to(
            again(node.operand), f"DECIMAL({int(node.arguments[0])},{int(node.arguments[1])})"
        )

    def exact(self, node: sql.Node, again: Callable[[sql.Node], str]) -> str:
        """An exact value in 76 digits, so no step narrows or wraps it."""
        scale = _scale(self.types.get(id(node)))
        if scale is None:
            return again(node)
        return wide(again(node), scale)

    def call(self, node: sql.Call, again: Any, context: str, dataset: str) -> str:  # noqa: ANN401
        name = node.name.upper()
        distinct = node.distinct and name == "COUNT" and context == "metric"
        if node.order is not None or (node.distinct and not distinct):
            raise _unsupported()
        if name == "DATE_TRUNC":
            return super().call(node, again, context, dataset)
        if name in ("SUM", "COUNT") and context == "metric":
            arguments = node.arguments
            where = again(node.filter) if node.filter is not None else None
            if name == "COUNT" and len(arguments) == 1 and isinstance(arguments[0], sql.Star):
                return "count()" if where is None else f"countIf({where})"
            if len(arguments) != 1:
                raise _unsupported()
            value = again(arguments[0])
            if name == "SUM":
                kind = self.types.get(id(arguments[0]))
                if kind is None:
                    raise _unsupported()
                value = self.accumulated(value, kind.logical.datatype, kind.logical.scale)
                return self.declared(node, self.total(value, where))
            function = "uniqExact" if distinct else "count"
            if where is None:
                return f"{function}({value})"
            return f"{function}If({value}, {where})"
        if name in ("COALESCE", "NULLIF", "ABS") and node.filter is None:
            function = {"COALESCE": "coalesce", "NULLIF": "nullIf", "ABS": "abs"}[name]
            return f"{function}({', '.join(again(a) for a in node.arguments)})"
        raise _unsupported()  # LOWER and UPPER included: ClickHouse's case mapping differs

    def cut(self, unit: str, value: str, zone: str | None) -> str:
        if zone is not None:
            raise SemanticError("UNSUPPORTED_OPERATION", {"operation": "time_zone"})
        if unit not in _DATE_BUCKETS:
            raise SemanticError("UNSUPPORTED_OPERATION", {"operation": "bucket"})
        return _DATE_BUCKETS[unit].format(d=value)


_LITERALS = (32, 64, 128)  # DuckDB's integer literal types, narrowest first


def _literal(value: int) -> LogicalType | None:
    for width in _LITERALS:
        if value < 2 ** (width - 1):
            return LogicalType("Integer", width=width, signed=True)
    return None


def _adapted(value: int, other: LogicalType) -> LogicalType:
    """A literal's type beside the integer `other`: `other`'s when the literal fits there."""
    width = other.width or 0
    limit = 2**width if other.signed is False else 2 ** (width - 1)
    if value < limit:
        return other
    return _literal(value) or other


def _narrower(reference: LogicalType, declared: LogicalType) -> bool:
    return (
        declared.datatype == "Integer"
        and reference.signed == declared.signed
        and (reference.width or 0) < (declared.width or 0)
    )


def _bits(node: NodeType) -> int:
    return magnitude_bits(_kind(node))


def _converted(column: str, kind: str) -> str:
    """A result column in the exact form the store reads it in (`contracts/store/v1`), every
    exact value checked against its declared type."""
    kind = canonical(kind)
    if kind in ("HUGEINT", "UHUGEINT"):
        return f"toString({checked(column, kind)})"
    if kind == "UUID":
        return f"toString({column})"
    if kind == "TIMESTAMP WITH TIME ZONE":
        return f"toDateTime64({column}, 6, 'UTC')"
    found = DECIMAL.fullmatch(kind)
    if found is not None:  # the store reads every decimal at 38 digits, at its scale
        return f"CAST({checked(column, kind)} AS Decimal(38, {int(found.group(2))}))"
    if exact_kind(kind):
        return checked(column, kind)
    return f"CAST({column} AS {engine_type(kind)})"


def compile_plan(
    plan: SemanticPlan,
    columns: Mapping[str, Iterable[str]],
    values: Mapping[int, PolicyValues],
) -> PhysicalQuery:
    """The ClickHouse query of a plan the planner made. `columns`: each table's column names as
    the engine's catalog holds them; `values`: each mapping subquery's values, resolved on the
    managed lake (`mapping_queries`). A plan needing a capability this adapter lacks is refused
    with `UNSUPPORTED_OPERATION`, naming it, before anything is written."""
    if plan.issued is not ISSUED_PLAN:
        raise PlanError("not a plan of the planner")
    missing = sorted(required(plan) - CAPABILITIES)
    if missing:
        raise SemanticError("UNSUPPORTED_OPERATION", {"operation": missing[0]})
    query = _ClickHouse(plan, columns, values).statement()
    check_statement(query.sql, {table for table, _ in query.reads})
    return PhysicalQuery(
        query.sql,
        query.parameters,
        query.columns,
        query.plan_fingerprint,
        adapter=NAME,
        adapter_version=VERSION,
        checks=query.checks,
        zones=query.zones,
        weights=query.weights,
        as_of=query.as_of,
        hidden=query.hidden,
        sorts=query.sorts,
        level=query.level,
        reads=query.reads,
    )
