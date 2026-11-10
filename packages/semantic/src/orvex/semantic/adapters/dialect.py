"""The SQL each dialect spells its own way, as DuckDB spells it (P2-31, #225): the hooks the
DuckDB adapter's writer inherits and another dialect's adapter overrides, never the shape of the
statement, with the quoting and the exact division they write."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from orvex.semantic.compiler.compiled import CompiledModel, nonzero_literal, zero_literal
from orvex.semantic.compiler.parsing import MemberId
from orvex.semantic.compiler.typecheck import NodeType, TypedNode
from orvex.semantic.errors import SemanticError
from orvex.semantic.ossie import sql
from orvex.semantic.result import ColumnSpec


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def table(source: str) -> str:
    """A dataset's source table as SQL: `table` (schema `main`) or `schema.table`, each part an
    identifier of its own (#359), as the store resolves it and the compiler admits it."""
    return ".".join(quote(part) for part in source.split("."))


def _unsupported() -> SemanticError:
    return SemanticError("UNSUPPORTED_OPERATION", {"operation": "expression"})


def _power(exponent: int) -> str:
    """10^exponent as an exact `HUGEINT`; past its range, the cast fails, never a float."""
    return f"CAST('1{'0' * exponent}' AS HUGEINT)"


def _unscaled(value: str, scale: int) -> str:
    """The exact integer behind a value of declared scale `scale`, as a `HUGEINT`: the value at
    exactly that scale (`DECIMAL(38, scale)`, whatever scale the engine computed it in; past the
    type, the cast fails), written as text with exactly `scale` fraction digits, the point
    removed. No arithmetic step, so no scale overflows a `DECIMAL(38)` on the way (#426)."""
    if scale == 0:
        return f"CAST({value} AS HUGEINT)"
    fixed = f"CAST({value} AS DECIMAL(38,{scale}))"
    return f"CAST(replace(CAST({fixed} AS VARCHAR), '.', '') AS HUGEINT)"


def _rounded(a: str, b: str, mode: str, quotient: str | None = None) -> str:
    """The quotient a / b rounded to an integer in `mode`, from truncating integer division
    (`quotient`, the dialect's) and its remainder; written so that no step can exceed the
    operands' range."""
    quotient, rest = quotient or f"({a} // {b})", f"({a} % {b})"
    away = f"(CASE WHEN ({a} < 0) <> ({b} < 0) THEN -1 ELSE 1 END)"
    half = f"abs({rest}) >= abs({b}) - abs({rest})"
    above = f"abs({rest}) > abs({b}) - abs({rest})"
    tie = f"abs({rest}) = abs({b}) - abs({rest})"
    if mode == "down":
        step = "0"
    elif mode == "up":
        step = f"CASE WHEN {rest} <> 0 THEN {away} ELSE 0 END"
    elif mode == "half_away_from_zero":
        step = f"CASE WHEN {half} THEN {away} ELSE 0 END"
    elif mode == "half_even":
        step = f"CASE WHEN {above} OR ({tie} AND {quotient} % 2 <> 0) THEN {away} ELSE 0 END"
    else:
        raise _unsupported()
    return f"({quotient} + {step})"


def _nodes(tree: object) -> list[TypedNode]:
    pending = [tree] if isinstance(tree, TypedNode) else []
    found: list[TypedNode] = []
    while pending:
        node = pending.pop()
        found.append(node)
        pending.extend(node.children)
    return found


class DuckDBDialect:
    """Each hook writes what DuckDB writes."""

    def adopt(self, compiled: CompiledModel) -> None:
        """The model the statements are written for, and the type of each of its expression
        nodes, by identity (its compiled trees)."""
        self.compiled = compiled
        self.types: dict[int, NodeType] = {}
        self.zeros: dict[int, str] = {}  # each `/` node: its member's `zero_denominator`
        payloads = {
            e.pointer.rsplit("/custom_extensions/", 1)[0]: e.payload
            for e in compiled.ossie.product_extensions
        }
        for member in compiled.members.values():
            policy = payloads.get(member.parsed.pointer, {}).get("zero_denominator")
            for tree in (member.typed.tree, *(t for _, t in member.typed.payload)):
                for found in _nodes(tree):
                    self.types[id(found.node)] = found.type
                    if isinstance(found.node, sql.Binary) and found.node.op == "/" and policy:
                        self.zeros[id(found.node)] = str(policy)

    def binary(self, node: sql.Binary, op: str, again: Callable[[sql.Node], str]) -> str:
        """`left op right`. An exact product is computed at 38 digits and cast back to its
        declared type: DuckDB multiplies two `DECIMAL(18)` values in 18-digit arithmetic and
        stops on a product the type holds. A float quotient follows its zero policy: a
        non-zero literal needs none, `null` divides by `NULLIF(d, 0)` (never an infinity), and
        anything else is refused (#426)."""
        left, right = again(node.left), again(node.right)
        if op == "%" and zero_literal(node.right):
            raise _unsupported()  # never a value: an error on every row, NULL ones on one engine
        if op == "/":
            if nonzero_literal(node.right):
                return f"({left} / {right})"
            if self.zeros.get(id(node)) != "null":
                raise _unsupported()
            return f"({left} / {self.zero_as_null(right)})"
        kinds = [self.types.get(id(n)) for n in (node.left, node.right)]
        families = {k.logical.datatype if k is not None else None for k in kinds}
        typed = self.types.get(id(node))
        if op == "*" and "Decimal" in families and families <= {"Decimal", "Integer"}:
            scales = [int(k.logical.scale or 0) if k is not None else 0 for k in kinds]
            wide = (
                f"CAST({v} AS DECIMAL(38,{s}))" for v, s in zip((left, right), scales, strict=True)
            )
            product = "({} * {})".format(*wide)
            if typed is None or typed.logical.precision is None or typed.logical.scale is None:
                raise _unsupported()
            return f"CAST({product} AS DECIMAL({typed.logical.precision},{typed.logical.scale}))"
        return f"({left} {op} {right})"

    def zero_as_null(self, value: str) -> str:
        """`value`, or NULL when it is zero."""
        return f"NULLIF({value}, 0)"

    def typed_as(self, node: sql.Node, value: sql.Node, again: Callable[[sql.Node], str]) -> str:
        """A value of a `CASE` or `COALESCE` at the alternative's declared exact type: DuckDB
        unifies decimals past 38 digits by cutting their scale, which rounds a value silently
        (0.333333333333333333 became 0.33333333333333). Past the type, the cast fails (#426)."""
        typed = self.types.get(id(node))
        coalesce = isinstance(node, sql.Call) and node.name.upper() == "COALESCE"
        if (
            not (isinstance(node, sql.Case) or coalesce)
            or typed is None
            or typed.logical.datatype != "Decimal"
            or typed.logical.precision is None
            or typed.logical.scale is None
        ):
            return again(value)
        kind = f"DECIMAL({typed.logical.precision},{typed.logical.scale})"
        return f"CAST({again(value)} AS {kind})"

    def divisible(self, node: sql.Binary) -> bool:
        """`/` is written only for a float quotient on every engine (a bare literal takes its
        partner's exactness): an exact quotient is C2's one division, never `/` (#426)."""
        typed = self.types.get(id(node))
        return node.op != "/" or (typed is not None and typed.logical.datatype == "Float")

    def column_ref(self, alias: str, dataset: str, name: str) -> str:
        """A source column of `dataset`, read from the row `alias`."""
        return f"{quote(alias)}.{quote(name)}"

    def table_sql(self, source: str) -> str:
        return table(source)

    def cast_to(self, expression: str, kind: str) -> str:
        """`expression` as the store type `kind` (`contracts/store/v1`)."""
        return f"CAST({expression} AS {kind})"

    def sum_of(self, expression: str, member: MemberId) -> str:
        """The sum of a stored column `member` (C8)."""
        return f"SUM({expression})"

    def null_of(self, kind: str) -> str:
        """A `NULL` of the store type `kind`."""
        return f"CAST(NULL AS {kind})"

    def running_sum(self, column: str, window: str, kind: str) -> str:
        """The sum of the aggregated column `column` (of the store type `kind`) over `window`."""
        return f"SUM({column}) OVER ({window})"

    def weighted_sum(self, value: str, weight: str, scales: tuple[int, int]) -> str:
        """`SUM(value * weight)`, exact: each operand widened first, the product at both scales
        (C7); an overflow is an error."""
        left, right = (
            f"CAST(({v}) AS DECIMAL(38,{s}))" for v, s in zip((value, weight), scales, strict=True)
        )
        product = f"{left} * {right}"
        return f"SUM(CAST({product} AS DECIMAL(38,{sum(scales)})))"

    def latest_first(self, axis: str) -> str:
        return f"{axis} DESC"

    def list_text(self, values: Any) -> str:  # noqa: ANN401
        return json.dumps(list(values))

    def bound_list(self, listed: str, kind: str) -> str:
        """The values of the bound JSON list `listed`, each of the type `kind`."""
        return f"(SELECT unnest(CAST(CAST({listed} AS JSON) AS {kind}[])))"

    def membership(self, field: str, values: str, negated: bool) -> str:
        return f"({field} {'NOT ' if negated else ''}IN {values})"

    def power(self, exponent: int) -> str:
        return _power(exponent)

    def unscaled(self, value: str, scale: int) -> str:
        return _unscaled(value, scale)

    def scaled(self, value: str, exponent: int) -> str:
        """`value`, an exact integer, times 10^exponent; an overflow is an error."""
        return f"{value} * {self.power(exponent)}"

    def rounded(self, a: str, b: str, mode: str) -> str:
        return _rounded(a, b, mode)

    def shifted(self, value: str, scale: int) -> str:
        """An integer-valued `DECIMAL(38,0)` moved `scale` digits to the right of the point."""
        return f"{value} * 0.{'0' * (scale - 1)}1" if scale else value

    def ordered(
        self,
        text: str,
        columns: list[ColumnSpec],
        checks: list[tuple[str, str]],
        order: Any,  # noqa: ANN401
        limit: int | None,
    ) -> str:
        """The statement's rows in `order` (none: as they come), cut to `limit` and one more."""
        if order:
            text += " ORDER BY " + ", ".join(
                f"{quote(s.name)} {'DESC' if s.descending else 'ASC'} "
                f"NULLS {'FIRST' if s.nulls_first else 'LAST'}"
                for s in order
            )
        if limit is not None:
            text += f" LIMIT {int(limit) + 1}"  # one more row says the page is truncated
        return text
