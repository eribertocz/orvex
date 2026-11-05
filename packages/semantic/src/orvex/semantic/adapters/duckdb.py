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

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from orvex.semantic.catalog.building import _logical
from orvex.semantic.compiler.catalog import LogicalType
from orvex.semantic.compiler.compiled import CompiledModel
from orvex.semantic.compiler.parsing import MemberId
from orvex.semantic.errors import SemanticError
from orvex.semantic.ossie import sql
from orvex.semantic.planning import ISSUED_PLAN, PlanError, SemanticPlan
from orvex.semantic.policies import predicate as policy_grammar
from orvex.semantic.policies.placement import datatype as field_datatype
from orvex.semantic.result import ColumnSpec
from orvex.semantic.result.descriptors import INTEGERS, OTHERS, pair_allowed

NAME = "duckdb"
VERSION = "1"
_ARITHMETIC = frozenset({"+", "-", "*", "/", "%"})
_COMPARISON = frozenset({"=", "<>", "<", "<=", ">", ">="})
_LOGICAL = frozenset({"AND", "OR"})
_CALLS = frozenset({"COALESCE", "NULLIF", "ABS", "LOWER", "UPPER"})
_AGGREGATES = frozenset({"SUM", "COUNT"})
_BUCKETS = frozenset({"day", "week", "month", "quarter", "year"})  # a date's (Monday weeks)
_CLOCK = frozenset({"hour", "minute"})  # an instant's only: it stays an instant
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
    # The zones the statement converts instants to (C6): each checked against the pinned
    # `tzdata` on the engine before it is read (`execution`).
    zones: tuple[str, ...] = ()
    # Then BIGINT columns: (column, bridge) for a metric allocated through a bridge (C7), each
    # row holding how many normalization groups read have invalid weights; any refuses all.
    weights: tuple[tuple[str, str], ...] = ()
    # Then BIGINT columns: (column, relationship) for an as-of join (C6), each row holding how
    # many rows read match not exactly one valid row; any refuses the whole result.
    as_of: tuple[tuple[str, str], ...] = ()


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
        document = self.compiled.ossie.document
        datasets = document.get("datasets", ())
        self.tables = {
            sql.normalize(str(d["name"])): str(d.get("source", d["name"]))
            for d in (datasets if isinstance(datasets, tuple) else ())
        }
        relationships = document.get("relationships", ())
        self.relationships = {
            sql.normalize(str(r["name"])): r
            for r in (relationships if isinstance(relationships, tuple) else ())
        }
        self.placement = plan.placement  # the caller's row policies (P2-21)
        self.ratios = 0  # each ratio's own columns, numbered across every fact
        self.zone: str | None = None  # the zone of the field being written (C6)
        self.zones: set[str] = set()  # every zone the statement converts to
        self.weights: list[tuple[str, str]] = []  # (column, bridge): invalid weight groups
        self.as_of: list[tuple[str, str]] = []  # (column, relationship): rows not matched once

    def bind(self, kind: str, value: str | None) -> str:
        name = f"p{len(self.parameters)}"
        self.parameters.append({"name": name, "type": kind, "value": value})
        return f"${name}"

    # members

    def field(self, member: MemberId, alias: str) -> str:
        compiled = self.compiled.require(member)
        tree = compiled.parsed.tree
        if tree is None:
            raise _unsupported()
        outer, self.zone = self.zone, compiled.typed.type.bucket_zone  # its calendar's zone
        try:
            return self.node(tree, alias, member.dataset or "", "field")
        finally:
            self.zone = outer

    def cut(self, unit: str, value: str, zone: str | None) -> str:
        """`value` cut to the calendar `unit` (C6, P2-19). An instant is converted to `zone`
        first, never the session's: a day or longer is the local date it starts on; an hour or
        a minute stays an instant, the one its local hour starts at, so an hour that repeats
        when clocks go back stays two rows, each with its offset."""
        if zone is None:
            if unit not in _BUCKETS:
                raise SemanticError("UNSUPPORTED_OPERATION", {"operation": "bucket"})
            return f"CAST(date_trunc('{unit}', {value}) AS DATE)"
        if unit not in _BUCKETS | _CLOCK:
            raise SemanticError("UNSUPPORTED_OPERATION", {"operation": "bucket"})
        self.zones.add(zone)
        local = f"timezone({self.bind('VARCHAR', zone)}, {value})"
        if unit in _BUCKETS:
            return f"CAST(date_trunc('{unit}', {local}) AS DATE)"
        return f"({value} - ({local} - date_trunc('{unit}', {local})))"

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
            kind = self.datatype(operand, dataset, context)
            if unit is not None and kind == "Date":
                return self.cut(unit, again(operand), None)
            if unit is not None and kind == "DateTimeTz" and self.zone is not None:
                return self.cut(unit, again(operand), self.zone)
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
        if bucket is not None and logical["family"] == "DateTimeTz" and bucket in _BUCKETS:
            logical = dict(_logical(LogicalType("Date")))  # the local date it starts on (C6)
        elif bucket is not None and not (
            logical["family"] == "Date" or (logical["family"] == "DateTimeTz" and bucket in _CLOCK)
        ):
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

    # Row policies (P2-21): the one place a table is named.

    def relation(self, dataset: str) -> str:
        """The rows of `dataset` the caller may read: its table, restricted by every row policy
        that applies, before anything else reads it (a join, a ranking, a check, an aggregate).
        Every read of a table in the statement goes through here."""
        table = quote(self.tables[dataset])
        restrictions = self.placement.of(dataset) if self.placement else ()
        if not restrictions:
            return table
        conditions = [self.restriction(r, dataset, "#p") for r in restrictions]
        return f"(SELECT * FROM {table} AS {quote('#p')} WHERE {' AND '.join(conditions)})"

    def restriction(self, restriction: Any, dataset: str, alias: str) -> str:  # noqa: ANN401
        """One policy on one dataset's row `alias`: its predicate on its own dataset, or, along
        its propagation path, on the related row, which must exist unless `orphans: visible`."""
        rule = restriction.rule
        if restriction.no_rows or rule.predicate is None:  # an attribute is missing: no rows
            return "FALSE"
        if not restriction.path:
            return self.policy_condition(rule, dataset, alias)
        current, at, tables, links = dataset, alias, [], []
        for depth, name in enumerate(reversed(restriction.path), 1):
            related = self.relationships[name]
            here = f"#c{depth}"
            if sql.normalize(str(related["from"])) == current:
                own, other, target = related["from_columns"], related["to_columns"], related["to"]
            else:
                own, other, target = related["to_columns"], related["from_columns"], related["from"]
            target = sql.normalize(str(target))
            tables.append(f"{quote(self.tables[target])} AS {quote(here)}")
            links += [
                f"{self.field(MemberId('field', current, sql.normalize(str(a))), at)}"
                f" = {self.field(MemberId('field', target, sql.normalize(str(b))), here)}"
                for a, b in zip(own, other, strict=True)
            ]
            current, at = target, here
        chain = f"SELECT 1 FROM {', '.join(tables)} WHERE {' AND '.join(links)}"
        passes = f"EXISTS ({chain} AND ({self.policy_condition(rule, current, at)}))"
        if rule.orphans == "visible":
            return f"(NOT EXISTS ({chain}) OR {passes})"
        return passes

    def policy_condition(self, rule: Any, dataset: str, alias: str) -> str:  # noqa: ANN401
        found = self.predicate_sql(rule.predicate, dataset, alias)
        registration = rule.registration
        if registration is not None:  # a deny-list holds only for registered callers
            key = str(registration.get("attribute", "user_id"))
            value = self.attribute_value(key, ("text", "integer"), "String")
            column = f"{quote('#g')}.{quote(str(registration['key']))}"
            registered = (
                f"EXISTS (SELECT 1 FROM {self.mapping(str(registration['table']))} AS "
                f"{quote('#g')} WHERE CAST({column} AS VARCHAR) = {value})"
            )
            found = f"({registered} AND {found})"
        return found

    def mapping(self, table: str) -> str:
        return ".".join(quote(part) for part in table.split("."))

    def attribute_value(self, name: str, kinds: tuple[str, ...], datatype: str | None) -> str:
        """An attribute of the validated context, always a bound parameter, of the type it is
        compared with. Placement already gave zero rows for any attribute that is absent or
        does not fit (`fits`), so one here is an internal failure, and refuses the question:
        a value is never substituted, where a `NOT` could make it match."""
        found = self.placement.attributes.get(name) if self.placement else None
        if found is None or found[0] not in kinds:
            raise _unsupported()
        kind, value = found
        if kind == "list":
            if not isinstance(value, tuple) or datatype not in ("Integer", "String"):
                raise _unsupported()
            text = self.bind("VARCHAR", json.dumps(list(value)))
            element = "HUGEINT" if datatype == "Integer" else "VARCHAR"
            return f"(SELECT unnest(CAST(CAST({text} AS JSON) AS {element}[])))"
        if kind == "boolean":
            return f"CAST({self.bind('VARCHAR', 'true' if value else 'false')} AS BOOLEAN)"
        if kind == "integer" and datatype != "String":
            return f"CAST({self.bind('VARCHAR', str(value))} AS HUGEINT)"
        return self.bind("VARCHAR", str(value))  # text, or a key compared as text

    def scalar_sql(self, operand: Any, other: Any, dataset: str, alias: str) -> str:  # noqa: ANN401
        """An operand of a comparison; an attribute takes the type of what it is compared with."""
        if isinstance(operand, policy_grammar.Attribute):
            datatype = (
                field_datatype(self.compiled, dataset, other.name)
                if isinstance(other, policy_grammar.Field)
                else None
            )
            return self.attribute_value(operand.name, ("text", "integer", "boolean"), datatype)
        return self.operand_sql(operand, dataset, alias)

    def operand_sql(self, operand: Any, dataset: str, alias: str) -> str:  # noqa: ANN401
        if isinstance(operand, policy_grammar.Field):
            return self.field(MemberId("field", dataset, operand.name), alias)
        if isinstance(operand, policy_grammar.Attribute):
            return self.attribute_value(operand.name, ("text", "integer", "boolean"), None)
        if operand.kind == "boolean":
            return "TRUE" if operand.text.upper() == "TRUE" else "FALSE"
        if operand.kind == "integer":
            return f"CAST({self.bind('VARCHAR', operand.text)} AS HUGEINT)"
        return self.bind("VARCHAR", operand.text)  # already the literal's value

    def condition_sql(self, condition: Any, dataset: str, alias: str) -> str:  # noqa: ANN401
        """`column = value` in a mapping subquery: an attribute is compared as text, the one
        form a managed table's key is checked in (as a registration is)."""
        column = f"{quote('#m')}.{quote(condition.column)}"
        if isinstance(condition.value, policy_grammar.Attribute):
            value = self.attribute_value(condition.value.name, ("text", "integer"), "String")
            return f"CAST({column} AS VARCHAR) = {value}"
        return f"{column} = {self.operand_sql(condition.value, dataset, alias)}"

    def predicate_sql(self, node: Any, dataset: str, alias: str) -> str:  # noqa: ANN401
        """A predicate of the closed grammar (`security-policies` v1), every value bound."""
        g = policy_grammar
        if isinstance(node, g.Compare):
            op = "<>" if node.op == "!=" else node.op
            left = self.scalar_sql(node.left, node.right, dataset, alias)
            right = self.scalar_sql(node.right, node.left, dataset, alias)
            return f"({left} {op} {right})"
        if isinstance(node, g.IsNull):
            field = self.operand_sql(node.field, dataset, alias)
            return f"({field} IS {'NOT ' if node.negated else ''}NULL)"
        if isinstance(node, g.InAttribute):
            field = self.operand_sql(node.field, dataset, alias)
            datatype = field_datatype(self.compiled, dataset, node.field.name)
            values = self.attribute_value(node.attribute.name, ("list",), datatype)
            return f"({field} {'NOT ' if node.negated else ''}IN {values})"
        if isinstance(node, g.InSelect | g.Exists):
            conditions = (
                " AND ".join(self.condition_sql(c, dataset, alias) for c in node.conditions)
                or "TRUE"
            )
            table = f"{self.mapping(node.table)} AS {quote('#m')}"
            if isinstance(node, g.InSelect):
                field = self.operand_sql(node.field, dataset, alias)
                return (
                    f"({field} {'NOT ' if node.negated else ''}IN (SELECT "
                    f"{quote('#m')}.{quote(node.column)} FROM {table} WHERE {conditions}))"
                )
            negated = "NOT " if node.negated else ""
            return f"({negated}EXISTS (SELECT 1 FROM {table} WHERE {conditions}))"
        if isinstance(node, g.Not):
            return f"(NOT {self.predicate_sql(node.operand, dataset, alias)})"
        if isinstance(node, g.Logical):
            joined = f" {node.op} ".join(
                self.predicate_sql(o, dataset, alias) for o in node.operands
            )
            return f"({joined})"
        raise _unsupported()

    def fact(self, source: Any) -> str:  # noqa: ANN401
        """The fact's table, or, for a period-end metric, only each entity's latest row per
        period (C2): a ranking over the entity and the period, by the axis, newest first. The
        entity plus the axis is the key, so no two rows tie."""
        latest = source.latest
        if latest is None:
            return self.relation(source.dataset)
        axis = self.field(latest.axis, "s")
        period = axis
        if latest.bucket is not None:  # the same period the question groups by, in its zone
            period = self.cut(latest.bucket, axis, latest.zone)
        partition = ", ".join([self.field(e, "s") for e in latest.entity] + [period])
        return (
            f"(SELECT * FROM {self.relation(source.dataset)} AS {quote('s')}"
            " QUALIFY row_number() OVER "
            f"(PARTITION BY {partition} ORDER BY {axis} DESC) = 1)"
        )

    def statement(self) -> PhysicalQuery:
        plan = self.plan
        parts = [self.aggregation(fact) for fact in plan.facts]
        if len(parts) == 1:
            text, columns, checks = parts[0]
        else:
            text, columns, checks = self.align(parts)
        if (self.weights or self.as_of) and (len(parts) > 1 or plan.derived):
            raise PlanError("a bridge is allocated only in one fact, with no metric over metrics")
        if plan.derived:
            text, columns, checks = self.derive(text, columns, checks)
        if plan.windows:
            if len(parts) > 1 or plan.derived:
                raise PlanError("a running total is computed over one fact's aggregation")
            text, columns = self.accumulate(text, columns, checks)
        if plan.order:
            text += " ORDER BY " + ", ".join(
                f"{quote(s.name)} {'DESC' if s.descending else 'ASC'} "
                f"NULLS {'FIRST' if s.nulls_first else 'LAST'}"
                for s in plan.order
            )
        if plan.limit is not None:
            text += f" LIMIT {int(plan.limit) + 1}"  # one more row says the page is truncated
        return PhysicalQuery(
            text,
            tuple(self.parameters),
            tuple(columns),
            plan.fingerprint,
            checks=tuple(checks),
            zones=tuple(sorted(self.zones)),
            weights=tuple(self.weights),
            as_of=tuple(self.as_of),
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
                expression = self.cut(group.bucket, expression, group.zone)
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
            if aggregate.allocation is not None:  # the weighted sum, rounded once (C7)
                written, scale = self.allocated(aggregate)
                select.append(f"{written} AS {quote(f'#n{k}')}")
                select.append(f"1 AS {quote(f'#d{k}')}")
                ratios.append((k, (aggregate, (scale, 0)), column))
                continue
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
        sources = {s.alias: s for s in plan.sources}
        rows = f" FROM {self.fact(sources[fact])} AS {quote(fact)}"
        for join in (j for j in plan.joins if j.left in own):
            on = " AND ".join(
                f"{self.field(a, join.left)} = {self.field(b, join.right)}" for a, b in join.pairs
            )
            if join.as_of is not None:  # the row valid when the event happened, closed-open
                event = self.field(join.as_of.event, join.left)
                start = self.field(join.as_of.valid_from, join.right)
                end = self.field(join.as_of.valid_to, join.right)
                on += f" AND {start} <= {event} AND {event} < {end}"
            right = self.relation(sources[join.right].dataset)
            rows += f" LEFT JOIN {right} AS {quote(join.right)} ON {on}"
        conditions = [
            self.predicate(self.field(f.member, f.source), f.predicate, f.member)
            for f in plan.filters
            if f.source in own
        ]
        weights: list[str] = []
        for aggregate in (a for a in plan.aggregates if a.source in own and a.allocation):
            assert aggregate.allocation is not None
            name = f"#w{len(self.weights)}"
            check = self.invalid_groups(aggregate.allocation, rows, conditions)
            select.append(f"{check} AS {quote(name)}")
            weights.append(quote(name))
            self.weights.append((name, aggregate.allocation.bridge))
        for join in (j for j in plan.joins if j.left in own and j.as_of):
            name = f"#t{len(self.as_of)}"
            select.append(f"{self.unmatched(join, rows, conditions)} AS {quote(name)}")
            weights.append(quote(name))
            self.as_of.append((name, self.relationship_name(join.relationship)))
        text = "SELECT " + ", ".join(select) + rows
        if conditions:
            text += " WHERE " + " AND ".join(conditions)
        if grouped:
            text += " GROUP BY " + ", ".join(grouped)
        checks: list[tuple[str, str]] = []
        if ratios:
            text, checks = self.divide(text, columns, ratios, after=weights)
        return text, columns, checks

    def relationship_name(self, normalized: str) -> str:
        found = self.compiled.ossie.document.get("relationships", ())
        relationships = found if isinstance(found, tuple) else ()
        return next(
            str(r["name"]) for r in relationships if sql.normalize(str(r["name"])) == normalized
        )

    def unmatched(self, join: Any, rows: str, conditions: list[str]) -> str:  # noqa: ANN401
        """How many left rows the question reads match not exactly one right row, counted on
        the whole right table, whatever filters keep of it: in the same statement (C6)."""
        as_of = join.as_of
        keys = [self.field(r, join.left) for r in as_of.rows]
        values = [self.field(a, join.left) for a, _ in join.pairs]
        event = self.field(as_of.event, join.left)
        read = ", ".join(f"{v} AS {quote(f'#r{i}')}" for i, v in enumerate([*keys, *values, event]))
        table = self.relation(next(s.dataset for s in self.plan.sources if s.alias == join.right))
        n = len(keys)
        on = " AND ".join(
            f"{self.field(b, '#h')} = {quote('#u')}.{quote(f'#r{n + i}')}"
            for i, (_, b) in enumerate(join.pairs)
        )
        moment = f"{quote('#u')}.{quote(f'#r{n + len(values)}')}"
        start = self.field(as_of.valid_from, "#h")
        end = self.field(as_of.valid_to, "#h")
        matches = (
            f"(SELECT count(*) FROM {table} AS {quote('#h')} WHERE {on}"
            f" AND {start} <= {moment} AND {moment} < {end})"
        )
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        return (
            f"(SELECT count(*) FROM (SELECT DISTINCT {read}{rows}{where}) AS {quote('#u')}"
            f" WHERE {matches} <> 1)"
        )

    def allocated(self, aggregate: Any) -> tuple[str, int]:  # noqa: ANN401
        """`SUM(value * weight)`, exact: the product keeps both scales (C7)."""
        allocation = aggregate.allocation
        tree = self.compiled.require(aggregate.metric).typed.tree
        assert tree is not None  # a plain SUM, as `analyze` checked
        assert isinstance(tree.node, sql.Call)
        value = self.node(
            tree.node.arguments[0], aggregate.source, aggregate.metric.dataset or "", "metric"
        )
        weight = self.field(allocation.weight, allocation.source)
        weight_type = self.compiled.require(allocation.weight).typed.type.logical
        scales = (_scale(_logical(tree.type.logical)), _scale(_logical(weight_type)))
        scale = sum(scales)
        if scale > 38:
            raise _unsupported()
        # Each operand widened first: the product is exact up to 38 digits, never narrower.
        product = (
            f"CAST(({value}) AS DECIMAL(38,{scales[0]}))"
            f" * CAST(({weight}) AS DECIMAL(38,{scales[1]}))"
        )
        return f"SUM(CAST({product} AS DECIMAL(38,{scale})))", scale

    def invalid_groups(self, allocation: Any, rows: str, conditions: list[str]) -> str:  # noqa: ANN401
        """How many normalization groups the query reads have weights that are missing, `NULL`
        or do not add up to the total within the tolerance: in the same statement, over the
        same rows, so on the same snapshot. Every weight of a group counts, whatever filters
        keep of its bridge rows."""
        keys = [self.field(a, allocation.side) for a, _ in allocation.scope]
        own = [self.field(b, "#b") for _, b in allocation.scope]
        weight = self.field(allocation.weight, "#b")
        table = self.relation(
            next(s.dataset for s in self.plan.sources if s.alias == allocation.source)
        )
        total, tolerance = (
            f"CAST({self.bind(number_parameter(v), v)} AS {number_parameter(v)})"
            for v in (allocation.total, allocation.tolerance)
        )
        sums = (
            f"(SELECT {', '.join(f'{c} AS {quote(f"#k{i}")}' for i, c in enumerate(own))}, "
            f"SUM({weight}) AS {quote('#s')}, count(*) - count({weight}) AS {quote('#n')} "
            f"FROM {table} AS {quote('#b')} GROUP BY {', '.join(own)})"
        )
        on = " AND ".join(f"{k} = {quote('#v')}.{quote(f'#k{i}')}" for i, k in enumerate(keys))
        s, n = f"{quote('#v')}.{quote('#s')}", f"{quote('#v')}.{quote('#n')}"
        bad = f"({s} IS NULL OR {n} > 0 OR abs({s} - {total}) > {tolerance})"
        where = " AND ".join([*conditions, bad])
        read = ", ".join(f"{k} AS {quote(f'#g{i}')}" for i, k in enumerate(keys))
        return (
            f"(SELECT count(*) FROM (SELECT DISTINCT {read}{rows} LEFT JOIN {sums} AS "
            f"{quote('#v')} ON {on} WHERE {where}) AS {quote('#i')})"
        )

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

    def derive(
        self, inner: str, columns: list[ColumnSpec], checks: list[tuple[str, str]]
    ) -> tuple[str, list[ColumnSpec], list[tuple[str, str]]]:
        """Each metric over metrics, row by row, from the aligned aggregates it names (S7):
        never aggregated again. `+` and `-` are exact; a ratio is C2's one division. Only the
        question's columns are kept, in its order, then every check."""
        by_name = {c.name: c for c in columns}
        steps = {d.name: d for d in self.plan.derived}
        dimensions = [c for c in columns if c.role == "dimension"]
        middle: list[str] = []
        ratios: list[tuple[int, Any, ColumnSpec]] = []
        outputs: list[ColumnSpec] = []
        for name in self.plan.outputs:
            step = steps.get(name)
            if step is None:
                outputs.append(by_name[name])
                continue
            column = self.column(step.metric, step.name, "metric", None)
            outputs.append(column)
            if step.division is None:
                tree = dict(self.compiled.require(step.metric).typed.payload)["over_metrics"]
                written = self.over(tree.node, step)
                middle.append(f"CAST({written} AS {column.encoding}) AS {quote(name)}")
                continue
            k = self.ratios
            self.ratios += 1
            numerator, denominator = (quote(n) for n in step.over)
            middle.append(f"{numerator} AS {quote(f'#n{k}')}")
            middle.append(f"{denominator} AS {quote(f'#d{k}')}")
            trees = dict(self.compiled.require(step.metric).typed.payload)
            scales = (
                _scale(_logical(trees["components.numerator"].type.logical)),
                _scale(_logical(trees["components.denominator"].type.logical)),
            )
            ratios.append((k, (step, scales), column))
        text = f"SELECT *{''.join(', ' + m for m in middle)} FROM ({inner}) AS {quote('a')}"
        final = dimensions + outputs
        keep = [check for check, _ in checks]
        if ratios:
            text, more = self.divide(text, final, ratios, keep)
            return text, final, checks + more
        kept = ", ".join(quote(c.name) for c in final) + "".join(", " + quote(k) for k in keep)
        return f"SELECT {kept} FROM ({text}) AS {quote('b')}", final, checks

    def accumulate(
        self, inner: str, columns: list[ColumnSpec], checks: list[tuple[str, str]]
    ) -> tuple[str, list[ColumnSpec]]:
        """Each running total over the aggregated rows (S9): summed in its order from the start
        of its partition, exactly, never aggregated again. Then the filters on its own groups
        choose the rows shown, so a filter never restarts it. Only the question's columns are
        kept, in its order, then every extra column."""
        by_name = {c.name: c for c in columns}
        steps = {w.name: w for w in self.plan.windows}
        dimensions = [c for c in columns if c.role == "dimension"]
        select = [quote(c.name) for c in dimensions]
        outputs: list[ColumnSpec] = []
        for name in self.plan.outputs:
            step = steps.get(name)
            if step is None:
                outputs.append(by_name[name])
                select.append(quote(name))
                continue
            column = self.column(step.metric, step.name, "metric", None)
            outputs.append(column)
            ordered = step.order[: len(step.order) - step.partitions]
            partitions = step.order[len(step.order) - step.partitions :]
            within = (
                f"PARTITION BY {', '.join(quote(p) for p in partitions)} " if partitions else ""
            )
            order = ", ".join(f"{quote(o)} ASC NULLS LAST" for o in ordered)
            total = (
                f"SUM({quote(step.over)}) OVER ({within}ORDER BY {order}"
                " ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)"
            )
            select.append(f"CAST({total} AS {column.encoding}) AS {quote(name)}")
        extra = [n for n, _ in checks] + [n for n, _ in self.weights] + [n for n, _ in self.as_of]
        select += [quote(n) for n in extra]
        text = f"SELECT {', '.join(select)} FROM ({inner}) AS {quote('w')}"
        if self.plan.after:
            shown = " AND ".join(
                self.predicate(quote(f.source), f.predicate, f.member) for f in self.plan.after
            )
            # A failed check keeps every row, so the refusal it carries is never filtered away.
            failed = [f"{quote(n)}" for n, _ in checks] + [
                f"{quote(n)} > 0" for n, _ in [*self.weights, *self.as_of]
            ]
            keep = " OR ".join([f"({shown})", *failed])
            text = f"SELECT * FROM ({text}) AS {quote('v')} WHERE {keep}"
        return text, dimensions + outputs

    def over(self, node: sql.Node, step: Any) -> str:  # noqa: ANN401
        """`+` and `-` of the aligned aggregates a metric over metrics names."""
        if isinstance(node, sql.Binary) and node.op in ("+", "-"):
            return f"({self.over(node.left, step)} {node.op} {self.over(node.right, step)})"
        if isinstance(node, sql.Ref):
            written = ".".join(node.parts)
            for aggregate in self.plan.aggregates:
                label = self.compiled.require(aggregate.metric).parsed.label
                if aggregate.name in step.over and sql.normalize(label) == written:
                    return quote(aggregate.name)
        raise _unsupported()

    def divide(
        self,
        inner: str,
        columns: list[ColumnSpec],
        ratios: list[tuple[int, Any, ColumnSpec]],
        keep: list[str] | None = None,
        after: list[str] | None = None,
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
        kept = [quote(k) for k in keep or ()]
        selected = outer + kept + flags + list(after or ())  # columns, checks, then weights
        return f"SELECT {', '.join(selected)} FROM ({middle}) AS {quote('r')}", checks


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
