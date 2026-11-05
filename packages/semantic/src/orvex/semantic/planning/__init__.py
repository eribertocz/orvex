"""The planner (P2-12, #311; `semantic-engine.md` §4): how a question the correctness engine
accepted is answered, as a logical plan with no SQL.

`plan(report)` turns a `CorrectnessReport` into an immutable, fingerprinted `SemanticPlan`. It
supports exactly what the correctness engine accepts today: aggregation to a coarser grain
along many-to-one paths, and filters on members those paths reach.

- **One aggregation per fact** (C3, P2-16). `facts` names each fact's own source; every join,
  filter, group and aggregate belongs to the one fact whose joins reach its source. Facts are
  aggregated apart, each with every filter, and aligned on the requested grain only. A fact
  that reads a conformed dimension through its own field (C5) groups and filters by that field.
- **A metric over metrics is computed last** (S7): `derived` reads the aligned aggregates it
  names, row by row, and is never aggregated. A source metric the question did not ask for is
  aggregated but not `shown`.

- **Only reports `analyze` made are planned.** Any other report is an internal error, so no
  plan exists that skipped the policies.
- **Every obligation is discharged** by a named step; one left over is an internal error and
  the plan never runs (C9).
- **Each dataset carries its data route.** Only the managed lake runs here; any other route is
  refused (`UNSUPPORTED_OPERATION`, `operation: route`) until P2-29.
- The plan names members and relationships, never SQL: it is the same for every engine.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from orvex.semantic.canonical import fingerprint
from orvex.semantic.compiler.compiled import CompiledModel
from orvex.semantic.compiler.parsing import MemberId
from orvex.semantic.correctness import CorrectnessReport
from orvex.semantic.correctness.graph import Graph
from orvex.semantic.correctness.report import ISSUED
from orvex.semantic.errors import SemanticError
from orvex.semantic.ossie import sql

PLAN_FORMAT = 1
ROUTES = frozenset({"managed"})  # the data routes this release executes (ADR-0027)
ISSUED_PLAN = object()


class PlanError(RuntimeError):
    """An internal error: the plan cannot be trusted, and never runs."""


@dataclass(frozen=True, slots=True)
class Latest:
    """Period end (C2): of the fact's rows, keep for each entity and period only the one with
    the latest value of `axis`. The entity is the key without the axis, so the row is unique."""

    axis: MemberId
    entity: tuple[MemberId, ...]
    bucket: str | None  # the period: the axis's own value when None
    zone: str | None = None  # an instant axis: its period cut in this zone (C6)


@dataclass(frozen=True, slots=True)
class Source:
    alias: str  # t0 is the fact
    dataset: str  # normalized
    table: str  # the dataset's `source`, as the model writes it
    route: str
    latest: Latest | None = None


@dataclass(frozen=True, slots=True)
class AsOf:
    """A temporal relationship (C6): the right row valid when the left row's `event` happened,
    `valid_from <= event < valid_to`. Exactly one must match each left row the question reads,
    checked in the same read; `rows` are the left dataset's key fields."""

    event: MemberId
    valid_from: MemberId
    valid_to: MemberId
    rows: tuple[MemberId, ...]


@dataclass(frozen=True, slots=True)
class Join:
    """Rows of `right` matched to each row of `left`, on equal pairs of fields. Never more
    than one match: every step is many-to-one or one-to-one (C1)."""

    relationship: str
    left: str  # alias
    right: str  # alias
    pairs: tuple[tuple[MemberId, MemberId], ...]  # (left field, right field)
    as_of: AsOf | None = None  # a temporal relationship, guarded (C6)


@dataclass(frozen=True, slots=True)
class Group:
    name: str  # the output column
    member: MemberId
    source: str  # alias
    bucket: str | None
    zone: str | None = None  # an instant's bucket: cut in this zone (C6)


@dataclass(frozen=True, slots=True)
class Filter:
    member: MemberId
    source: str
    predicate: Mapping[str, Any]  # the bound query's canonical predicate


@dataclass(frozen=True, slots=True)
class Division:
    """A ratio's one division, after both components are aggregated (C2): rounded once to
    `scale` in `mode`, and `zero` (`null` or `reject`) for a zero denominator, as declared."""

    scale: int
    mode: str
    zero: str


@dataclass(frozen=True, slots=True)
class Allocation:
    """A metric counted through a bridge (C7): each fact row's value times its bridge row's
    `weight`, exactly. The weights of every normalization group read (the fact side's `scope`
    fields, matched to the bridge's) must add up to `total` within `tolerance`, in the same
    read, or the whole result is refused."""

    bridge: str  # the bridge dataset, as written
    source: str  # the bridge's alias
    weight: MemberId
    scope: tuple[tuple[MemberId, MemberId], ...]  # (fact-side field, bridge field)
    side: str  # the alias of the fact-side fields
    total: str
    tolerance: str


@dataclass(frozen=True, slots=True)
class Aggregate:
    name: str
    metric: MemberId
    source: str
    discharges: tuple[str, ...]  # obligation ids
    division: Division | None = None  # a ratio: its components, then this division
    shown: bool = True  # False: only read by a metric over metrics
    allocation: Allocation | None = None  # through a bridge (C7), rounded by `division`


@dataclass(frozen=True, slots=True)
class Derived:
    """A metric over metrics (S7): computed after alignment from the aggregates `over` names,
    row by row; a ratio by its one `division` (C2), with `over` its numerator and denominator."""

    name: str
    metric: MemberId
    over: tuple[str, ...]  # aggregate names
    discharges: tuple[str, ...]
    division: Division | None = None


@dataclass(frozen=True, slots=True)
class Window:
    """A running total (S9): the aggregate `over` summed over the aggregated rows, in the
    order of the groups `order` names, restarting at each value of the last `partitions` of
    them; computed after aggregation, never aggregated again."""

    name: str
    metric: MemberId
    over: str  # an aggregate name
    order: tuple[str, ...]  # group names: the order, then the partitions
    partitions: int
    discharges: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Sort:
    name: str  # an output column
    descending: bool
    nulls_first: bool


@dataclass(frozen=True, slots=True)
class SemanticPlan:
    report_fingerprint: str
    sources: tuple[Source, ...]
    joins: tuple[Join, ...]
    filters: tuple[Filter, ...]
    groups: tuple[Group, ...]
    aggregates: tuple[Aggregate, ...]
    order: tuple[Sort, ...]  # the query's order, then every group as the tie-breaker
    limit: int | None
    fingerprint: str
    report: CorrectnessReport = field(compare=False, repr=False)
    facts: tuple[str, ...] = ("t0",)  # each fact's source, aggregated apart (C3)
    derived: tuple[Derived, ...] = ()  # computed after alignment (S7)
    outputs: tuple[str, ...] = ()  # with `derived` or `windows`: every metric column, in order
    windows: tuple[Window, ...] = ()  # computed after aggregation (S9)
    # Filters on a running total's own groups: they choose the rows shown, after it (S9).
    after: tuple[Filter, ...] = ()
    # Set only by `plan`, after construction: a copy (`dataclasses.replace`) has none.
    issued: object = field(default=None, init=False, compare=False, repr=False)

    @property
    def compiled(self) -> CompiledModel:
        compiled: CompiledModel = self.report.compiled
        return compiled


def _key(member: MemberId) -> list[str]:
    return [member.kind, member.dataset or "", member.name]


def _document(plan: dict[str, Any]) -> dict[str, object]:
    return (
        {
            "report": plan["report_fingerprint"],
            "sources": [
                [s.alias, s.dataset, s.table, s.route]
                + (
                    [
                        [_key(s.latest.axis), [_key(e) for e in s.latest.entity], s.latest.bucket]
                        + ([s.latest.zone] if s.latest.zone else [])
                    ]
                    if s.latest
                    else []
                )
                for s in plan["sources"]
            ],
            "joins": [
                [j.relationship, j.left, j.right, [[_key(a), _key(b)] for a, b in j.pairs]]
                + ([_as_of(j.as_of)] if j.as_of else [])
                for j in plan["joins"]
            ],
            "filters": [[_key(f.member), f.source, _plain(f.predicate)] for f in plan["filters"]],
            "groups": [
                [g.name, _key(g.member), g.source, g.bucket] + ([g.zone] if g.zone else [])
                for g in plan["groups"]
            ],
            "aggregates": [
                [
                    a.name,
                    _key(a.metric),
                    a.source,
                    list(a.discharges),
                    [a.division.scale, a.division.mode, a.division.zero] if a.division else None,
                ]
                + ([] if a.shown else ["hidden"])
                + ([_allocation(a.allocation)] if a.allocation else [])
                for a in plan["aggregates"]
            ],
            "order": [[s.name, s.descending, s.nulls_first] for s in plan["order"]],
            "limit": plan["limit"],
        }
        | ({"facts": list(plan["facts"])} if len(plan["facts"]) > 1 else {})
        | (
            {
                "derived": [
                    [
                        d.name,
                        _key(d.metric),
                        list(d.over),
                        list(d.discharges),
                        [d.division.scale, d.division.mode, d.division.zero]
                        if d.division
                        else None,
                    ]
                    for d in plan["derived"]
                ],
                "outputs": list(plan["outputs"]),
            }
            if plan["derived"]
            else {}
        )
        | (
            {
                "windows": [
                    [
                        w.name,
                        _key(w.metric),
                        w.over,
                        list(w.order),
                        w.partitions,
                        list(w.discharges),
                    ]
                    for w in plan["windows"]
                ],
                "after": [[_key(f.member), f.source, _plain(f.predicate)] for f in plan["after"]],
                "outputs": list(plan["outputs"]),
            }
            if plan["windows"]
            else {}
        )
    )


def _allocation(found: Allocation) -> list[object]:
    return [
        found.bridge,
        found.source,
        _key(found.weight),
        [[_key(x), _key(y)] for x, y in found.scope],
        found.side,
        found.total,
        found.tolerance,
    ]


def _as_of(found: AsOf) -> list[object]:
    return [
        _key(found.event),
        _key(found.valid_from),
        _key(found.valid_to),
        [_key(r) for r in found.rows],
    ]


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


def _member(key: Any) -> MemberId:  # noqa: ANN401
    kind, dataset, name = key
    return MemberId("field" if kind == "field" else "metric", dataset or None, name)


class _Planner:
    def __init__(self, report: CorrectnessReport) -> None:
        self.report = report
        self.compiled: CompiledModel = report.compiled
        self.canonical = report.bound.canonical
        self.graph = Graph(self.compiled)
        self.written = {w: n for n, w in self.graph.relationship_names.items()}
        document = self.compiled.ossie.document
        self.relationships = {
            sql.normalize(str(r["name"])): r for r in document.get("relationships", ())
        }
        self.tables = {
            sql.normalize(str(d["name"])): str(d.get("source", d["name"]))
            for d in document.get("datasets", ())
        }
        dataset = {w: n for n, w in self.graph.names.items()}
        self.home_of = {m["metric"]: dataset[m["home"]] for m in report.facts["metrics"]}
        self.sources: list[Source] = []
        self.aliases: dict[tuple[str, tuple[str, ...]], str] = {}
        self.joins: list[Join] = []
        self.paths = {
            (dataset[p["from"]], p["member"], p["slot"], p["role"]): tuple(
                self.written[r] for r in p["relationships"]
            )
            for p in report.facts["paths"]
        }
        # A fact reading a conformed dimension through its own field (C5).
        self.conformed = {
            (dataset[p["from"]], p["member"], p["slot"], p["role"]): p["as"]
            for p in report.facts["paths"]
            if "as" in p
        }
        self.derived = {d["metric"]: d for d in report.facts.get("derived", ())}
        self.windows = {w["metric"]: w for w in report.facts.get("windows", ())}
        self.after = set(report.facts.get("after", ()))
        self.zones = {
            (g["member"], g["bucket"]): g["zone"] for g in report.facts["grain"] if "zone" in g
        }
        self.allocated = {
            m: a["bridge"] for a in report.facts.get("allocations", ()) for m in a["metrics"]
        }

    def read_as(self, home: str, member: MemberId, slot: str, role: str | None) -> MemberId:
        """The member the fact `home` reads for `member`: itself, or its conformed field."""
        written = self.graph.relationship_names.get(role, role) if role else None
        label = self.conformed.get((home, self.label(member), slot, written))
        if label is None:
            return member
        return next(m for m in self.compiled.members if self.label(m) == label)

    def source(self, alias: str, dataset: str) -> Source:
        route = "managed"  # every dataset is in the managed lake until route B (P2-29)
        if route not in ROUTES:
            raise SemanticError("UNSUPPORTED_OPERATION", {"operation": "route"})
        return Source(alias, dataset, self.tables[dataset], route)

    def label(self, member: MemberId) -> str:
        return self.compiled.require(member).parsed.label

    def allocation(self, label: str) -> Allocation | None:
        """The bridge a metric is counted through, from the join that enters it."""
        written = self.allocated.get(label)
        if written is None:
            return None
        bridge = next(n for n, w in self.graph.names.items() if w == written)
        declared = self.graph.payloads[bridge]["bridge"]
        left = sql.normalize(str(declared["left"]))
        aliases = {s.alias: s.dataset for s in self.sources}
        entering = [j for j in self.joins if j.relationship == left and aliases[j.right] == bridge]
        if len(entering) != 1:
            raise PlanError(f"the bridge {written} is entered {len(entering)} times")
        (join,) = entering
        return Allocation(
            written,
            join.right,
            MemberId("field", bridge, sql.normalize(str(declared["weight"]))),
            join.pairs,
            join.left,
            str(declared["total"]),
            str(declared["tolerance"]),
        )

    def member(self, label: str) -> MemberId:
        return next(m for m in self.compiled.members if self.label(m) == label)

    def label_of(self, node: object) -> str:
        """The label of the metric a bare reference names."""
        assert isinstance(node, sql.Ref)  # `analyze` accepted only bare metric references
        written = ".".join(node.parts)
        return next(
            self.label(m)
            for m in self.compiled.members
            if m.kind == "metric" and sql.normalize(self.label(m)) == written
        )

    def fact(self, home: str) -> str:
        """The source of a fact: one per home dataset, never shared with another fact."""
        if (home, ()) not in self.aliases:
            alias = f"t{len(self.sources)}"
            self.sources.append(self.source(alias, home))
            self.aliases[home, ()] = alias
        return self.aliases[home, ()]

    def alias(self, home: str, member: MemberId, slot: str, role: str | None = None) -> str:
        """The source holding `member` for the fact `home`, joining along its path once."""
        written = self.graph.relationship_names.get(role, role) if role else None
        path = self.paths.get((home, self.label(member), slot, written), ())
        if not path and self.conformed.get((home, self.label(member), slot, written)):
            label = self.conformed[home, self.label(member), slot, written]
            path = self.paths.get((home, label, slot, None), ())
        at, current = self.fact(home), home
        for depth in range(1, len(path) + 1):
            prefix = path[:depth]
            name = prefix[-1]
            step = next(s for s in self.graph.links[current] if s.relationship == name)
            if (home, prefix) not in self.aliases:
                alias = f"t{len(self.sources)}"
                self.sources.append(self.source(alias, step.to))
                self.aliases[home, prefix] = alias
                self.joins.append(self.join(name, current, at, step.to, alias))
            at, current = self.aliases[home, prefix], step.to
        return at

    def join(self, name: str, left: str, left_alias: str, right: str, right_alias: str) -> Join:
        relationship = self.relationships[name]
        start = sql.normalize(str(relationship["from"]))
        own = [sql.normalize(str(c)) for c in relationship["from_columns"]]
        other = [sql.normalize(str(c)) for c in relationship["to_columns"]]
        if start != left:  # the reverse of a one-to-one step
            own, other = other, own
        pairs = tuple(
            (MemberId("field", left, a), MemberId("field", right, b))
            for a, b in zip(own, other, strict=True)
        )
        temporal = self.graph.relationship_payloads.get(name, {}).get("temporal")
        as_of = None
        if isinstance(temporal, Mapping):  # `analyze` accepted it, crossed forward only

            def field(ref: object, side: str) -> MemberId:
                parts = [sql.normalize(p) for p in str(ref).split(".")]
                return MemberId("field", parts[0] if len(parts) == 2 else side, parts[-1])

            as_of = AsOf(
                field(temporal["event_time"], left),
                field(temporal["valid_from"], right),
                field(temporal["valid_to"], right),
                tuple(MemberId("field", left, k) for k in self.graph.keys[left]),
            )
        return Join(name, left_alias, right_alias, pairs, as_of)

    def names(self, members: list[MemberId]) -> dict[MemberId, str]:
        """Each output column's name: the member's own name, or its label when two collide."""
        written = {m: self.label(m).rsplit(".", 1)[-1] for m in members}
        counts: dict[str, int] = {}
        for name in written.values():
            counts[name] = counts.get(name, 0) + 1
        return {m: (n if counts[n] == 1 else self.label(m)) for m, n in written.items()}

    def build(self) -> SemanticPlan:
        canonical = self.canonical
        group_keys = [(_member(m), role, bucket) for m, role, bucket in canonical["group_by"]]
        metric_keys = [_member(m) for m, _, _ in canonical["metrics"]]
        members = [m for m, _, _ in group_keys] + metric_keys
        names = self.names(list(dict.fromkeys(members)))
        sources: list[MemberId] = []  # every aggregated metric, in the question's order
        for metric in metric_keys:
            derived = self.derived.get(self.label(metric))
            window = self.windows.get(self.label(metric))
            over = [self.member(o) for o in derived["over"]] if derived else [metric]
            over = [self.member(window["over"])] if window else over
            sources += [m for m in over if m not in sources]
        homes = list(dict.fromkeys(self.home_of[self.label(m)] for m in sources))
        facts = tuple(self.fact(home) for home in homes)
        filters = tuple(
            Filter(
                self.read_as(home, _member(f["member"]), "filter", f["role"]),
                self.alias(home, _member(f["member"]), "filter", f["role"]),
                f["predicate"],
            )
            for home in homes
            for f in canonical["filters"]
            if self.label(_member(f["member"])) not in self.after
        )
        roles_of: dict[MemberId, set[str | None]] = {}
        for member, role, _ in group_keys:
            roles_of.setdefault(member, set()).add(role)
        groups: list[Group] = []
        for home in homes:  # the same grain in every fact, each from its own sources
            for member, role, bucket in group_keys:
                unit = bucket[0] if bucket else None
                name = names[member]
                if len(roles_of[member]) > 1 and role:  # one dimension through two roles
                    name = f"{name}_{self.graph.relationship_names[role]}"
                group = Group(
                    name,
                    self.read_as(home, member, "group_by", role),
                    self.alias(home, member, "group_by", role),
                    unit,
                    self.zones.get((self.label(member), unit)),
                )
                if group not in groups:
                    groups.append(group)
        obligations: dict[str, list[str]] = {}
        for obligation in self.report.obligations:
            obligations.setdefault(obligation.metric, []).append(obligation.id)
        payloads = {
            e.pointer.rsplit("/custom_extensions/", 1)[0]: e.payload
            for e in self.compiled.ossie.product_extensions
        }

        def division_of(metric: MemberId) -> Division | None:
            payload = payloads.get(self.compiled.require(metric).parsed.pointer, {})
            if self.label(metric) in self.allocated:  # rounded once, after the sum (C7)
                rounding = payload["rounding"]
                assert isinstance(rounding, Mapping)  # `analyze` required it
                return Division(int(rounding["scale"]), str(rounding["mode"]), "null")
            if payload.get("additivity") != "recomputable":
                return None
            rounding = payload["rounding"]
            assert isinstance(rounding, Mapping)  # `ossie-extensions` v1, checked on read
            return Division(
                int(rounding["scale"]), str(rounding["mode"]), str(payload["zero_denominator"])
            )

        asked = set(metric_keys)
        column = {m: names[m] if m in asked else f"#{self.label(m)}" for m in sources}
        aggregates = []
        for metric in sources:
            label = self.label(metric)
            discharges = tuple(sorted(obligations[label]))
            source = self.fact(self.home_of[label])
            aggregates.append(
                Aggregate(
                    column[metric],
                    metric,
                    source,
                    discharges,
                    division_of(metric),
                    metric in asked,
                    self.allocation(label),
                )
            )
        window_steps = []
        for metric in dict.fromkeys(metric_keys):
            found_window = self.windows.get(self.label(metric))
            if found_window is None:
                continue
            by_label = {self.label(g.member): g.name for g in groups}  # one fact: every group
            window_steps.append(
                Window(
                    names[metric],
                    metric,
                    column[self.member(found_window["over"])],
                    tuple(by_label[label] for label in found_window["order"]),
                    int(found_window["partitions"]),
                    tuple(sorted(obligations[self.label(metric)])),
                )
            )
        after = tuple(
            Filter(
                _member(f["member"]),
                next(
                    g.name
                    for g in groups
                    if self.label(g.member) == self.label(_member(f["member"]))
                ),
                f["predicate"],
            )
            for f in canonical["filters"]
            if self.label(_member(f["member"])) in self.after
        )
        derived_steps = []
        for metric in dict.fromkeys(metric_keys):
            found = self.derived.get(self.label(metric))
            if found is None:
                continue
            inputs = tuple(column[self.member(o)] for o in found["over"])
            division = division_of(metric)
            if division is not None:  # its numerator, then its denominator
                trees = dict(self.compiled.require(metric).typed.payload)
                ends = [
                    self.label_of(trees[p].node)
                    for p in ("components.numerator", "components.denominator")
                ]
                inputs = tuple(column[self.member(e)] for e in ends)
            discharges = tuple(sorted(obligations[self.label(metric)]))
            derived_steps.append(Derived(names[metric], metric, inputs, discharges, division))
        order = [
            Sort(names[_member(m)], direction == "desc", nulls == "first")
            for m, direction, nulls in canonical["order"]
        ]
        ordered = {s.name for s in order}
        output = list(dict.fromkeys(g.name for g in groups))
        order += [Sort(name, False, False) for name in output if name not in ordered]
        ends = [
            m["period"]["axis"]
            for m in self.report.facts["metrics"]
            if m.get("period") and m["period"]["behavior"] == "period_end"
        ]
        if ends:
            axis = next(m for m in self.compiled.members if self.label(m) == ends[0])
            home = axis.dataset or ""
            alias = self.fact(home)
            period = next((g for g in groups if g.member == axis and g.source == alias), None)
            bucket = period.bucket if period else None
            zone = period.zone if period else None
            # `analyze` accepted the key as exactly the declared entity plus the axis.
            entity = tuple(
                MemberId("field", home, key) for key in self.graph.keys[home] if key != axis.name
            )
            at = next(i for i, s in enumerate(self.sources) if s.alias == alias)
            fact = self.sources[at]
            self.sources[at] = Source(
                fact.alias, fact.dataset, fact.table, fact.route, Latest(axis, entity, bucket, zone)
            )
        discharged = {o for a in aggregates for o in a.discharges}
        discharged |= {o for d in derived_steps for o in d.discharges}
        discharged |= {o for w in window_steps for o in w.discharges}
        left = {o.id for o in self.report.obligations} - discharged
        if left:
            raise PlanError(f"obligations left undischarged: {sorted(left)}")
        parts: dict[str, Any] = {
            "report_fingerprint": self.report.fingerprint,
            "sources": tuple(self.sources),
            "joins": tuple(self.joins),
            "filters": filters,
            "groups": tuple(groups),
            "aggregates": tuple(aggregates),
            "order": tuple(order),
            "limit": int(canonical["limit"]) if canonical.get("limit") is not None else None,
            "facts": facts,
            "derived": tuple(derived_steps),
            "outputs": tuple(names[m] for m in dict.fromkeys(metric_keys))
            if derived_steps or window_steps
            else (),
            "windows": tuple(window_steps),
            "after": after,
        }
        found = SemanticPlan(
            **parts,
            fingerprint=fingerprint("semantic_plan", PLAN_FORMAT, _document(parts)),
            report=self.report,
        )
        object.__setattr__(found, "issued", ISSUED_PLAN)
        return found


def plan(report: CorrectnessReport) -> SemanticPlan:
    """The plan of a report `analyze` made, or an internal error."""
    if report.issued is not ISSUED:
        raise PlanError("not a report of the correctness engine")
    return _Planner(report).build()
