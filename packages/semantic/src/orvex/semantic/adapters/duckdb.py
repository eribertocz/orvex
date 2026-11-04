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
        if node.order is not None or node.distinct:
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
                inner = again(arguments[0])
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

    def statement(self) -> PhysicalQuery:
        plan = self.plan
        select: list[str] = []
        grouped: list[str] = []
        columns: list[ColumnSpec] = []
        for group in plan.groups:
            expression = self.field(group.member, group.source)
            if group.bucket is not None:
                if group.bucket not in _BUCKETS:
                    raise SemanticError("UNSUPPORTED_OPERATION", {"operation": "bucket"})
                expression = f"CAST(date_trunc('{group.bucket}', {expression}) AS DATE)"
            select.append(f"{expression} AS {quote(group.name)}")
            grouped.append(expression)
            columns.append(self.column(group.member, group.name, "dimension", group.bucket))
        for aggregate in plan.aggregates:
            expression = self.metric(aggregate.metric, aggregate.source)
            select.append(f"{expression} AS {quote(aggregate.name)}")
            columns.append(self.column(aggregate.metric, aggregate.name, "metric", None))
        text = "SELECT " + ", ".join(select)
        sources = {s.alias: s for s in plan.sources}
        text += f" FROM {quote(sources['t0'].table)} AS {quote('t0')}"
        for join in plan.joins:
            on = " AND ".join(
                f"{self.field(a, join.left)} = {self.field(b, join.right)}" for a, b in join.pairs
            )
            text += f" LEFT JOIN {quote(sources[join.right].table)} AS {quote(join.right)} ON {on}"
        conditions = [
            self.predicate(self.field(f.member, f.source), f.predicate, f.member)
            for f in plan.filters
        ]
        if conditions:
            text += " WHERE " + " AND ".join(conditions)
        if grouped:
            text += " GROUP BY " + ", ".join(grouped)
        if plan.order:
            text += " ORDER BY " + ", ".join(
                f"{quote(s.name)} {'DESC' if s.descending else 'ASC'} "
                f"NULLS {'FIRST' if s.nulls_first else 'LAST'}"
                for s in plan.order
            )
        if plan.limit is not None:
            text += f" LIMIT {int(plan.limit) + 1}"  # one more row says the page is truncated
        return PhysicalQuery(text, tuple(self.parameters), tuple(columns), plan.fingerprint)


def compile_plan(plan: SemanticPlan) -> PhysicalQuery:
    """The DuckDB query of a plan the planner made, or an internal error."""
    if plan.issued is not ISSUED_PLAN:
        raise PlanError("not a plan of the planner")
    return _Writer(plan).statement()
