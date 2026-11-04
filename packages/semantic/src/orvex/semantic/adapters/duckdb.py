"""The DuckDB adapter (P2-12, #311): a `SemanticPlan` as one DuckDB `SELECT` with bound
parameters. It is the only stage that knows a dialect (`semantic-engine.md` §4).

- **Values are parameters only.** Every filter value is a `$pN` parameter with its exact type:
  a number as text, cast to the `DECIMAL(p,s)` that holds it exactly; a date or a timestamp as
  ISO text. A text literal of the model is a parameter too; only the model's own numbers,
  checked to be digits, are written in the text. The SQL text never holds a caller's value.
- **Identifiers** come only from the compiled model, always double-quoted, quotes doubled.
- **Only expressions with one meaning in DuckDB** are written: column and field references,
  numbers, the arithmetic and comparison operators, `AND`, `OR`, `NOT`, `IS [NOT] NULL`,
  `BETWEEN`, `IN`, `CASE`, `COALESCE`, `NULLIF`, `ABS`, `LOWER`, `UPPER`, `SUM` and `COUNT`.
  Anything else is refused (`UNSUPPORTED_OPERATION`, `operation: expression`) before any read.
- **The output** is described by the compiled members (`semantic-result` mapping table). A
  column DuckDB returns with another type is an internal error, never converted, so an exact
  value never passes through a float or a rounding.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from orvex.semantic.catalog.building import _logical
from orvex.semantic.compiler.compiled import CompiledModel
from orvex.semantic.compiler.parsing import MemberId
from orvex.semantic.errors import SemanticError
from orvex.semantic.ossie import sql
from orvex.semantic.planning import ISSUED_PLAN, PlanError, SemanticPlan
from orvex.semantic.result import ColumnSpec
from orvex.semantic.result.descriptors import INTEGERS, OTHERS, pair_allowed

NAME = "duckdb"
VERSION = "1"
_ARITHMETIC = frozenset({"+", "-", "*", "/", "%"})
_COMPARISON = frozenset({"=", "<>", "<", "<=", ">", ">="})
_LOGICAL = frozenset({"AND", "OR"})
_CALLS = frozenset({"COALESCE", "NULLIF", "ABS", "LOWER", "UPPER"})
_AGGREGATES = frozenset({"SUM", "COUNT"})
_BUCKETS = frozenset({"day", "month", "quarter", "year"})
_PARAMETER_TYPES = {
    "text": "VARCHAR",
    "date": "DATE",
    "timestamp": "TIMESTAMPTZ",
    "local_timestamp": "TIMESTAMP",
    "boolean": "BOOLEAN",
}


@dataclass(frozen=True, slots=True)
class PhysicalQuery:
    sql: str
    parameters: tuple[Mapping[str, str | None], ...]  # `store/v1` `Parameter`s
    columns: tuple[ColumnSpec, ...]
    plan_fingerprint: str
    adapter: str = NAME
    adapter_version: str = VERSION
    # Extra BOOLEAN columns after `columns`: (column, metric) for a ratio declared
    # `zero_denominator: reject`; any true row refuses the whole result.
    checks: tuple[tuple[str, str], ...] = ()


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _unsupported() -> SemanticError:
    return SemanticError("UNSUPPORTED_OPERATION", {"operation": "expression"})


def encoding(logical: Mapping[str, object]) -> str:
    """The `semantic-result` encoding of a logical type, from its mapping table."""
    family = logical.get("family")
    if family == "Decimal":
        return f"DECIMAL({logical.get('precision')},{logical.get('scale')})"
    for name in [*INTEGERS, *OTHERS]:
        if pair_allowed(logical, name) and not (name == "FLOAT" and family == "Float"):
            return name
    raise _unsupported()


def number_parameter(text: str) -> str:
    """The exact `DECIMAL(p,s)` of a canonical number."""
    try:
        value = Decimal(text)
    except InvalidOperation as error:
        raise _unsupported() from error
    _, digits, exponent = value.as_tuple()
    assert isinstance(exponent, int)
    scale = max(0, -exponent)
    precision = max(len(digits), scale, 1)
    if precision > 38:
        raise _unsupported()
    return f"DECIMAL({precision},{scale})"


class _Writer:
    def __init__(self, plan: SemanticPlan) -> None:
        self.plan = plan
        self.compiled: CompiledModel = plan.compiled
        self.parameters: list[dict[str, str | None]] = []
        self.dataset_alias = {s.alias: s.dataset for s in plan.sources}
        self.ratios = 0  # each ratio's own columns, numbered across every fact

    def bind(self, kind: str, value: str | None) -> str:
        name = f"p{len(self.parameters)}"
        self.parameters.append({"name": name, "type": kind, "value": value})
        return f"${name}"

    # members

    def field(self, member: MemberId, alias: str) -> str:
        tree = self.compiled.require(member).parsed.tree
        if tree is None:
            raise _unsupported()
        return self.node(tree, alias, member.dataset or "", "field")

    def metric(self, member: MemberId, alias: str) -> str:
        tree = self.compiled.require(member).parsed.tree
        if tree is None:
            raise _unsupported()
        return self.node(tree, alias, member.dataset or "", "metric")

    def node(self, node: sql.Node, alias: str, dataset: str, context: str) -> str:
        def again(child: sql.Node) -> str:
            return self.node(child, alias, dataset, context)

        if isinstance(node, sql.Ref):
            return self.ref(node, alias, dataset, context)
        if isinstance(node, sql.Number):
            if not re.fullmatch(r"[0-9]+(\.[0-9]+)?", node.text):
                raise _unsupported()
            return node.text  # a model constant, checked to be digits: never a caller's value
        if isinstance(node, sql.String):
            return self.bind("VARCHAR", node.value)
        if isinstance(node, sql.Boolean):
            return "TRUE" if node.value else "FALSE"
        if isinstance(node, sql.Null):
            return "NULL"
        if isinstance(node, sql.Unary) and node.op in ("-", "+"):
            return f"({node.op}{again(node.operand)})"
        if isinstance(node, sql.Binary):
            op = node.op.upper()
            if op not in _ARITHMETIC | _COMPARISON | _LOGICAL:
                raise _unsupported()
            return f"({again(node.left)} {op} {again(node.right)})"
        if isinstance(node, sql.Not):
            return f"(NOT {again(node.operand)})"
        if isinstance(node, sql.IsNull):
            return f"({again(node.operand)} IS {'NOT ' if node.negated else ''}NULL)"
        if isinstance(node, sql.Between):
            negated = "NOT " if node.negated else ""
            low, high = again(node.low), again(node.high)
            return f"({again(node.operand)} {negated}BETWEEN {low} AND {high})"
        if isinstance(node, sql.InList):
            items = ", ".join(again(i) for i in node.items)
            return f"({again(node.operand)} {'NOT ' if node.negated else ''}IN ({items}))"
        if isinstance(node, sql.Case):
            text = "(CASE"
            if node.operand is not None:
                text += f" {again(node.operand)}"
            for when, then in node.whens:
                text += f" WHEN {again(when)} THEN {again(then)}"
            if node.otherwise is not None:
                text += f" ELSE {again(node.otherwise)}"
            return text + " END)"
        if isinstance(node, sql.Cast):
            return self.cast(node, again, dataset, context)
        if isinstance(node, sql.Call):
            return self.call(node, again, context, dataset)
        raise _unsupported()

    def datatype(self, node: sql.Node, dataset: str, context: str) -> str | None:
        """The type of a reference to a field or a column, when the model knows it."""
        if not isinstance(node, sql.Ref):
            return None
        owner = node.parts[0] if len(node.parts) == 2 else dataset
        member = MemberId("field", owner, node.parts[-1])
        if context == "metric" and member in self.compiled.members:
            return self.compiled.require(member).typed.type.logical.datatype
        # A column: the type of the field of its dataset that is exactly that column.
        for found in self.compiled.members.values():
            exposes = found.parsed.tree == sql.Ref((node.parts[-1],))
            if found.id.kind == "field" and found.id.dataset == dataset and exposes:
                return found.typed.type.logical.datatype
        return None

    def cast(self, node: sql.Cast, again: Any, dataset: str, context: str) -> str:  # noqa: ANN401
        """Only a cast with one exact meaning: an integer widened to a `DECIMAL`."""
        if (
            node.type_name.upper() == "DECIMAL"
            and len(node.arguments) == 2
            and not node.safe
            and self.datatype(node.operand, dataset, context) == "Integer"
        ):
            precision, scale = node.arguments
            return f"CAST({again(node.operand)} AS DECIMAL({int(precision)},{int(scale)}))"
        raise _unsupported()

    def call(self, node: sql.Call, again: Any, context: str, dataset: str) -> str:  # noqa: ANN401
        name = node.name.upper()
        distinct = node.distinct and name == "COUNT" and context == "metric"
        if node.order is not None or (node.distinct and not distinct):
            raise _unsupported()
        if name == "DATE_TRUNC" and len(node.arguments) == 2 and node.filter is None:
            # The calendar truncation of a date is a date: DuckDB answers a TIMESTAMP at
            # midnight, so it is cast back. A truncation of an instant needs C6 (P2-19).
            part, operand = node.arguments
            unit = part.value.lower() if isinstance(part, sql.String) else None
            if unit in _BUCKETS and self.datatype(operand, dataset, context) == "Date":
                return f"CAST(date_trunc('{unit}', {again(operand)}) AS DATE)"
            raise _unsupported()
        if name in _AGGREGATES and context == "metric":
            arguments = node.arguments
            if name == "COUNT" and len(arguments) == 1 and isinstance(arguments[0], sql.Star):
                inner = "*"
            elif len(arguments) == 1:
                inner = ("DISTINCT " if distinct else "") + again(arguments[0])
            else:
                raise _unsupported()
            where = f" FILTER (WHERE {again(node.filter)})" if node.filter is not None else ""
            return f"{name}({inner}){where}"
        if name in _CALLS and node.filter is None:
            return f"{name}({', '.join(again(a) for a in node.arguments)})"
        raise _unsupported()

    def ref(self, node: sql.Ref, alias: str, dataset: str, context: str) -> str:
        """In a metric, a name is a field (and that field's expression is written); in a
        field, a name is always a source column of its own dataset (Ossie "Name Resolution"),
        never another field, even one with the same name."""
        parts = node.parts
        if context == "metric":
            owner, name = (parts[0], parts[1]) if len(parts) == 2 else (dataset, parts[0])
            member = MemberId("field", owner, name)
            if member not in self.compiled.members or self.dataset_alias.get(alias) != owner:
                raise _unsupported()  # a metric over another metric or dataset needs its rule
            return self.field(member, alias)
        if len(parts) == 2 and parts[0] != dataset:
            raise _unsupported()
        return f"{quote(alias)}.{quote(parts[-1])}"

    # predicates

    def value(self, value: Any, member: MemberId) -> str:  # noqa: ANN401
        kind, raw = value
        if kind == "number":
            text = str(raw)
            return f"CAST({self.bind(number_parameter(text), text)} AS {number_parameter(text)})"
        if kind not in _PARAMETER_TYPES:
            raise _unsupported()
        if kind == "boolean":
            return self.bind("BOOLEAN", "true" if raw is True or raw == "true" else "false")
        return self.bind(_PARAMETER_TYPES[kind], None if raw is None else str(raw))

    def predicate(self, column: str, predicate: Mapping[str, Any], member: MemberId) -> str:
        op = predicate["op"]
        if op == "is_null":
            return f"({column} IS NULL)"
        if op == "is_not_null":
            return f"({column} IS NOT NULL)"
        if op in ("in", "not_in"):
            items = ", ".join(self.value(v, member) for v in predicate["values"])
            if op == "in":
                null = f" OR {column} IS NULL" if predicate["include_null"] else ""
                return f"({column} IN ({items}){null})"
            if predicate["exclude_null"]:
                return f"({column} IS NOT NULL AND {column} NOT IN ({items}))"
            return f"({column} IS NULL OR {column} NOT IN ({items}))"
        if op == "range":
            bounds = {"gte": ">=", "gt": ">", "lte": "<=", "lt": "<"}
            terms = [
                f"{column} {sign} {self.value(predicate[key], member)}"
                for key, sign in bounds.items()
                if key in predicate
            ]
            return "(" + " AND ".join(terms) + ")"
        raise _unsupported()

    # the statement

    def column(self, member: MemberId, name: str, role: str, bucket: str | None) -> ColumnSpec:
        typed = self.compiled.require(member).typed.type
        logical = dict(_logical(typed.logical))
        if bucket is not None and logical["family"] != "Date":
            raise _unsupported()
        nullable = typed.nullable or (role == "dimension" and bool(self.plan.joins))
        return ColumnSpec(
            name=name,
            member={"name": member.name} | ({"dataset": member.dataset} if member.dataset else {}),
            role=role,
            encoding=encoding(logical),
            nullable=nullable or role == "metric",
            logical=logical,
            semantic_type=typed.semantic_type,
            exactness=typed.exactness,
            bucket={"unit": bucket} if bucket else None,
        )

    def fact(self, source: Any) -> str:  # noqa: ANN401
        """The fact's table, or, for a period-end metric, only each entity's latest row per
        period (C2): a ranking over the entity and the period, by the axis, newest first. The
        entity plus the axis is the key, so no two rows tie."""
        latest = source.latest
        if latest is None:
            return quote(source.table)
        axis = self.field(latest.axis, "s")
        period = axis
        if latest.bucket is not None:
            if latest.bucket not in _BUCKETS:
                raise SemanticError("UNSUPPORTED_OPERATION", {"operation": "bucket"})
            period = f"CAST(date_trunc('{latest.bucket}', {axis}) AS DATE)"
        partition = ", ".join([self.field(e, "s") for e in latest.entity] + [period])
        return (
            f"(SELECT * FROM {quote(source.table)} AS {quote('s')} QUALIFY row_number() OVER "
            f"(PARTITION BY {partition} ORDER BY {axis} DESC) = 1)"
        )

    def statement(self) -> PhysicalQuery:
        plan = self.plan
        parts = [self.aggregation(fact) for fact in plan.facts]
        if len(parts) == 1:
            text, columns, checks = parts[0]
        else:
            text, columns, checks = self.align(parts)
        if plan.order:
            text += " ORDER BY " + ", ".join(
                f"{quote(s.name)} {'DESC' if s.descending else 'ASC'} "
                f"NULLS {'FIRST' if s.nulls_first else 'LAST'}"
                for s in plan.order
            )
        if plan.limit is not None:
            text += f" LIMIT {int(plan.limit) + 1}"  # one more row says the page is truncated
        return PhysicalQuery(
            text, tuple(self.parameters), tuple(columns), plan.fingerprint, checks=tuple(checks)
        )

    def members(self, fact: str) -> set[str]:
        """The sources of one fact: its own, and every source its joins reach."""
        found = {fact}
        for join in self.plan.joins:  # in path order: a join's left side is added first
            if join.left in found:
                found.add(join.right)
        if any((j.left in found) != (j.right in found) for j in self.plan.joins):
            raise PlanError("a join reaches a source out of path order")
        return found

    def aggregation(self, fact: str) -> tuple[str, list[ColumnSpec], list[tuple[str, str]]]:
        """One fact aggregated on its own to the requested grain, with every filter (C3)."""
        plan = self.plan
        own = self.members(fact)
        select: list[str] = []
        grouped: list[str] = []
        columns: list[ColumnSpec] = []
        for group in (g for g in plan.groups if g.source in own):
            expression = self.field(group.member, group.source)
            if group.bucket is not None:
                if group.bucket not in _BUCKETS:
                    raise SemanticError("UNSUPPORTED_OPERATION", {"operation": "bucket"})
                expression = f"CAST(date_trunc('{group.bucket}', {expression}) AS DATE)"
            select.append(f"{expression} AS {quote(group.name)}")
            grouped.append(expression)
            columns.append(self.column(group.member, group.name, "dimension", group.bucket))
        ratios: list[tuple[int, Any, ColumnSpec]] = []
        for aggregate in (a for a in plan.aggregates if a.source in own):
            column = self.column(aggregate.metric, aggregate.name, "metric", None)
            columns.append(column)
            if aggregate.division is None:
                expression = self.metric(aggregate.metric, aggregate.source)
                select.append(f"{expression} AS {quote(aggregate.name)}")
                continue
            k = self.ratios
            self.ratios += 1
            trees = dict(self.compiled.require(aggregate.metric).typed.payload)
            dataset = aggregate.metric.dataset or ""
            for part, prefix in (("components.numerator", "#n"), ("components.denominator", "#d")):
                tree = trees[part]
                written = self.node(tree.node, aggregate.source, dataset, "metric")
                select.append(f"{written} AS {quote(f'{prefix}{k}')}")
            scales = (
                _scale(_logical(trees["components.numerator"].type.logical)),
                _scale(_logical(trees["components.denominator"].type.logical)),
            )
            ratios.append((k, (aggregate, scales), column))
        text = "SELECT " + ", ".join(select)
        sources = {s.alias: s for s in plan.sources}
        text += f" FROM {self.fact(sources[fact])} AS {quote(fact)}"
        for join in (j for j in plan.joins if j.left in own):
            on = " AND ".join(
                f"{self.field(a, join.left)} = {self.field(b, join.right)}" for a, b in join.pairs
            )
            text += f" LEFT JOIN {quote(sources[join.right].table)} AS {quote(join.right)} ON {on}"
        conditions = [
            self.predicate(self.field(f.member, f.source), f.predicate, f.member)
            for f in plan.filters
            if f.source in own
        ]
        if conditions:
            text += " WHERE " + " AND ".join(conditions)
        if grouped:
            text += " GROUP BY " + ", ".join(grouped)
        checks: list[tuple[str, str]] = []
        if ratios:
            text, checks = self.divide(text, columns, ratios)
        return text, columns, checks

    def align(
        self, parts: list[tuple[str, list[ColumnSpec], list[tuple[str, str]]]]
    ) -> tuple[str, list[ColumnSpec], list[tuple[str, str]]]:
        """The facts' aggregations side by side, matched on the requested grain only: the same
        group columns in each, `NULL` matching `NULL`. Each aggregation is unique on them, so no
        row repeats; a group one fact lacks has `NULL` for that fact's metrics."""
        dimensions = [c for c in parts[0][1] if c.role == "dimension"]
        for _, columns, _ in parts[1:]:  # the planner gives every fact the same grain
            if [c.name for c in columns if c.role == "dimension"] != [d.name for d in dimensions]:
                raise PlanError("facts aggregated to different grains")
        names = [quote(f"f{i}") for i in range(len(parts))]
        select = [
            f"COALESCE({', '.join(f'{f}.{quote(d.name)}' for f in names)}) AS {quote(d.name)}"
            for d in dimensions
        ]
        columns = list(dimensions)
        metrics = {
            c.name: (names[i], c)
            for i, part in enumerate(parts)
            for c in part[1]
            if c.role == "metric"
        }
        for aggregate in self.plan.aggregates:  # the question's order of metrics
            at, column = metrics[aggregate.name]
            select.append(f"{at}.{quote(column.name)}")
            columns.append(column)
        checks = [check for part in parts for check in part[2]]
        at_check = {check: names[i] for i, part in enumerate(parts) for check, _ in part[2]}
        select += [f"{at_check[check]}.{quote(check)}" for check, _ in checks]
        text = f"SELECT {', '.join(select)} FROM ({parts[0][0]}) AS {names[0]}"
        for i in range(1, len(parts)):
            on = " AND ".join(
                f"COALESCE({', '.join(f'{f}.{quote(d.name)}' for f in names[:i])})"
                f" IS NOT DISTINCT FROM {names[i]}.{quote(d.name)}"
                for d in dimensions
            )
            text += f" FULL OUTER JOIN ({parts[i][0]}) AS {names[i]} ON {on or 'TRUE'}"
        return text, columns, checks

    def divide(
        self, inner: str, columns: list[ColumnSpec], ratios: list[tuple[int, Any, ColumnSpec]]
    ) -> tuple[str, list[tuple[str, str]]]:
        """Each ratio as one exact division of its aggregated components (C2): both scaled to
        exact `HUGEINT`s, divided with the remainder, rounded once as declared."""
        scaled = []
        for k, (_, (sn, sd)), column in ratios:
            scale = int(str(column.logical["scale"])) if column.logical else 0
            # N/10^sn ÷ D/10^sd at scale s is (N·10^(sd+s)) / (D·10^sn); the common power of
            # ten is cancelled first, so no intermediate is larger than it must be.
            common = min(sd + scale, sn)
            numerator = _unscaled(quote(f"#n{k}"), sn)
            denominator = _unscaled(quote(f"#d{k}"), sd)
            scaled.append(f"{numerator} * {_power(sd + scale - common)} AS {quote(f'#a{k}')}")
            scaled.append(f"{denominator} * {_power(sn - common)} AS {quote(f'#b{k}')}")
        middle = f"SELECT *, {', '.join(scaled)} FROM ({inner}) AS {quote('q')}"
        by_name = {column.name: (k, plan) for k, plan, column in ratios}
        outer: list[str] = []
        flags: list[str] = []
        checks: list[tuple[str, str]] = []
        for column in columns:
            if column.name not in by_name:
                outer.append(quote(column.name))
                continue
            k, (aggregate, _) = by_name[column.name]
            division = aggregate.division
            a, b = quote(f"#a{k}"), quote(f"#b{k}")
            scale = int(str(column.logical["scale"])) if column.logical else 0
            value = f"CAST({_rounded(a, b, division.mode)} AS DECIMAL(38,0))"
            if scale:
                value = f"{value} * 0.{'0' * (scale - 1)}1"
            outer.append(
                f"CASE WHEN {b} = 0 THEN NULL ELSE CAST({value} AS {column.encoding}) END"
                f" AS {quote(column.name)}"
            )
            if division.zero == "reject":
                check = f"#zero{k}"
                flags.append(f"({b} = 0) AS {quote(check)}")
                checks.append((check, self.compiled.require(aggregate.metric).parsed.label))
            elif division.zero != "null":
                raise _unsupported()
        # Every check after every column: the order `execution` reads them in.
        return f"SELECT {', '.join(outer + flags)} FROM ({middle}) AS {quote('r')}", checks


def _power(exponent: int) -> str:
    """10^exponent as an exact `HUGEINT`; past its range, the cast fails, never a float."""
    return f"CAST('1{'0' * exponent}' AS HUGEINT)"


def _unscaled(value: str, scale: int) -> str:
    """The exact integer behind a `DECIMAL(p, scale)`, as a `HUGEINT`: its integer part and its
    fraction are scaled apart, so a 38-digit value never overflows a `DECIMAL(38)` on the way."""
    if scale == 0:
        return f"CAST({value} AS HUGEINT)"
    return (
        f"(CAST(trunc({value}) AS HUGEINT) * {_power(scale)}"
        f" + CAST(({value} - trunc({value})) * {_power(scale)} AS HUGEINT))"
    )


def _scale(logical: Mapping[str, object]) -> int:
    if logical.get("family") == "Integer":
        return 0
    if logical.get("family") == "Decimal" and isinstance(logical.get("scale"), int):
        return int(str(logical["scale"]))
    raise _unsupported()  # only an exact component divides exactly


def _rounded(a: str, b: str, mode: str) -> str:
    """The quotient a / b rounded to an integer in `mode`, from truncating integer division
    and its remainder; written so that no step can exceed the operands' range."""
    quotient, rest = f"({a} // {b})", f"({a} % {b})"
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


def compile_plan(plan: SemanticPlan) -> PhysicalQuery:
    """The DuckDB query of a plan the planner made, or an internal error."""
    if plan.issued is not ISSUED_PLAN:
        raise PlanError("not a plan of the planner")
    return _Writer(plan).statement()
