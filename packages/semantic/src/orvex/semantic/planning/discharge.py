"""Obligation discharge, verified (C9; P2-22, #344).

The planner names, for each step, the obligations it means to discharge. This module does not
take its word for it. For each obligation of the report, a checker of its kind reads the plan's
structure, and later the statement the adapter wrote, and returns the label of the step that
discharges it: the evidence the receipt names. An obligation without evidence is an internal
error (`PlanError`), raised before anything is read; it is never a result.

- **Every rule has a checker** (`CHECKERS`). A rule the correctness engine applies with no
  checker here fails the guard test, so no obligation can go unchecked.
- **Row policies are checked against the policies themselves**, not only against the placement
  the plan was built from: every policy that reaches a dataset read, on its own dataset or by
  propagation, is applied there unless the caller is exempt from it, and the statement reads
  that dataset only through its restricted relation.
- **Labels name model elements only** (`aggregate:<fact>`, `latest:<fact>`, `derived:<metric>`,
  `window:<metric>`, `source:<dataset>`, `weights:<bridge>`, `as_of:<relationship>`), so they
  pass the receipt's visibility rules like the obligation itself.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from orvex.semantic.compiler.parsing import MemberId
from orvex.semantic.correctness.graph import Graph
from orvex.semantic.correctness.report import Obligation
from orvex.semantic.planning import PlanError

Evidence = dict[str, str]  # obligation id -> the label of the step that discharges it


class DischargeError(PlanError):
    """An obligation without evidence: an internal error, and the plan never runs."""


def _target(obligation: Obligation) -> str:
    """What the obligation is about: the metric, dataset, bridge or relationship after `:`."""
    return obligation.id.split(":", 1)[1]


class _Plan:
    """What the checkers read of a plan, derived from its structure only."""

    def __init__(self, plan: Any) -> None:  # noqa: ANN401
        self.plan = plan
        self.compiled = plan.compiled
        self.graph = Graph(self.compiled)
        self.sources = {s.alias: s for s in plan.sources}
        self.trees = self._trees()
        self.aggregates = {self.label(a.metric): a for a in plan.aggregates}
        self.by_name = {a.name: a for a in plan.aggregates}
        self.derived = {self.label(d.metric): d for d in plan.derived}
        self.windows = {self.label(w.metric): w for w in plan.windows}
        self.metrics = {m["metric"]: m for m in plan.report.facts["metrics"]}
        self.names = {g.name for g in plan.groups}

    def label(self, member: MemberId) -> str:
        found: str = self.compiled.require(member).parsed.label
        return found

    def written(self, dataset: str) -> str:
        return self.graph.names.get(dataset, dataset)

    def relationship(self, normalized: str) -> str:
        return self.graph.relationship_names.get(normalized, normalized)

    def _trees(self) -> dict[str, frozenset[str]]:
        """Each fact's aliases: the fact and every alias its joins reach. Two facts sharing
        an alias would join one fact's rows to another's (C3)."""
        trees: dict[str, frozenset[str]] = {}
        for fact in self.plan.facts:
            reached = {fact}
            grown = True
            while grown:
                grown = False
                for join in self.plan.joins:
                    if join.left in reached and join.right not in reached:
                        reached.add(join.right)
                        grown = True
            trees[fact] = frozenset(reached)
        seen: set[str] = set()
        for tree in trees.values():
            if tree & seen:
                raise DischargeError("two facts read one source: each must be aggregated apart")
            seen |= tree
        return trees

    def aggregate(self, metric: str) -> Any:  # noqa: ANN401
        found = self.aggregates.get(metric)
        if found is None:
            raise DischargeError(f"no aggregation of {metric}")
        if found.source not in self.trees:
            raise DischargeError(f"{metric} is not aggregated in its fact's own step")
        home = self.metrics.get(metric, {}).get("home")
        if home is not None and self.written(self.sources[found.source].dataset) != home:
            raise DischargeError(f"{metric} is aggregated from another fact than its home")
        return found

    def fact_label(self, alias: str) -> str:
        return self.written(self.sources[alias].dataset)


def _aggregate(p: _Plan, o: Obligation) -> str:
    """C1: the metric aggregated in its fact's own step, grouped by every requested group."""
    found = p.aggregate(_target(o))
    own = [g for g in p.plan.groups if g.source in p.trees[found.source]]
    grain = p.plan.report.facts["grain"]  # what the question asked, not what the plan holds
    asked = sorted(str(e["bucket"] or "") for e in grain)
    if (
        len({g.name for g in own}) != len(grain)
        or len(own) != len(grain)
        or sorted(g.bucket or "" for g in own) != asked
        or {g.name for g in own} != p.names
    ):
        raise DischargeError(f"{_target(o)} is not aggregated at the question's grain")
    return f"aggregate:{p.fact_label(found.source)}"


def _recompute(p: _Plan, o: Obligation) -> str:
    """C2: a ratio divided once, after its components are aggregated, never averaged."""
    derived = p.derived.get(_target(o))
    if derived is not None:
        if derived.division is None or len(derived.over) != 2:
            raise DischargeError(f"{_target(o)} is not divided once from its components")
        if any(name not in p.by_name for name in derived.over):
            raise DischargeError(f"{_target(o)} divides values no aggregation gives")
        return f"derived:{_target(o)}"
    found = p.aggregate(_target(o))
    if found.division is None:
        raise DischargeError(f"{_target(o)} is not recomputed from its components")
    return f"aggregate:{p.fact_label(found.source)}"


def _distinct(p: _Plan, o: Obligation) -> str:
    """C2: a distinct count taken at the requested grain, never added up."""
    found = p.aggregate(_target(o))
    if found.allocation is not None or found.division is not None:
        raise DischargeError(f"{_target(o)} is not counted at the requested grain")
    if any(w.over == found.name for w in p.plan.windows) or any(
        found.name in d.over for d in p.plan.derived
    ):
        raise DischargeError(f"{_target(o)} is added up after it is counted")
    _aggregate(p, o)
    return f"aggregate:{p.fact_label(found.source)}"


def _period_end(p: _Plan, o: Obligation) -> str:
    """C2: the fact read through its latest row per entity and period, by its declared axis,
    the period cut as the question's group on that axis is."""
    found = p.aggregate(_target(o))
    source = p.sources[found.source]
    latest = source.latest
    period = p.metrics.get(_target(o), {}).get("period") or {}
    if latest is None or p.label(latest.axis) != period.get("axis"):
        raise DischargeError(f"{_target(o)} is not read at each entity's period end")
    if not latest.entity:
        raise DischargeError(f"{_target(o)} has no entity to take the period end of")
    group = next(
        (g for g in p.plan.groups if g.member == latest.axis and g.source == source.alias), None
    )
    if group is not None and (group.bucket, group.zone) != (latest.bucket, latest.zone):
        raise DischargeError(f"{_target(o)} takes its period end in another period")
    return f"latest:{p.fact_label(source.alias)}"


def _isolate(p: _Plan, o: Obligation) -> str:
    """C3: each fact aggregated apart, before any join to another fact's rows."""
    found = p.aggregate(_target(o))
    homes = {m["home"] for m in p.metrics.values()}
    if len(p.plan.facts) != len(homes):
        raise DischargeError("the facts are not aggregated apart")
    return f"aggregate:{p.fact_label(found.source)}"


def _align(p: _Plan, o: Obligation) -> str:
    """C5: a metric over metrics computed after alignment, from aligned aggregates."""
    derived = p.derived.get(_target(o))
    if derived is None or not derived.over or _target(o) in p.aggregates:
        raise DischargeError(f"{_target(o)} is not computed after alignment")
    if any(name not in p.by_name for name in derived.over):
        raise DischargeError(f"{_target(o)} reads values no aggregation gives")
    return f"derived:{_target(o)}"


def _as_of(p: _Plan, o: Obligation) -> str:
    """C6: the temporal relationship crossed as an as-of join, with its guard."""
    if not any(
        j.as_of is not None and p.relationship(j.relationship) == _target(o) for j in p.plan.joins
    ):
        raise DischargeError(f"{_target(o)} is not crossed as a guarded as-of join")
    return f"as_of:{_target(o)}"


def _weights(p: _Plan, o: Obligation) -> str:
    """C7: the bridge's weights checked in the read that allocates through it."""
    if not any(
        a.allocation is not None and a.allocation.bridge == _target(o) for a in p.plan.aggregates
    ):
        raise DischargeError(f"the weights of {_target(o)} are not checked")
    return f"weights:{_target(o)}"


def _allocate(p: _Plan, o: Obligation) -> str:
    """C7: the metric allocated through its bridge, exactly, then rounded once."""
    found = p.aggregate(_target(o))
    if found.allocation is None or found.division is None:
        raise DischargeError(f"{_target(o)} is not allocated exactly and rounded once")
    return f"aggregate:{p.fact_label(found.source)}"


def _policy(p: _Plan, o: Obligation) -> str:
    """C9: every read of the dataset restricted by the policies placed on it."""
    placement = p.plan.placement
    reads = [s for s in p.plan.sources if p.written(s.dataset) == _target(o)]
    if not reads or placement is None:
        raise DischargeError(f"{_target(o)} is not read through its restrictions")
    for source in reads:
        wanted = sorted({r.rule.name for r in placement.of(source.dataset)})
        if sorted(source.policies) != wanted:
            raise DischargeError(f"{_target(o)} is read without its row policies")
    return f"source:{_target(o)}"


def _window(p: _Plan, o: Obligation) -> str:
    """S9: the running total computed after aggregation, at its order and partition."""
    window = p.windows.get(_target(o))
    if window is None or window.over not in p.by_name or _target(o) in p.aggregates:
        raise DischargeError(f"{_target(o)} is not computed after aggregation")
    if not window.order or any(name not in p.names for name in window.order):
        raise DischargeError(f"{_target(o)} is not ordered by the question's groups")
    if not 0 <= window.partitions < len(window.order):
        raise DischargeError(f"{_target(o)} has no order left after its partitions")
    return f"window:{_target(o)}"


CHECKERS: Mapping[tuple[str, str], Callable[[_Plan, Obligation], str]] = {
    ("C1", "aggregate"): _aggregate,
    ("C2", "recompute"): _recompute,
    ("C2", "distinct"): _distinct,
    ("C2", "period_end"): _period_end,
    ("C3", "isolate"): _isolate,
    ("C5", "align"): _align,
    ("C6", "as_of"): _as_of,
    ("C7", "weights"): _weights,
    ("C7", "allocate"): _allocate,
    ("C9", "policy"): _policy,
    ("S9", "window"): _window,
}
RULES_CHECKED = frozenset(rule for rule, _ in CHECKERS)


def _policies_reached(p: _Plan) -> None:
    """Every policy that reaches a dataset read, on its own dataset or by propagation, applies
    there, unless the caller's own groups exempt them from it; and each such dataset carries
    its obligation. Checked against the policy model, not the placement the plan was built on."""
    policies = p.plan.report.policies
    placement = p.plan.placement
    if policies is None:
        raise DischargeError("the plan carries no policy model to check its placement against")
    groups = set(p.plan.report.caller_groups)
    obligations = {o.id for o in p.plan.report.obligations}
    for source in p.plan.sources:
        for rule in policies.row_policies:
            reached = rule.dataset == source.dataset or any(
                r.dataset == source.dataset for r in policies.propagation.get(rule.name, ())
            )
            if not reached:
                continue
            applied = placement.applied.get(rule.name) if placement else None
            exempt = bool(rule.exempt_groups & groups)
            if applied is None or (applied == "exempt") != exempt:
                raise DischargeError(f"{rule.name} is not decided for this caller")
            if exempt:
                continue
            if rule.name not in source.policies:
                raise DischargeError(f"{p.written(source.dataset)} is read without {rule.name}")
            if f"C9.policy:{p.written(source.dataset)}" not in obligations:
                raise DischargeError(f"{p.written(source.dataset)} has no policy obligation")


def verify_plan(plan: Any) -> Evidence:  # noqa: ANN401
    """The evidence of every obligation of the plan's report, or `DischargeError`."""
    p = _Plan(plan)
    evidence: Evidence = {}
    for obligation in plan.report.obligations:
        checker = CHECKERS.get((obligation.rule, obligation.kind))
        if checker is None:
            raise DischargeError(f"no checker for {obligation.rule}.{obligation.kind}")
        evidence[obligation.id] = checker(p, obligation)
    _policies_reached(p)
    return evidence


_TOKEN = re.compile(r'"(?:[^"]|"")*"|\'(?:[^\']|\'\')*\'|\$?\w+|\S')


def _table_reads(text: str) -> list[tuple[str, str | None, bool]]:
    """Every table the statement reads, as (quoted table, quoted alias or None, whether it is
    written as the restricted relation `(SELECT * FROM table AS "#p" WHERE …)`): an identifier
    after `FROM`, `JOIN`, or a comma of a `FROM` list, that no `.` or `(` follows."""
    tokens = _TOKEN.findall(text)
    found: list[tuple[str, str | None, bool]] = []
    listing: list[bool] = [False]  # per parenthesis depth: inside a FROM list
    for at, token in enumerate(tokens):
        upper = token.upper()
        if token == "(":
            listing.append(False)
            continue
        if token == ")":
            listing.pop()
            continue
        starts = upper in ("FROM", "JOIN") or (token == "," and listing[-1])
        if upper in ("FROM", "JOIN"):
            listing[-1] = True
        elif upper in (
            "WHERE",
            "ON",
            "GROUP",
            "ORDER",
            "HAVING",
            "LIMIT",
            "QUALIFY",
            "UNION",
            "WINDOW",
            "SELECT",
        ):
            listing[-1] = False
        if not starts or at + 1 >= len(tokens):
            continue
        name = tokens[at + 1]
        after = tokens[at + 2] if at + 2 < len(tokens) else ""
        if not name.startswith('"') or after in (".", "("):
            continue
        alias = tokens[at + 3] if after.upper() == "AS" and at + 3 < len(tokens) else None
        restricted = (
            upper == "FROM"
            and tokens[at - 2 : at] == ["SELECT", "*"]
            and alias == '"#p"'
            and at + 4 < len(tokens)
            and tokens[at + 4].upper() == "WHERE"
        )
        found.append((name, alias, restricted))
    return found


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def verify_physical(plan: Any, physical: Any) -> None:  # noqa: ANN401
    """The statement holds what the plan's evidence promised, or `DischargeError`:
    - a guard column for every as-of join and every allocation through a bridge;
    - every read of a dataset a policy restricts is its restricted relation, or a related row
      read inside a propagation chain (`#c…`), never its bare table."""
    p = _Plan(plan)
    as_of = {p.relationship(j.relationship) for j in plan.joins if j.as_of is not None}
    if {name for _, name in physical.as_of} != as_of:
        raise DischargeError("an as-of join has no guard in the statement")
    bridges = {a.allocation.bridge for a in plan.aggregates if a.allocation is not None}
    if {bridge for _, bridge in physical.weights} != bridges:
        raise DischargeError("an allocation has no weight check in the statement")
    for column, _ in (*physical.as_of, *physical.weights):
        if _quote(column) not in physical.sql:
            raise DischargeError("a guard column is missing from the statement")
    restricted = {_quote(s.table) for s in plan.sources if s.policies}
    reads = _table_reads(physical.sql)
    for table in restricted:
        mine = [r for r in reads if r[0] == table]
        if not any(is_restricted for _, _, is_restricted in mine):
            raise DischargeError("a restricted dataset is not read through its restrictions")
        for _, alias, is_restricted in mine:
            chained = alias is not None and re.fullmatch(r'"#c[0-9]+"', alias) is not None
            if not (is_restricted or chained):
                raise DischargeError("a restricted dataset is read without its restrictions")
