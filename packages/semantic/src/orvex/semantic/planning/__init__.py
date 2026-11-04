"""The planner (P2-12, #311; `semantic-engine.md` §4): how a question the correctness engine
accepted is answered, as a logical plan with no SQL.

`plan(report)` turns a `CorrectnessReport` into an immutable, fingerprinted `SemanticPlan`. It
supports exactly what the correctness engine accepts today: one fact, additive aggregation to
a coarser grain along many-to-one paths, and filters on members those paths reach.

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


@dataclass(frozen=True, slots=True)
class Source:
    alias: str  # t0 is the fact
    dataset: str  # normalized
    table: str  # the dataset's `source`, as the model writes it
    route: str
    latest: Latest | None = None


@dataclass(frozen=True, slots=True)
class Join:
    """Rows of `right` matched to each row of `left`, on equal pairs of fields. Never more
    than one match: every step is many-to-one or one-to-one (C1)."""

    relationship: str
    left: str  # alias
    right: str  # alias
    pairs: tuple[tuple[MemberId, MemberId], ...]  # (left field, right field)


@dataclass(frozen=True, slots=True)
class Group:
    name: str  # the output column
    member: MemberId
    source: str  # alias
    bucket: str | None


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
class Aggregate:
    name: str
    metric: MemberId
    source: str
    discharges: tuple[str, ...]  # obligation ids
    division: Division | None = None  # a ratio: its components, then this division


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
    # Set only by `plan`, after construction: a copy (`dataclasses.replace`) has none.
    issued: object = field(default=None, init=False, compare=False, repr=False)

    @property
    def compiled(self) -> CompiledModel:
        compiled: CompiledModel = self.report.compiled
        return compiled


def _key(member: MemberId) -> list[str]:
    return [member.kind, member.dataset or "", member.name]


def _document(plan: dict[str, Any]) -> dict[str, object]:
    return {
        "report": plan["report_fingerprint"],
        "sources": [
            [s.alias, s.dataset, s.table, s.route]
            + (
                [[_key(s.latest.axis), [_key(e) for e in s.latest.entity], s.latest.bucket]]
                if s.latest
                else []
            )
            for s in plan["sources"]
        ],
        "joins": [
            [j.relationship, j.left, j.right, [[_key(a), _key(b)] for a, b in j.pairs]]
            for j in plan["joins"]
        ],
        "filters": [[_key(f.member), f.source, _plain(f.predicate)] for f in plan["filters"]],
        "groups": [[g.name, _key(g.member), g.source, g.bucket] for g in plan["groups"]],
        "aggregates": [
            [
                a.name,
                _key(a.metric),
                a.source,
                list(a.discharges),
                [a.division.scale, a.division.mode, a.division.zero] if a.division else None,
            ]
            for a in plan["aggregates"]
        ],
        "order": [[s.name, s.descending, s.nulls_first] for s in plan["order"]],
        "limit": plan["limit"],
    }


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
        home = report.facts["metrics"][0]["home"]
        self.home = next(n for n, w in self.graph.names.items() if w == home)
        self.sources: list[Source] = [self.source("t0", self.home)]
        self.aliases: dict[tuple[str, ...], str] = {(): "t0"}
        self.joins: list[Join] = []
        self.paths = {
            (p["member"], p["slot"]): tuple(self.written[r] for r in p["relationships"])
            for p in report.facts["paths"]
        }

    def source(self, alias: str, dataset: str) -> Source:
        route = "managed"  # every dataset is in the managed lake until route B (P2-29)
        if route not in ROUTES:
            raise SemanticError("UNSUPPORTED_OPERATION", {"operation": "route"})
        return Source(alias, dataset, self.tables[dataset], route)

    def label(self, member: MemberId) -> str:
        return self.compiled.require(member).parsed.label

    def alias(self, member: MemberId, slot: str) -> str:
        """The source holding `member`, joining along its path from the fact once."""
        path = self.paths.get((self.label(member), slot), ())
        at, current = "t0", self.home
        for depth in range(1, len(path) + 1):
            prefix = path[:depth]
            name = prefix[-1]
            step = next(s for s in self.graph.links[current] if s.relationship == name)
            if prefix not in self.aliases:
                alias = f"t{len(self.sources)}"
                self.sources.append(self.source(alias, step.to))
                self.aliases[prefix] = alias
                self.joins.append(self.join(name, current, at, step.to, alias))
            at, current = self.aliases[prefix], step.to
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
        return Join(name, left_alias, right_alias, pairs)

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
        filters = tuple(
            Filter(_member(f["member"]), self.alias(_member(f["member"]), "filter"), f["predicate"])
            for f in canonical["filters"]
        )
        groups: list[Group] = []
        for member, _, bucket in group_keys:
            unit = bucket[0] if bucket else None
            group = Group(names[member], member, self.alias(member, "group_by"), unit)
            if group not in groups:
                groups.append(group)
        obligations: dict[str, list[str]] = {}
        for obligation in self.report.obligations:
            obligations.setdefault(obligation.metric, []).append(obligation.id)
        payloads = {
            e.pointer.rsplit("/custom_extensions/", 1)[0]: e.payload
            for e in self.compiled.ossie.product_extensions
        }
        aggregates = []
        for metric in dict.fromkeys(metric_keys):
            label = self.label(metric)
            payload = payloads.get(self.compiled.require(metric).parsed.pointer, {})
            division = None
            if payload.get("additivity") == "recomputable":
                rounding = payload["rounding"]
                assert isinstance(rounding, Mapping)  # `ossie-extensions` v1, checked on read
                division = Division(
                    int(rounding["scale"]), str(rounding["mode"]), str(payload["zero_denominator"])
                )
            discharges = tuple(sorted(obligations[label]))
            aggregates.append(Aggregate(names[metric], metric, "t0", discharges, division))
        order = [
            Sort(names[_member(m)], direction == "desc", nulls == "first")
            for m, direction, nulls in canonical["order"]
        ]
        ordered = {s.name for s in order}
        order += [Sort(g.name, False, False) for g in groups if g.name not in ordered]
        ends = [
            m["period"]["axis"]
            for m in self.report.facts["metrics"]
            if m.get("period") and m["period"]["behavior"] == "period_end"
        ]
        if ends:
            axis = next(m for m in self.compiled.members if self.label(m) == ends[0])
            bucket = next((g.bucket for g in groups if g.member == axis), None)
            # `analyze` accepted the key as exactly the declared entity plus the axis.
            entity = tuple(
                MemberId("field", self.home, key)
                for key in self.graph.keys[self.home]
                if key != axis.name
            )
            fact = self.sources[0]
            self.sources[0] = Source(
                fact.alias, fact.dataset, fact.table, fact.route, Latest(axis, entity, bucket)
            )
        discharged = {o for a in aggregates for o in a.discharges}
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
