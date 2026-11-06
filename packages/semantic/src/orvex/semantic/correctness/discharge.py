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
from orvex.semantic.ossie import sql
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

    def payload(self, label: str) -> Mapping[str, Any]:
        """The metric's own product extension, read from the model, never from the plan."""
        member = next(m for m in self.compiled.members if self.label(m) == label)
        pointer = self.compiled.require(member).parsed.pointer
        for extension in self.compiled.ossie.product_extensions:
            if extension.pointer.rsplit("/custom_extensions/", 1)[0] == pointer:
                found: Mapping[str, Any] = extension.payload
                return found
        return {}

    def components(self, label: str) -> tuple[str, str]:
        """A ratio's declared numerator and denominator, as metric labels."""
        member = next(m for m in self.compiled.members if self.label(m) == label)
        trees = dict(self.compiled.require(member).typed.payload)
        ends = []
        for part in ("components.numerator", "components.denominator"):
            node = trees[part].node
            if not isinstance(node, sql.Ref):
                raise DischargeError(f"{label} has no metric as a component")
            written = ".".join(node.parts)
            ends.append(
                next(
                    self.label(m)
                    for m in self.compiled.members
                    if m.kind == "metric" and sql.normalize(self.label(m)) == written
                )
            )
        return ends[0], ends[1]

    def division(self, label: str) -> tuple[int, str, str]:
        """The declared rounding and zero-denominator policy of a ratio, or of an allocation
        (rounded once, `null` on zero: there is no denominator)."""
        payload = self.payload(label)
        rounding = payload.get("rounding")
        if not isinstance(rounding, Mapping):
            raise DischargeError(f"{label} declares no rounding")
        zero = str(payload.get("zero_denominator", "null"))
        return int(rounding["scale"]), str(rounding["mode"]), zero

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
    """C2: a ratio divided once, after its components are aggregated, never averaged: its
    numerator by its denominator, as declared, with the declared rounding and zero policy."""
    name = _target(o)
    derived = p.derived.get(name)
    if derived is not None:
        if derived.division is None or len(derived.over) != 2:
            raise DischargeError(f"{name} is not divided once from its components")
        steps = [p.by_name.get(n) for n in derived.over]
        if any(step is None for step in steps):
            raise DischargeError(f"{name} divides values no aggregation gives")
        found = tuple(p.label(step.metric) for step in steps if step is not None)
        if found != p.components(name):
            raise DischargeError(f"{name} does not divide its numerator by its denominator")
        division = derived.division
        evidence = f"derived:{name}"
    else:
        step = p.aggregate(name)
        if step.division is None:
            raise DischargeError(f"{name} is not recomputed from its components")
        division = step.division
        evidence = f"aggregate:{p.fact_label(step.source)}"
    if (division.scale, division.mode, division.zero) != p.division(name):
        raise DischargeError(f"{name} is not rounded as declared")
    return evidence


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
    """C2: the fact read through its latest row per entity and period: the entity is the
    fact's key without the axis (`analyze` accepted exactly that key), the axis as declared,
    and the period and zone those of the question's group on the axis, or none."""
    name = _target(o)
    found = p.aggregate(name)
    source = p.sources[found.source]
    latest = source.latest
    period = p.metrics.get(name, {}).get("period") or {}
    if latest is None or p.label(latest.axis) != period.get("axis"):
        raise DischargeError(f"{name} is not read at each entity's period end")
    key = p.graph.keys[source.dataset]
    entity = tuple(MemberId("field", source.dataset, k) for k in key if k != latest.axis.name)
    if not entity or latest.entity != entity or latest.axis.dataset != source.dataset:
        raise DischargeError(f"{name} takes its period end per another entity")
    group = next(
        (g for g in p.plan.groups if g.member == latest.axis and g.source == source.alias), None
    )
    wanted = (group.bucket, group.zone) if group is not None else (None, None)
    if (latest.bucket, latest.zone) != wanted:
        raise DischargeError(f"{name} takes its period end in another period or zone")
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
    declared = next(
        (d["over"] for d in p.plan.report.facts.get("derived", ()) if d["metric"] == _target(o)),
        None,
    )
    read = {p.label(p.by_name[name].metric) for name in derived.over}
    if declared is None or not read <= set(declared):
        raise DischargeError(f"{_target(o)} reads metrics it is not declared over")
    return f"derived:{_target(o)}"


def _as_of(p: _Plan, o: Obligation) -> str:
    """C6: the temporal relationship crossed as an as-of join, by its declared event time and
    validity, with its guard over the left dataset's whole key."""
    name = _target(o)
    for join in p.plan.joins:
        if join.as_of is None or p.relationship(join.relationship) != name:
            continue
        temporal = p.graph.relationship_payloads.get(join.relationship, {}).get("temporal")
        if not isinstance(temporal, Mapping):
            raise DischargeError(f"{name} declares no temporal match")
        left, right = p.sources[join.left].dataset, p.sources[join.right].dataset

        def field(ref: object, side: str) -> MemberId:
            parts = [sql.normalize(x) for x in str(ref).split(".")]
            return MemberId("field", parts[0] if len(parts) == 2 else side, parts[-1])

        declared = (
            field(temporal["event_time"], left),
            field(temporal["valid_from"], right),
            field(temporal["valid_to"], right),
        )
        rows = tuple(MemberId("field", left, k) for k in p.graph.keys[left])
        found = (join.as_of.event, join.as_of.valid_from, join.as_of.valid_to)
        if found != declared or not rows or join.as_of.rows != rows:
            raise DischargeError(f"{name} is not matched by its declared validity")
        return f"as_of:{name}"
    raise DischargeError(f"{name} is not crossed as a guarded as-of join")


def _bridge(p: _Plan, aggregate: Any, bridge: str) -> None:  # noqa: ANN401
    """The allocation through `bridge` as the bridge declares it: entered by its `left`
    relationship, weighed by its `weight`, normalized over that join's fields, to its `total`
    within its `tolerance`."""
    allocation = aggregate.allocation
    normalized = next((n for n, w in p.graph.names.items() if w == bridge), None)
    declared = p.graph.payloads.get(normalized or "", {}).get("bridge")
    if allocation is None or not isinstance(declared, Mapping):
        raise DischargeError(f"{bridge} is not allocated as it declares")
    left = sql.normalize(str(declared["left"]))
    entering = [
        j
        for j in p.plan.joins
        if j.relationship == left and j.right == allocation.source and j.left == allocation.side
    ]
    weight = MemberId("field", normalized or "", sql.normalize(str(declared["weight"])))
    if (
        len(entering) != 1
        or p.sources[allocation.source].dataset != normalized
        or not allocation.scope
        or allocation.scope != entering[0].pairs
        or allocation.weight != weight
        or (allocation.total, allocation.tolerance)
        != (str(declared["total"]), str(declared["tolerance"]))
    ):
        raise DischargeError(f"{bridge} is not allocated as it declares")


def _weights(p: _Plan, o: Obligation) -> str:
    """C7: the bridge's weights checked in the read that allocates through it, as declared."""
    bridge = _target(o)
    found = [a for a in p.plan.aggregates if a.allocation and a.allocation.bridge == bridge]
    if not found:
        raise DischargeError(f"the weights of {bridge} are not checked")
    for aggregate in found:
        _bridge(p, aggregate, bridge)
    return f"weights:{bridge}"


def _allocate(p: _Plan, o: Obligation) -> str:
    """C7: the metric allocated through its declared bridge, exactly, then rounded once."""
    name = _target(o)
    found = p.aggregate(name)
    bridges = [
        a["bridge"] for a in p.plan.report.facts.get("allocations", ()) if name in a["metrics"]
    ]
    if found.allocation is None or found.division is None or bridges != [found.allocation.bridge]:
        raise DischargeError(f"{name} is not allocated exactly and rounded once")
    _bridge(p, found, found.allocation.bridge)
    division = found.division
    if (division.scale, division.mode) != p.division(name)[:2] or division.zero != "null":
        raise DischargeError(f"{name} is not rounded as declared")
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


def _rows(p: _Plan, o: Obligation) -> str:
    """C1 for a detail page (P2-24): one row per row of the base dataset, as the model reads it:
    the base read once, every field and sort column from it or along a many-to-one join, no
    aggregation, the base's whole key last in the order, a page size, and every masked field
    written as its mask."""
    plan = p.plan
    base = _target(o)
    if plan.mode != "detail" or plan.aggregates or plan.groups or len(plan.facts) != 1:
        raise DischargeError(f"{base} is not read one row per row")
    fact = plan.facts[0]
    if p.written(p.sources[fact].dataset) != base or plan.page is None:
        raise DischargeError(f"{base} is not the page's base, read once")
    if any(g.source not in p.trees[fact] for g in (*plan.rows, *plan.hidden)):
        raise DischargeError("a field is read outside the base's joins")
    for join in plan.joins:
        left = p.sources[join.left].dataset
        step = next((s for s in p.graph.links[left] if s.relationship == join.relationship), None)
        if step is None or not step.safe or step.temporal or join.as_of is not None:
            raise DischargeError(f"{p.relationship(join.relationship)} could multiply the rows")
    columns = {g.name: g.member for g in (*plan.rows, *plan.hidden)}
    normalized = p.sources[fact].dataset
    key = [MemberId("field", normalized, k) for k in p.graph.keys[normalized]]
    tail = [(columns.get(s.name), s.descending) for s in plan.order[len(plan.order) - len(key) :]]
    last = {member for member, descending in tail if not descending}
    if not key or len(plan.order) < len(key) or not set(key) <= last:
        raise DischargeError(f"{base}'s key is not the order's last tie-breaker")
    masked = dict(plan.masks)
    wanted = p.plan.report.facts.get("masks", {})
    shown = {p.label(g.member): g.name for g in plan.rows}
    if any(shown.get(label) not in masked for label in wanted):
        raise DischargeError("a masked field is not written as its mask")
    return f"rows:{base}"


CHECKERS: Mapping[tuple[str, str], Callable[[_Plan, Obligation], str]] = {
    ("C1", "aggregate"): _aggregate,
    ("C1", "rows"): _rows,
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
            paths = {
                r.path
                for r in policies.propagation.get(rule.name, ())
                if r.dataset == source.dataset
            }
            if rule.dataset == source.dataset:
                paths.add(())
            placed = [r for r in placement.of(source.dataset) if r.rule.name == rule.name]
            if {r.path for r in placed} != paths or any(r.rule != rule for r in placed):
                raise DischargeError(f"{rule.name} is not placed on each of its paths as declared")
            missing = bool(policies.missing_attributes(rule.name, placement.attributes))
            if missing and not all(r.no_rows for r in placed):
                raise DischargeError(f"{rule.name} lacks an attribute but gives rows")
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


Read = tuple[tuple[str, ...], str | None, bool, bool]


def _table_reads(text: str) -> list[Read]:
    """Every table the statement reads, as (its quoted name parts, its quoted alias or None,
    whether it is a restricted relation, whether it is read inside one). A table is an
    identifier, qualified or not, after `FROM`, `JOIN` or a comma of a `FROM` list, that no
    `(` follows. A restricted relation is `(SELECT * FROM table AS "#p" WHERE <condition>)`
    with a condition other than a bare `TRUE`; everything inside its parentheses is read
    inside it. Unbalanced parentheses are no statement to trust."""
    tokens = _TOKEN.findall(text)
    found: list[Read] = []
    listing: list[bool] = [False]  # per parenthesis depth: inside a FROM list
    guarded: list[bool] = [False]  # per parenthesis depth: inside a restricted relation
    ends = {"WHERE", "ON", "GROUP", "ORDER", "HAVING", "LIMIT", "QUALIFY", "UNION", "WINDOW"}
    for at, token in enumerate(tokens):
        upper = token.upper()
        if token == "(":
            listing.append(False)
            guarded.append(guarded[-1])
            continue
        if token == ")":
            if len(listing) == 1:
                raise DischargeError("the statement's parentheses do not balance")
            listing.pop()
            guarded.pop()
            continue
        starts = upper in ("FROM", "JOIN") or (token == "," and listing[-1])
        if upper in ("FROM", "JOIN"):
            listing[-1] = True
        elif upper in ends or upper == "SELECT":
            listing[-1] = False
        if not starts:
            continue
        parts: list[str] = []
        k = at + 1
        while k < len(tokens) and tokens[k].startswith('"'):
            parts.append(tokens[k])
            if k + 1 < len(tokens) and tokens[k + 1] == ".":
                k += 2
                continue
            k += 1
            break
        if not parts or (k < len(tokens) and tokens[k] == "("):
            continue  # a subquery or a table function, read token by token
        alias = tokens[k + 1] if k + 1 < len(tokens) and tokens[k].upper() == "AS" else None
        condition = tokens[k + 3 : k + 5] if k + 4 < len(tokens) else []
        restricted = (
            len(parts) == 1
            and upper == "FROM"
            and at >= 3
            and tokens[at - 3 : at] == ["(", "SELECT", "*"]
            and alias == '"#p"'
            and tokens[k + 2].upper() == "WHERE"
            and [c.upper() for c in condition] != ["TRUE", ")"]
        )
        found.append((tuple(parts), alias, restricted, guarded[-1]))
        if restricted:
            guarded[-1] = True
    if len(listing) != 1:
        raise DischargeError("the statement's parentheses do not balance")
    return found


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def verify_physical(plan: Any, physical: Any) -> Evidence:  # noqa: ANN401
    """The plan's evidence, checked again, and the statement holding what it promised, or
    `DischargeError`:
    - a guard column for every as-of join and every allocation through a bridge;
    - every read of a dataset a policy restricts is its restricted relation, or a related row
      read inside a restricted relation's propagation chain (`#c…`); never its bare table,
      qualified or not, anywhere else."""
    evidence = verify_plan(plan)
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
    if plan.mode == "detail":
        if plan.page is None or not physical.sql.rstrip().endswith(f"LIMIT {plan.page + 1}"):
            raise DischargeError("the detail statement is not one page and one more row")
        for name, _ in plan.masks:  # written as its bound mask, never read
            if not re.search(
                rf"CAST\(\$p[0-9]+ AS VARCHAR\) AS {re.escape(_quote(name))}", physical.sql
            ):
                raise DischargeError("a masked field is read in the statement")
        if (plan.start is not None) != (' AS "d" WHERE ' in physical.sql):
            raise DischargeError("the page does not start where its cursor says")
    restricted = {_quote(s.table) for s in plan.sources if s.policies}
    reads = _table_reads(physical.sql)
    for table in restricted:
        mine = [r for r in reads if r[0][-1] == table]
        if not any(is_restricted for _, _, is_restricted, _ in mine):
            raise DischargeError("a restricted dataset is not read through its restrictions")
        for _, alias, is_restricted, inside in mine:
            chained = inside and alias is not None and re.fullmatch(r'"#c[0-9]+"', alias)
            if not (is_restricted or chained):
                raise DischargeError("a restricted dataset is read without its restrictions")
    return evidence
