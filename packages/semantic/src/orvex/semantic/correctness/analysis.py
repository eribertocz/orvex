"""The correctness engine (P2-11, #310; `semantic-engine.md` §4 and §5).

`analyze(bound, compiled, policies, context)` decides, before any SQL exists, whether a bound
query has exactly one correct answer. It is pure and deterministic: it reads no data and writes
no SQL. It returns a `CorrectnessReport`, or raises a `SemanticError` with a stable code.

**Policies come first** (P2-08), and are never bypassed:
- a member the caller may not see is refused as `MEMBER_NOT_FOUND`. `bind_query(hidden=...)`
  gives that refusal the caller's own text, so it cannot be told from a member that does not
  exist; here it is checked again, for a query bound without it;
- a masked member used to group, filter or sort is refused as `MEMBER_MASKED`. A metric that
  reads a masked member is already hidden (P2-08), so a distinct count over one is not found;
- a dataset that a policy not yet executable makes unusable, or that a row policy reaches, is
  refused as `UNSUPPORTED_OPERATION` (`row_policy` until P2-21), never read without it.

**Fail closed.** Every query shape needs the rules in `SHAPES`. A shape whose rule is not
implemented yet is refused as `UNSUPPORTED_OPERATION`, its `operation` naming the shape; it
never reaches the planner. Only C1 is implemented, so a query is answered only when it reads
one fact, every metric is additive, and every member it groups, filters or sorts by is reached
from that fact through relationships that never multiply its rows.

**C1, the grain algebra.** A metric is aggregated from the natural grain of its home dataset
(its declared `grain`, or its primary key) to the requested grain. Each requested member must be
reached from the home dataset along exactly one path of many-to-one (or one-to-one) steps:
- no path at all: `ILLEGAL_GRAIN_TRANSITION`;
- more than one such path, with no role to choose: `AMBIGUOUS_RELATIONSHIP_PATH` (C4's
  placeholder until P2-15);
- only paths with a one-to-many step: `fanout` (C3, P2-16); through a bridge: `bridge_allocation`
  (C7, P2-18); an as-of relationship: `as_of_join` (C6, P2-20).

C1 validates the grain; it never authorizes an aggregation by itself. The metric's additivity
must be `additive`, declared or derived (a plain `SUM` or `COUNT` with no `DISTINCT`); any other
class is refused until its rule lands, and a metric with neither is refused as
`MISSING_ADDITIVITY_DECLARATION`. Each accepted metric gives one obligation,
`C1.aggregate:<metric>`, which the planner's aggregation step discharges (P2-12).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from orvex.semantic.compiler.compiled import CompiledMember, CompiledModel
from orvex.semantic.compiler.parsing import MemberId
from orvex.semantic.correctness.graph import Graph, Path
from orvex.semantic.correctness.report import CorrectnessReport, Obligation, make_report
from orvex.semantic.errors import SemanticError
from orvex.semantic.ossie import sql
from orvex.semantic.policies import PolicyModel, read_caller
from orvex.semantic.query.binding import BoundQuery

RULES: tuple[tuple[str, int], ...] = (("C1", 1),)  # implemented, in the order they run
IMPLEMENTED = frozenset(rule for rule, _ in RULES)


@dataclass(frozen=True, slots=True)
class Shape:
    rule: str  # the rule that answers it
    roadmap: str  # where that rule lands


# Every shape a query can have, and the rule it needs, in the order refusals are checked. A
# later issue that implements a rule adds it to `RULES`; the tests list every corpus case.
SHAPES: Mapping[str, Shape] = {
    "aggregate": Shape("C1", "P2-11"),
    "detail_rows": Shape("detail rows", "P2-24"),
    "recomputable": Shape("C2", "P2-13"),
    "holistic": Shape("C2", "P2-14"),
    "semi_additive": Shape("C2", "P2-14"),
    "non_additive": Shape("C2", "P2-14"),
    "multi_fact": Shape("C5", "P2-17"),
    "fanout": Shape("C3", "P2-16"),
    "calendar_bucket": Shape("C6", "P2-19"),
    "zoned_bucketing": Shape("C6", "P2-19"),
    "ordered": Shape("C6", "P2-20"),
    "as_of_join": Shape("C6", "P2-20"),
    "bridge_allocation": Shape("C7", "P2-18"),
    "metric_context": Shape("S1", "P4"),
    "level_of_detail": Shape("S1", "P4"),
    "over_metrics": Shape("S7", "#237"),
    "filter_context": Shape("S8", "#237"),
    "context_modifier": Shape("S8", "#237"),
    "reaggregable_state": Shape("S11", "#237"),
    "as_known_at": Shape("S12", "#237"),
    "metric_filter": Shape("S7-S14", "#237"),
    "top_n": Shape("S7-S14", "#237"),
    "coverage": Shape("S7-S14", "#237"),
    "segment_filter": Shape("S7-S14", "#237"),
    "hierarchy_filter": Shape("S7-S14", "#237"),
    "metric_extension": Shape("unknown declaration", "a later issue"),
}
# A metric declaration that changes how it is computed needs its own rule. Any declaration not
# listed here or in `_PLAIN_DECLARATIONS` refuses the query (`metric_extension`).
_DECLARATION_SHAPES: Mapping[str, str] = {
    "window": "ordered",
    "allocation": "bridge_allocation",
    "components": "recomputable",
    "non_additive_axes": "semi_additive",
    "period_end": "semi_additive",
    "context": "metric_context",
    "level_of_detail": "level_of_detail",
    "over_metrics": "over_metrics",
    "reaggregable_state": "reaggregable_state",
}
_PLAIN_DECLARATIONS = frozenset(
    {
        "version",
        "element",
        "additivity",
        "aggregate_class",
        "approximate",
        "currency",
        "unit",
        "home_dataset",
        "rounding",
        "zero_denominator",
        "format",
        "owner",
        "status",
        "deprecation",
    }
)
_DERIVED_ADDITIVE = frozenset({"SUM", "COUNT"})


def _refuse_pending(shapes: set[str]) -> None:
    for shape, needs in SHAPES.items():
        if shape in shapes and needs.rule not in IMPLEMENTED:
            raise SemanticError("UNSUPPORTED_OPERATION", {"operation": shape})


def _member(key: Sequence[str]) -> MemberId:
    kind, dataset, name = key
    return MemberId("field" if kind == "field" else "metric", dataset or None, name)


@dataclass(frozen=True, slots=True)
class _Row:
    """A member the query groups, filters or sorts by."""

    member: MemberId
    role: str | None
    bucket: str | None
    slot: str  # MEMBER_MASKED's operation


class _Query:
    """The bound query's members, by the slot each one fills."""

    def __init__(self, bound: BoundQuery) -> None:
        canonical = bound.canonical
        self.mode = str(canonical["mode"])
        self.shapes: set[str] = set()
        self.metrics: list[MemberId] = []
        self.rows: list[_Row] = []
        self.grouped: list[_Row] = []
        for entry in canonical.get("filters", ()):
            self._filter(entry, "filter")
        for member, _, _ in canonical.get("order", ()):
            found = _member(member)
            if found.kind == "metric":
                self.metrics.append(found)
            else:
                self.rows.append(_Row(found, None, None, "sort"))
        if self.mode != "aggregate":
            self.shapes.add("detail_rows")
            return
        self.shapes.add("aggregate")
        for member, context, apply in canonical["metrics"]:
            self.metrics.append(_member(member))
            if context is not None or apply:
                self.shapes.add("metric_context")
        for member, role, bucket in canonical["group_by"]:
            row = _Row(_member(member), role, bucket[0] if bucket else None, "group_by")
            if bucket and bucket[1] is not None:
                self.shapes.add("calendar_bucket")
            self.rows.append(row)
            self.grouped.append(row)
        for _, filters in canonical.get("contexts", ()):
            self.shapes.add("filter_context")
            for entry in filters:
                self._filter(entry, "filter")
        for _, metric, dimension, grain, _ in canonical.get("metric_filters", ()):
            self.shapes.add("metric_filter")
            self.metrics.append(_member(metric))
            for member in (dimension, *grain):
                self.rows.append(_Row(_member(member), None, None, "group_by"))
        top = canonical.get("top_n")
        if top is not None:
            self.shapes.add("top_n")
            self.metrics.append(_member(top["by"]))
            for member in top["per"]:
                self.rows.append(_Row(_member(member), None, None, "group_by"))
        for _, _, members in canonical.get("context_modifiers", ()):
            self.shapes.add("context_modifier")
            for member in members:
                self.rows.append(_Row(_member(member), None, None, "filter"))
        if canonical.get("coverage") is not None:
            self.shapes.add("coverage")
        if canonical.get("as_known_at") is not None:
            self.shapes.add("as_known_at")

    def _filter(self, entry: Mapping[str, Any], slot: str) -> None:
        self.rows.append(_Row(_member(entry["member"]), entry["role"], None, slot))
        op = entry["predicate"]["op"]
        if op == "in_segment":
            self.shapes.add("segment_filter")
        elif op == "descendants_of":
            self.shapes.add("hierarchy_filter")


class _Analysis:
    def __init__(
        self,
        bound: BoundQuery,
        compiled: CompiledModel,
        policies: PolicyModel,
        groups: Sequence[str],
    ) -> None:
        self.bound = bound
        self.compiled = compiled
        self.policies = policies
        self.groups = tuple(groups)
        self.graph = Graph(compiled)
        self.payloads = {
            e.pointer.rsplit("/custom_extensions/", 1)[0]: e.payload
            for e in compiled.ossie.product_extensions
        }
        self.query = _Query(bound)

    # names, as the model writes them

    def label(self, member: MemberId) -> str:
        return self.compiled.require(member).parsed.label

    def dataset(self, name: str) -> str:
        return self.graph.names.get(name, name)

    def key_label(self, dataset: str, key: str) -> str:
        member = MemberId("field", dataset, key)
        if member in self.compiled.members:
            return self.label(member)
        return f"{self.dataset(dataset)}.{key}"

    # policies, first

    def check_policies(self) -> None:
        for member in self.bound.members:
            if self.policies.protection(member, self.groups) == "hidden":
                raise SemanticError("MEMBER_NOT_FOUND", {"member": self.label(member)})
        for row in self.query.rows:  # a detail page's own fields may be masked (P2-24)
            if self.policies.protection(row.member, self.groups) == "masked":
                raise SemanticError(
                    "MEMBER_MASKED", {"member": self.label(row.member), "operation": row.slot}
                )
        self.check_datasets(self.read_datasets())

    def read_datasets(self) -> set[str]:
        found: set[str] = set()
        for member in self.bound.members:
            compiled = self.compiled.require(member)
            found |= self.home(member, compiled)
        return found

    def check_datasets(self, datasets: set[str]) -> None:
        self.policies.check_usable(datasets)
        for rule in self.policies.row_policies:
            reached = {rule.dataset, *(r.dataset for r in self.policies.propagation[rule.name])}
            if reached & datasets:
                raise SemanticError("UNSUPPORTED_OPERATION", {"operation": "row_policy"})

    # members

    def home(self, member: MemberId, compiled: CompiledMember) -> set[str]:
        found = {f.dataset for f in compiled.parsed.lineage if f.dataset is not None}
        if member.dataset is not None:
            found.add(member.dataset)
        declared = self.payloads.get(compiled.parsed.pointer, {}).get("home_dataset")
        if declared is not None:
            found.add(sql.normalize(str(declared)))
        return found

    def answerable(self, member: MemberId) -> None:
        """A declaration the member needs and lacks refuses the query (#280 §3)."""
        compiled = self.compiled.require(member)
        for declaration in compiled.missing:
            if declaration == "additivity":
                continue  # decided below, with what can be derived
            raise SemanticError(
                "MISSING_EXTENSION_DECLARATION",
                {"element": member.kind, "name": self.label(member), "declaration": declaration},
            )

    def declaration_shapes(self, metric: MemberId) -> set[str]:
        """The shapes the metric's declarations need, and those of every metric it reads."""
        shapes: set[str] = set()
        pending, seen = [metric], {metric}
        while pending:
            current = pending.pop()
            compiled = self.compiled.require(current)
            for key in self.payloads.get(compiled.parsed.pointer, {}):
                if key in _DECLARATION_SHAPES:
                    shapes.add(_DECLARATION_SHAPES[key])
                elif key not in _PLAIN_DECLARATIONS:
                    shapes.add("metric_extension")
            for dependency in compiled.parsed.depends_on:
                if dependency.kind == "metric" and dependency not in seen:
                    seen.add(dependency)
                    pending.append(dependency)
        return shapes

    def additivity(self, member: MemberId) -> tuple[str, str]:
        """The metric's additivity class and whether it was `declared` or `derived`."""
        compiled = self.compiled.require(member)
        typed = compiled.typed
        if typed.type.aggregate_class in ("holistic", "sketch"):
            return "holistic", "derived"
        declared = self.payloads.get(compiled.parsed.pointer, {}).get("additivity")
        if declared is not None:
            return str(declared), "declared"
        node = typed.tree.node if typed.tree is not None else None
        if (
            isinstance(node, sql.Call)
            and node.name in _DERIVED_ADDITIVE
            and not node.distinct
            and typed.type.aggregate_class == "distributive"
        ):
            return "additive", "derived"
        raise SemanticError("MISSING_ADDITIVITY_DECLARATION", {"metric": self.label(member)})

    # C1

    def path(self, home: str, row: _Row, metric: MemberId) -> Path:
        target = row.member.dataset or home
        paths = self.graph.paths(home, target)
        if row.role is not None:
            paths = [p for p in paths if row.role in p.relationships]
        if not paths:
            raise SemanticError(
                "ILLEGAL_GRAIN_TRANSITION",
                {"metric": self.label(metric), "grain": [self.label(row.member)]},
            )
        safe = [
            p
            for p in paths
            if all(s.safe for s in p.steps) and not any(self.graph.bridge(d) for d in p.datasets)
        ]
        if len(safe) > 1:
            names = sorted(
                {self.graph.relationship_names[r] for p in safe for r in p.relationships}
            )
            shared = set.intersection(*({*p.relationships} for p in safe))
            roles = sorted(
                {self.graph.relationship_names[r] for p in safe for r in p.relationships}
                - {self.graph.relationship_names[r] for r in shared}
            )
            raise SemanticError(
                "AMBIGUOUS_RELATIONSHIP_PATH",
                {
                    "from": self.dataset(home),
                    "to": self.dataset(target),
                    "roles": roles if len(roles) >= 2 else names,
                },
            )
        if not safe:
            if any(self.graph.bridge(d) for p in paths for d in p.datasets):
                return self._pending("bridge_allocation")
            return self._pending("fanout")
        if any(step.temporal for step in safe[0].steps):
            return self._pending("as_of_join")
        return safe[0]

    def _pending(self, shape: str) -> Path:
        _refuse_pending({shape})
        raise AssertionError(shape)  # every shape but `aggregate` is pending

    def analyze(self) -> CorrectnessReport:
        self.check_policies()
        query = self.query
        shapes = set(query.shapes)
        metrics = sorted(set(query.metrics))
        rows = query.rows
        for member in [*metrics, *(r.member for r in rows)]:
            self.answerable(member)
        for row in rows:
            typed = self.compiled.require(row.member).typed.type
            if typed.buckets_instant or (row.bucket and typed.logical.datatype == "DateTimeTz"):
                shapes.add("zoned_bucketing")
        homes: dict[MemberId, set[str]] = {}
        for metric in metrics:
            homes[metric] = self.home(metric, self.compiled.require(metric))
            if not homes[metric]:
                raise SemanticError(
                    "MISSING_EXTENSION_DECLARATION",
                    {
                        "element": "metric",
                        "name": self.label(metric),
                        "declaration": "home_dataset",
                    },
                )
            shapes |= self.declaration_shapes(metric)
        if len(set().union(*homes.values())) > 1:
            shapes.add("multi_fact")
        classes = {m: self.additivity(m) for m in metrics}
        _refuse_pending(shapes | {c for c, _ in classes.values() if c != "additive"})
        (home,) = set().union(*homes.values())
        grouped = {r.member for r in query.grouped}
        for row in rows:
            if row.slot == "sort" and row.member not in grouped:
                raise SemanticError(
                    "ILLEGAL_GRAIN_TRANSITION",
                    {"metric": self.label(metrics[0]), "grain": [self.label(row.member)]},
                )
        paths: dict[_Row, Path] = {row: self.path(home, row, metrics[0]) for row in rows}
        crossed = {home, *(d for p in paths.values() for d in p.datasets)}
        self.check_datasets(crossed)
        return self.report(home, metrics, classes, paths, crossed)

    def report(
        self,
        home: str,
        metrics: list[MemberId],
        classes: Mapping[MemberId, tuple[str, str]],
        paths: Mapping[_Row, Path],
        crossed: set[str],
    ) -> CorrectnessReport:
        natural = [self.key_label(home, key) for key in self.graph.grain(home)]
        requested = sorted(
            {(self.label(r.member), r.bucket) for r in self.query.grouped},
            key=lambda e: (e[0], e[1] or ""),
        )
        to_grain = tuple(f"{label}@{bucket}" if bucket else label for label, bucket in requested)
        grain_members = sorted({label for label, _ in requested} | set(natural))
        datasets = sorted(self.dataset(d) for d in crossed)
        obligations = [
            Obligation(
                id=f"C1.aggregate:{self.label(m)}",
                rule="C1",
                kind="aggregate",
                metric=self.label(m),
                from_grain=tuple(natural),
                to_grain=to_grain,
                members=tuple(sorted({self.label(m), *grain_members})),
                datasets=tuple(datasets),
            )
            for m in metrics
        ]
        facts = {
            "mode": self.query.mode,
            "grain": [{"member": label, "bucket": bucket} for label, bucket in requested],
            "metrics": [
                {
                    "metric": self.label(m),
                    "home": self.dataset(home),
                    "grain": natural,
                    "additivity": classes[m][0],
                    "additivity_source": classes[m][1],
                }
                for m in sorted(metrics, key=self.label)
            ],
            "paths": sorted(
                (
                    {
                        "member": self.label(row.member),
                        "slot": row.slot,
                        "from": self.dataset(home),
                        "relationships": [
                            self.graph.relationship_names[r] for r in path.relationships
                        ],
                    }
                    for row, path in paths.items()
                ),
                key=lambda e: (e["member"], e["slot"], e["relationships"]),
            ),
            "datasets": datasets,
            "row_policies": [],
        }
        unique_paths = []
        for entry in facts["paths"]:
            if entry not in unique_paths:
                unique_paths.append(entry)
        facts["paths"] = unique_paths
        return make_report(
            self.compiled.model_fingerprint,
            self.bound.fingerprint,
            facts,
            obligations,
            RULES,
        )


def analyze(
    bound: BoundQuery,
    compiled: CompiledModel,
    policies: PolicyModel,
    context: Mapping[str, Any],
) -> CorrectnessReport:
    """The correctness report of `bound` for the caller `context` describes (`security-context`
    v1, read by `read_caller`), or a `SemanticError`. Roles never widen anything: only the
    caller's groups decide what is visible (#238)."""
    caller = read_caller(context)
    return _Analysis(bound, compiled, policies, caller.groups).analyze()
