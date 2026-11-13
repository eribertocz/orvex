"""Which row policies withhold every row from the caller, and why (P3-09, #443): an attribute
the policy needs is missing or does not fit (`POLICY_ATTRIBUTE_MISSING`), or the caller has
no mapping (`POLICY_MAPPING_MISSING`), read at the question's own snapshot.

The statement already gives such a policy's rows as none; this only says why, for the notice
and for the audit event `server` records (`dataset`, `policy`). A caller has no mapping when:
- a deny-list's `registration` does not list them (a count of its rows for the caller);
- or the policy's predicate is, or is a conjunction holding, a mapping subquery (`IN (SELECT
  ...)` or `EXISTS`, not negated) that has no row for the caller: its condition is then false
  for every row. A subquery that reads the restricted row cannot be evaluated apart: on a
  plan holding one, the allow-lists' rows are still none where they should be, with no
  `POLICY_MAPPING_MISSING`.

Nothing is read for a policy whose attribute is missing. On another engine the subqueries'
values were already read on the managed lake at the same snapshot (`resolve`) and are reused.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import pyarrow as pa
import pyarrow.compute as pc
from orvex.semantic.adapters.dialect import quote
from orvex.semantic.adapters.duckdb import MappingQuery, PolicyValues, mapping_queries
from orvex.semantic.engines import dataset_label
from orvex.semantic.errors import SemanticError
from orvex.semantic.planning import SemanticPlan
from orvex.semantic.policies import predicate as grammar
from orvex.semantic.policies.placement import Restriction

type Run = Callable[[str, Sequence[Mapping[str, str | None]]], pa.RecordBatchReader]

ATTRIBUTE_MISSING = "POLICY_ATTRIBUTE_MISSING"
MAPPING_MISSING = "POLICY_MAPPING_MISSING"


@dataclass(frozen=True, slots=True)
class Withheld:
    """One policy that gives the caller no row: the notice's code, and the policy's dataset (as
    the model writes it) and name. Internal: only the notice's own audience reaches a caller."""

    code: str
    dataset: str
    policy: str


def withheld(
    plan: SemanticPlan, run: Run, values: Mapping[int, PolicyValues] | None = None
) -> list[Withheld]:
    """Every policy of the plan's placement that withholds every row, attribute ones first,
    each group by policy name. `run` reads the managed lake at the question's snapshot;
    `values` are the subqueries' values when the plan runs on another engine."""
    placement = plan.placement
    if placement is None:
        return []
    rules: dict[str, Restriction] = {
        r.rule.name: r for found in placement.restrictions.values() for r in found
    }
    missing = [_of(plan, rules[n], ATTRIBUTE_MISSING) for n in sorted(rules) if rules[n].no_rows]
    queries: list[MappingQuery] | None = None  # read once, and only when an allow-list asks
    unmapped: list[Withheld] = []
    for name in sorted(rules):
        restriction = rules[name]
        if restriction.no_rows:
            continue
        if restriction.rule.registration is not None:
            empty = not _registered(plan, restriction, run)
        elif not (nodes := _positive(restriction.rule.predicate)):
            empty = False
        elif values is not None:
            empty = any(n in values and not values[n].values for n in nodes)
        else:
            queries = _queries(plan) if queries is None else queries
            empty = any(q.key in nodes and not _count(run, q.sql, q.parameters) for q in queries)
        if empty:
            unmapped.append(_of(plan, restriction, MAPPING_MISSING))
    return missing + unmapped


def _of(plan: SemanticPlan, restriction: Restriction, code: str) -> Withheld:
    rule = restriction.rule
    return Withheld(code, dataset_label(plan.compiled, rule.dataset), rule.name)


def _positive(node: grammar.Node | None) -> set[int]:
    """The mapping subqueries whose emptiness makes the whole predicate false for every row:
    the predicate itself, or a conjunct of a top-level `AND`, never negated."""
    if isinstance(node, grammar.Logical) and node.op == "AND":
        return {k for operand in node.operands for k in _positive(operand)}
    if isinstance(node, grammar.InSelect | grammar.Exists) and not node.negated:
        return {id(node)}
    return set()


def _queries(plan: SemanticPlan) -> list[MappingQuery]:
    """The plan's mapping subqueries, bound for the managed lake; none when one of them reads
    the restricted row and so cannot be evaluated apart."""
    try:
        return mapping_queries(plan)
    except SemanticError:  # `POLICY_NOT_SUPPORTED_ON_ENGINE`: only another engine refuses it
        return []


def _registered(plan: SemanticPlan, restriction: Restriction, run: Run) -> bool:
    """Whether the deny-list's registration lists the caller, on the question's snapshot."""
    placement = plan.placement
    registration = restriction.rule.registration
    if placement is None or registration is None:
        return True
    key = str(registration.get("attribute", "user_id"))
    attribute = placement.attributes[key]  # present and of a fitting type, or no_rows
    table = ".".join(quote(part) for part in str(registration["table"]).split("."))
    column = quote(str(registration["key"]))
    statement = f"SELECT * FROM {table} WHERE CAST({column} AS VARCHAR) = $p0"
    value = {"name": "p0", "type": "VARCHAR", "value": str(attribute[1])}
    return _count(run, statement, (value,)) > 0


def _count(run: Run, statement: str, parameters: Sequence[Mapping[str, str | None]]) -> int:
    counted = run(f'SELECT count(*) AS "#n" FROM ({statement}) AS "#q"', parameters).read_all()
    return int(pc.max(counted.column("#n")).as_py() or 0)  # one scalar: a count
