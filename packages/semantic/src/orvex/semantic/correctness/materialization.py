"""C8, materialization equivalence (P2-25, #348).

An aggregate table (`aggregate_table`, S5) may answer a question in place of its base dataset
only with a proof, from the model alone, that its answer is the base answer: the same groups,
the same filters, the same metric semantics, the same rounding and the same security. The proof
never looks at the data; whether the stored numbers are current is evidence (`consistent`,
P2-26), checked apart (decision 1).

What the table stores is declared, never guessed from names: `base_dataset`, and on each
measure the base `metric` it stores at the table's grain, re-aggregated by `SUM(column)`.

- **Grain.** Every group and filter is a field of the base dataset itself (no role, no bucket,
  no join) whose declared conformed dimension is in the table's grain, and the table has one
  field of that dimension and type.
- **Metrics.** An additive metric stored by a measure; or a recomputable ratio whose numerator
  and denominator are each, exactly, the expression of a stored metric. Never a distinct count,
  a period-end balance, a running total, a metric over metrics, an allocation or an as-of join.
- **Security: exact preservation (Owner precision on #348).** Every row policy that restricts
  the base for this caller decides each base row by fields of the grain only, the same fields
  the table stores, so every group the table holds is wholly authorized or wholly not. A policy
  reached by propagation decides a base row by the related row it links to: it is preserved when
  the base's columns of that first link are grain fields (#354). A policy reading any other
  field, one propagated through any other link, a policy on the table itself, or a table field
  the caller cannot see makes the table not eligible. A table is never a reason to refuse
  a question: the base is read, with its policies.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from orvex.semantic.compiler.parsing import MemberId
from orvex.semantic.errors import SemanticError
from orvex.semantic.ossie import sql
from orvex.semantic.policies import predicate as grammar
from orvex.semantic.policies.placement import place


@dataclass(frozen=True, slots=True)
class Candidate:
    """A declared aggregate table of the question's base, and the proof's outcome."""

    dataset: str  # normalized
    name: str  # as written
    base: str  # normalized
    eligible: bool
    reason: str | None = None  # why not, for builders: grain, filter, metric, policy, shape
    fields: Mapping[MemberId, MemberId] = field(default_factory=dict)  # base field -> table field
    stored: Mapping[MemberId, MemberId] = field(default_factory=dict)  # base metric -> column
    components: Mapping[MemberId, tuple[MemberId, MemberId]] = field(default_factory=dict)
    grain: int = 0  # how many dimensions the table keeps: the smaller, the better


class _No(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason


def candidates(
    analysis: Any,  # noqa: ANN401
    home: str,
    metrics: list[MemberId],
    classes: Mapping[MemberId, tuple[str, str]],
) -> list[Candidate]:
    """Every aggregate table that declares `home` as its base, eligible or not, in the order a
    plan would choose them: eligible first, then the smallest grain, then by name."""
    graph = analysis.graph
    found: list[Candidate] = []
    for dataset, payload in sorted(graph.payloads.items()):
        table = payload.get("aggregate_table")
        if not isinstance(table, Mapping):
            continue
        base = table.get("base_dataset")
        if base is None or sql.normalize(str(base)) != home:
            continue
        name = graph.names[dataset]
        grain = [sql.normalize(str(d)) for d in (payload.get("grain") or {}).get("dimensions", ())]
        try:
            mapping, stored, components = _prove(
                analysis, home, dataset, table, grain, metrics, classes
            )
        except _No as no:
            found.append(Candidate(dataset, name, home, False, no.reason, grain=len(grain)))
            continue
        found.append(
            Candidate(dataset, name, home, True, None, mapping, stored, components, len(grain))
        )
    return sorted(found, key=lambda c: (not c.eligible, c.grain, c.name))


def _prove(
    analysis: Any,  # noqa: ANN401
    home: str,
    dataset: str,
    table: Mapping[str, Any],
    grain: list[str],
    metrics: list[MemberId],
    classes: Mapping[MemberId, tuple[str, str]],
) -> tuple[
    dict[MemberId, MemberId], dict[MemberId, MemberId], dict[MemberId, tuple[MemberId, MemberId]]
]:
    compiled = analysis.compiled
    if analysis.periods:
        raise _No("period_end")
    if analysis.derived or analysis.windows or analysis.bridges:
        raise _No("shape")
    if analysis.temporal or getattr(analysis.query, "totals", None):
        raise _No("shape")
    own = [m for m in compiled.members if m.kind == "field" and m.dataset == dataset]
    base_fields = [m for m in compiled.members if m.kind == "field" and m.dataset == home]

    def by_dimension(members: list[MemberId]) -> dict[str, list[MemberId]]:
        found: dict[str, list[MemberId]] = {}
        for member in members:
            name = analysis.conformed_name(member)
            if name is not None:
                found.setdefault(name, []).append(member)
        return found

    stored_by, based_by = by_dimension(own), by_dimension(base_fields)

    def mapped(member: MemberId, reason: str) -> MemberId:
        """The table's field for one base field: the one field of its conformed dimension on
        each side, so a field behind the dimension (a day under a month) never passes for it."""
        if member.kind != "field" or member.dataset != home:
            raise _No(reason)
        name = analysis.conformed_name(member)
        if name is None or name not in grain:
            raise _No(reason)
        if based_by.get(name) != [member] or len(stored_by.get(name, ())) != 1:
            raise _No(reason)
        target = stored_by[name][0]
        wanted = compiled.require(member).typed.type.logical
        if compiled.require(target).typed.type.logical != wanted:
            raise _No(reason)
        return target

    fields: dict[MemberId, MemberId] = {}
    for row in analysis.query.rows:
        if row.slot not in ("group_by", "filter"):
            continue
        reason = "grain" if row.slot == "group_by" else "filter"
        if row.role is not None or row.bucket is not None:
            raise _No(reason)
        fields[row.member] = mapped(row.member, reason)

    stored = stored_measures(compiled, dataset, table, home)

    used: dict[MemberId, MemberId] = {}
    components: dict[MemberId, tuple[MemberId, MemberId]] = {}
    for metric in metrics:
        typed = compiled.require(metric).typed
        additivity = classes.get(metric, ("", ""))[0]
        if additivity == "additive" and metric in stored:
            _same_values(analysis, metric, stored[metric])
            used[metric] = stored[metric]
            continue
        if additivity == "recomputable":
            trees = dict(typed.payload)
            ends = []
            for part in ("components.numerator", "components.denominator"):
                node = trees[part].node
                match = next(
                    (
                        m
                        for m in stored
                        if compiled.require(m).parsed.tree == node
                        and _class(analysis, classes, m) == "additive"
                    ),
                    None,
                )
                if match is None:
                    raise _No("ratio")
                _same_values(analysis, match, stored[match])
                ends.append(stored[match])
            components[metric] = (ends[0], ends[1])
            continue
        raise _No("distinct" if additivity == "distinct_count" else "metric")

    _policies(analysis, home, dataset, fields, mapped)
    for member in [*fields.values(), *used.values(), *(c for p in components.values() for c in p)]:
        if analysis.policies.protection(member, analysis.groups) != "visible":
            raise _No("policy")
    return fields, used, components


def stored_measures(
    compiled: Any,  # noqa: ANN401
    dataset: str,
    table: Mapping[str, Any],
    home: str,
) -> dict[MemberId, MemberId]:
    """Each base metric an aggregate table stores, and the table column storing it: a measure
    that names its `metric` and stores it as the plain sum of one column. Any other measure is
    not a re-aggregation this proof knows, and proves nothing."""
    own = [m for m in compiled.members if m.kind == "field" and m.dataset == dataset]
    columns = {sql.normalize(m.name): m for m in own}
    stored: dict[MemberId, MemberId] = {}
    for measure in table.get("measures", ()):
        if "metric" not in measure:
            continue
        expression = sql.parse(str(measure["expression"]))
        if not (
            isinstance(expression, sql.Call)
            and expression.name.upper() == "SUM"
            and not expression.distinct
            and expression.filter is None
            and len(expression.arguments) == 1
            and isinstance(expression.arguments[0], sql.Ref)
            and len(expression.arguments[0].parts) == 1
            and expression.arguments[0].parts[0] in columns
        ):
            continue
        written = sql.normalize(str(measure["metric"]))
        metric = next(
            (
                m
                for m in compiled.members
                if m.kind == "metric" and sql.normalize(m.name) == written
            ),
            None,
        )
        if metric is not None and metric.dataset in (None, home):
            stored[metric] = columns[expression.arguments[0].parts[0]]
    return stored


def _same_values(analysis: Any, metric: MemberId, column: MemberId) -> None:  # noqa: ANN401
    """The stored column holds the metric's own values: the same logical type (an exact
    decimal stays a decimal of the same scale, an integer an integer) and the same declared
    unit or currency as the field the metric aggregates."""
    compiled = analysis.compiled
    if compiled.require(column).typed.type.logical != compiled.require(metric).typed.type.logical:
        raise _No("metric")
    tree = compiled.require(metric).parsed.tree
    refs = sql.references(tree) if tree is not None else []
    measured = [
        MemberId("field", r.parts[0] if len(r.parts) == 2 else (metric.dataset or ""), r.parts[-1])
        for r in refs
    ]

    def declared(member: MemberId) -> tuple[object, object, object]:
        payload = analysis.payloads.get(compiled.require(member).parsed.pointer, {})
        return payload.get("semantic_type"), payload.get("currency"), payload.get("unit")

    sources = {declared(m) for m in measured if m in compiled.members}
    if len(sources) > 1 or (sources and declared(column) not in sources):
        raise _No("unit")


def _class(analysis: Any, classes: Mapping[MemberId, tuple[str, str]], metric: MemberId) -> str:  # noqa: ANN401
    """A metric's class, from the question or, for a stored metric it does not ask, as the
    correctness engine classifies it; an undeclared one proves nothing."""
    if metric in classes:
        return classes[metric][0]
    try:
        return str(analysis.additivity(metric)[0])
    except SemanticError:
        return ""


def _policies(
    analysis: Any,  # noqa: ANN401
    home: str,
    dataset: str,
    fields: dict[MemberId, MemberId],
    mapped: Any,  # noqa: ANN401
) -> None:
    """Exact preservation: each restriction on the base decides a base row by grain fields
    only, so the table's groups are each wholly in or wholly out; nothing restricts the table
    beyond what restricts the base."""
    placement = place(analysis.policies, [home, dataset], analysis.caller, analysis.compiled)
    if placement.of(dataset):
        raise _No("policy")  # the table's own policies would restrict beyond the base's
    for restriction in placement.of(home):
        if restriction.no_rows:
            continue  # zero rows from either plan
        if restriction.path:
            # Decided by the related row the base row links to (#354): exact when the base's
            # columns of that first link are kept as the grain, so every row of a group links to
            # the same related row, and the group is wholly in or out, `orphans: visible`, role
            # paths, mapping tables and registrations included (they read the related rows).
            for name in link_columns(analysis.compiled, home, restriction.path):
                member = MemberId("field", home, sql.normalize(name))
                if member not in analysis.compiled.members:
                    raise _No("policy")
                try:
                    fields.setdefault(member, mapped(member, "policy"))
                except _No:
                    raise _No("policy") from None
            continue
        predicate = restriction.rule.predicate
        if predicate is None:
            raise _No("policy")
        for name in _fields(predicate):
            member = MemberId("field", home, sql.normalize(name))
            if member not in analysis.compiled.members:
                raise _No("policy")
            try:
                fields.setdefault(member, mapped(member, "policy"))
            except _No:
                raise _No("policy") from None


def link_columns(compiled: Any, dataset: str, path: tuple[str, ...]) -> tuple[str, ...]:  # noqa: ANN401
    """`dataset`'s own columns in the first step of a propagation path from it: the step written
    last, since a path runs from the policy's dataset (`Restriction.path`). A step this proof
    does not know is no proof."""
    found = compiled.ossie.document.get("relationships", ())
    relationships = {
        sql.normalize(str(r["name"])): r for r in (found if isinstance(found, tuple) else ())
    }
    steps = [relationships.get(sql.normalize(name)) for name in path]
    related = steps[-1] if steps else None
    if related is None or any(step is None for step in steps):
        raise _No("policy")
    # Only the first link may read the base: a later step through it (a path back to the base,
    # or a self-relationship) would read the base's rows inside the table's relation (#354).
    if any(
        dataset in {sql.normalize(str(step["from"])), sql.normalize(str(step["to"]))}
        for step in steps[:-1]
        if step is not None
    ) or sql.normalize(str(related["from"])) == sql.normalize(str(related["to"])):
        raise _No("policy")
    if sql.normalize(str(related["from"])) == dataset:
        return tuple(str(c) for c in related["from_columns"])
    if sql.normalize(str(related["to"])) == dataset:
        return tuple(str(c) for c in related["to_columns"])
    raise _No("policy")


def _fields(node: object) -> list[str]:
    """Every base field a policy predicate reads, its mapping-table conditions included; a
    node this proof does not know is no proof."""
    if isinstance(node, grammar.Field):
        return [node.name]
    if isinstance(node, grammar.Attribute | grammar.Value):
        return []
    if isinstance(node, grammar.Compare):
        return [*_fields(node.left), *_fields(node.right)]
    if isinstance(node, grammar.InAttribute):
        return _fields(node.field)
    if isinstance(node, grammar.InSelect):
        return [*_fields(node.field), *(f for c in node.conditions for f in _fields(c.value))]
    if isinstance(node, grammar.Exists):
        return [f for c in node.conditions for f in _fields(c.value)]
    if isinstance(node, grammar.IsNull):
        return _fields(node.field)
    if isinstance(node, grammar.Not):
        return _fields(node.operand)
    if isinstance(node, grammar.Logical):
        return [f for o in node.operands for f in _fields(o)]
    raise _No("policy")
