"""Read-cost estimates of detail pages (#352): an upper bound of the rows of the lake files the
answer can need, from the file statistics of the snapshot read, never a guess or a sample
(`semantic-engine.md` §8). It is not DuckDB's physical scan count, which can be higher (late
materialization reads a file twice) or lower (DuckDB prunes row groups as well).

Every table the statement reads counts all its rows: each data file's row count before deletes,
and the rows kept in the catalog. Only the base of a detail page, when the statement reads it
once, is pruned. A file is left out when its exact statistics prove it holds no row that a
condition on a plain column of the base keeps: a filter, the cursor's start on the first sort
key, or a conjunct of a row policy on the base itself. A file without the statistics a proof
needs is counted, so the bound only gets looser, never wrong.

The estimate decides nothing: no lower bound is ever inferred, and a question is never refused
on it (Owner precision on #352). Without statistics, or with incomplete ones for any table read,
there is no estimate, and the receipt omits it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from orvex.contracts.store.v1 import FileStatistics, StatisticsRequest, StatisticsResult
from orvex.semantic.adapters.duckdb import PhysicalQuery, encoding
from orvex.semantic.catalog.building import _logical
from orvex.semantic.compiler.compiled import CompiledModel
from orvex.semantic.compiler.parsing import MemberId
from orvex.semantic.ossie import sql
from orvex.semantic.policies import predicate as grammar

Family = Literal["number", "string", "date", "boolean"]
INTEGERS = {
    "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
    "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT",
}  # fmt: skip


@dataclass(frozen=True, slots=True)
class Condition:
    """What a plain column of the base keeps: some values, a range, or `NULL` or not."""

    column: str  # as the statement reads it
    family: Family
    op: Literal["in", "range", "is_null", "is_not_null"]
    values: tuple[Any, ...] = ()  # `in`: parsed values
    lower: tuple[Any, bool] | None = None  # `range`: (value, inclusive)
    upper: tuple[Any, bool] | None = None
    include_null: bool = False  # a `NULL` is kept too


def _family(column_type: str) -> Family | None:
    upper = column_type.upper()
    if upper in INTEGERS or upper.startswith("DECIMAL"):
        return "number"
    if upper == "VARCHAR":
        return "string"
    if upper == "DATE":
        return "date"
    if upper == "BOOLEAN":
        return "boolean"
    return None  # anything else is never compared here: no file is left out for it


def parse(family: Family, text: object) -> Any:  # noqa: ANN401
    """A value of a family, exactly (a number as a `Decimal`), or `ValueError`."""
    if isinstance(text, bool):
        text = "true" if text else "false"
    value = str(text)
    if family == "number":
        try:
            parsed = Decimal(value)
        except InvalidOperation as error:
            raise ValueError(value) from error
        if not parsed.is_finite():
            raise ValueError(value)
        return parsed
    if family == "date":
        return dt.date.fromisoformat(value)
    if family == "boolean":
        if value not in ("true", "false"):
            raise ValueError(value)
        return value == "true"
    return value


def _plain(compiled: CompiledModel, member: MemberId, dataset: str) -> tuple[str, Family] | None:
    """The base column a field reads unchanged, and its family; none for anything else."""
    if member.kind != "field" or member.dataset != dataset:
        return None
    found = compiled.require(member)
    tree = found.parsed.tree
    if not isinstance(tree, sql.Ref) or (len(tree.parts) == 2 and tree.parts[0] != dataset):
        return None
    if found.typed.type.bucket_zone is not None:
        return None
    family = _family(encoding(dict(_logical(found.typed.type.logical))))
    return None if family is None else (tree.parts[-1], family)


def _keeps(condition: Condition, file: FileStatistics) -> bool:
    """Whether the file can hold a row the condition keeps, from its exact statistics."""
    stats = next(
        (c for c in file.columns if c.column.casefold() == condition.column.casefold()), None
    )
    if stats is None:
        return True
    nulls = stats.nulls
    if condition.op == "is_null":
        return nulls is None or nulls > 0
    all_null = nulls is not None and nulls == file.rows
    if condition.op == "is_not_null":
        return not all_null
    if condition.include_null and (nulls is None or nulls > 0):
        return True
    if all_null:
        return False
    if stats.min is None or stats.max is None:
        return True
    try:
        low, high = parse(condition.family, stats.min), parse(condition.family, stats.max)
    except ValueError:
        return True
    if condition.op == "in":
        return any(low <= v <= high for v in condition.values)
    if condition.lower is not None:
        value, inclusive = condition.lower
        if high < value or (high == value and not inclusive):
            return False
    if condition.upper is not None:
        value, inclusive = condition.upper
        if low > value or (low == value and not inclusive):
            return False
    return True


def _filters(plan: Any, compiled: CompiledModel, base: str, fact: str) -> Iterator[Condition]:  # noqa: ANN401
    for f in plan.filters:
        found = _plain(compiled, f.member, base) if f.source == fact else None
        if found is None:
            continue
        column, family = found
        predicate = f.predicate
        try:
            if predicate["op"] == "in":
                yield Condition(
                    column,
                    family,
                    "in",
                    values=tuple(parse(family, raw) for _, raw in predicate["values"]),
                    include_null=bool(predicate.get("include_null")),
                )
            elif predicate["op"] == "range":
                low = next(((k, predicate[k]) for k in ("gte", "gt") if k in predicate), None)
                high = next(((k, predicate[k]) for k in ("lte", "lt") if k in predicate), None)
                yield Condition(
                    column,
                    family,
                    "range",
                    lower=None if low is None else (parse(family, low[1][1]), low[0] == "gte"),
                    upper=None if high is None else (parse(family, high[1][1]), high[0] == "lte"),
                )
            elif predicate["op"] in ("is_null", "is_not_null"):
                yield Condition(column, family, predicate["op"])
        except ValueError, TypeError, KeyError:
            continue  # a value this family does not read: nothing is left out for it


def _start(plan: Any, compiled: CompiledModel, base: str, fact: str) -> Iterator[Condition]:  # noqa: ANN401
    """The rows after the cursor's position, on the first sort key: at or after its value
    (ties continue on the next keys), and `NULL` where `NULLS LAST` puts it after."""
    if plan.start is None or not plan.order:
        return
    first = plan.order[0]
    group = next((g for g in (*plan.rows, *plan.hidden) if g.name == first.name), None)
    found = _plain(compiled, group.member, base) if group and group.source == fact else None
    if found is None:
        return
    column, family = found
    value = plan.start[0]
    if value is None:
        if not first.nulls_first:  # after a NULL, NULLS LAST: only NULLs follow
            yield Condition(column, family, "is_null")
        return
    try:
        bound = (parse(family, value), True)
    except ValueError:
        return
    yield Condition(
        column,
        family,
        "range",
        lower=None if first.descending else bound,
        upper=bound if first.descending else None,
        include_null=not first.nulls_first,
    )


COMPARE = {"=": "in", "<": "upper", "<=": "upper", ">": "lower", ">=": "lower"}
FLIP = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "=": "="}


def _policies(plan: Any, compiled: CompiledModel, base: str) -> Iterator[Condition]:  # noqa: ANN401
    """Each conjunct of a row policy on the base's own rows: every policy holds (they are
    joined by AND), and within one, every operand of a top-level AND."""
    placement = plan.placement
    if placement is None:
        return
    attributes: Mapping[str, Any] = placement.attributes
    for restriction in placement.of(base):
        rule = restriction.rule
        if restriction.no_rows or restriction.path or rule.predicate is None:
            continue
        node = rule.predicate
        operands = (
            node.operands if isinstance(node, grammar.Logical) and node.op == "AND" else (node,)
        )
        for operand in operands:
            yield from _conjunct(operand, compiled, base, attributes)


def _scalar(operand: Any, attributes: Mapping[str, Any]) -> object:  # noqa: ANN401
    if isinstance(operand, grammar.Value):
        return operand.text
    found = attributes.get(operand.name)
    if found is None or found[0] == "list":
        raise ValueError(operand.name)
    return found[1]


def _conjunct(
    node: Any,  # noqa: ANN401
    compiled: CompiledModel,
    base: str,
    attributes: Mapping[str, Any],
) -> Iterator[Condition]:
    def plain(field: grammar.Field) -> tuple[str, Family] | None:
        return _plain(compiled, MemberId("field", base, field.name), base)

    try:
        if isinstance(node, grammar.Compare):
            left, op, right = node.left, node.op, node.right
            if not isinstance(left, grammar.Field):
                left, op, right = right, FLIP.get(op, op), left
            if not isinstance(left, grammar.Field) or isinstance(right, grammar.Field):
                return
            found = plain(left)
            if found is None or op not in COMPARE:
                return
            column, family = found
            value = parse(family, _scalar(right, attributes))
            if op == "=":
                yield Condition(column, family, "in", values=(value,))
            elif COMPARE[op] == "lower":
                yield Condition(column, family, "range", lower=(value, op == ">="))
            else:
                yield Condition(column, family, "range", upper=(value, op == "<="))
        elif isinstance(node, grammar.IsNull):
            found = plain(node.field)
            if found is not None:
                yield Condition(*found, "is_not_null" if node.negated else "is_null")
        elif isinstance(node, grammar.InAttribute) and not node.negated:
            found = plain(node.field)
            attribute = attributes.get(node.attribute.name)
            if found is not None and attribute is not None and attribute[0] == "list":
                column, family = found
                values = tuple(parse(family, v) for v in attribute[1])
                yield Condition(column, family, "in", values=values)
    except ValueError:
        return  # a value this column's family does not read: nothing is left out for it


def conditions(plan: Any, compiled: CompiledModel) -> list[Condition]:  # noqa: ANN401
    """Every condition on a plain column of a detail page's base that each row it reads holds."""
    fact = plan.facts[0]
    base = next(s.dataset for s in plan.sources if s.alias == fact)
    return [
        *_filters(plan, compiled, base, fact),
        *_start(plan, compiled, base, fact),
        *_policies(plan, compiled, base),
    ]


def estimate(
    plan: Any,  # noqa: ANN401 - a detail `SemanticPlan`
    physical: PhysicalQuery,
    compiled: CompiledModel,
    statistics: Callable[[StatisticsRequest], StatisticsResult | None] | None,
    snapshot: int,
) -> dict[str, int] | None:
    """`{"rows_scanned": n}`, an upper bound of the rows of the files the answer can need, or
    none."""
    if statistics is None or plan.mode != "detail" or not physical.reads:
        return None
    tables = list(dict.fromkeys(table for table, _ in physical.reads))
    try:  # a name or a count the request cannot carry: no estimate, never a refusal
        request = StatisticsRequest.model_validate(
            {"kind": "statistics", "tables": tables, "snapshot": snapshot}
        )
    except ValueError:  # pydantic's validation error
        return None
    found = statistics(request)
    if found is None:  # the store gave none (`server` maps its refusal to none): no estimate
        return None
    by_table = {t.table.root: t for t in found.tables}
    if found.snapshot != snapshot or any(not t.complete for t in found.tables):
        return None
    if set(by_table) != set(tables):
        return None
    fact = plan.facts[0]
    base = next(s.dataset for s in plan.sources if s.alias == fact)
    base_reads = [table for table, dataset in physical.reads if dataset == base]
    pruned = conditions(plan, compiled) if len(base_reads) == 1 else []
    rows = 0
    for table, dataset in physical.reads:
        stats = by_table[table]
        kept: Sequence[FileStatistics] = stats.files
        if dataset == base and pruned:
            kept = [f for f in stats.files if all(_keeps(c, f) for c in pruned)]
        rows += stats.inlined_rows + sum(f.rows for f in kept)
    return {"rows_scanned": rows}
