"""The correctness engine (P2-11, #310; `semantic-engine.md` §4 and §5).

`analyze(bound, compiled, policies, context)` decides, before any SQL exists, whether a bound
query has exactly one correct answer. It is pure and deterministic: it reads no data and writes
no SQL. It returns a `CorrectnessReport`, or raises a `SemanticError` with a stable code.

**Policies come first** (P2-08), and are never bypassed:
- a member the caller may not see is refused as `MEMBER_NOT_FOUND` by `bind_query(hidden=...)`,
  with the caller's own text, before any other check, so it cannot be told from a member that
  does not exist. A query that reaches `analyze` still naming one was bound for another
  caller: that is an internal error (`UnfilteredQuery`), never a user-facing answer, since any
  answer would say more than a missing member does;
- a masked member used to group, filter or sort is refused as `MEMBER_MASKED`. A metric that
  reads a masked member is already hidden (P2-08), so a distinct count over one is not found;
- a dataset that a policy not yet executable makes unusable, or that a row policy reaches, is
  refused as `UNSUPPORTED_OPERATION` (`row_policy` until P2-21), never read without it.

**Fail closed.** Every query shape needs the rules in `SHAPES`. A shape whose rule is not
implemented yet is refused as `UNSUPPORTED_OPERATION`, its `operation` naming the shape; it
never reaches the planner. A query is answered only when every metric's class is answered and
every member it groups, filters or sorts by is reached from each metric's home through
relationships that never multiply its rows.

**C1, the grain algebra.** A metric is aggregated from the natural grain of its home dataset
(its declared `grain`, or its primary key) to the requested grain. Each requested member must be
reached from the home dataset along exactly one path of many-to-one (or one-to-one) steps:
- no path at all: `ILLEGAL_GRAIN_TRANSITION`;
- more than one such path, with no role to choose: `AMBIGUOUS_RELATIONSHIP_PATH` (C4's
  placeholder until P2-15);
- only paths with a one-to-many step: `fanout`, which has more than one meaning; through a
  bridge: `bridge_allocation` (C7, P2-18); an as-of relationship: `as_of_join` (C6, P2-20).

**C3, isolation** (P2-16, #331). Metrics of more than one home dataset are answered when each
metric, on its own, is: every grouped, filtered or sorted member is reached from that metric's
own home along exactly one many-to-one path. Each home is aggregated apart, to the requested
grain, and the results are aligned on that grain only, so a fact's rows never meet another
fact's rows; each metric gives the obligation `C3.isolate:<metric>`. A member one home cannot
reach that way refuses the whole query, before any read: no fact is answered without a filter
the question asked for. A metric that itself reads two datasets is still `multi_fact`.

**C5, conformed dimensions** (P2-17, #333). A fact that cannot reach a requested member is
grouped and filtered by its own field of the same declared `conformed_dimension`, of the same
type: in its own dataset or one it reaches many-to-one, exactly one. Conformance is declared,
never inferred from names: a member with no declaration stays `ILLEGAL_GRAIN_TRANSITION`, and a
declared one the fact has no single field for, or one of another type, is `multi_fact`.

**Metrics over metrics** (S7 `over_metrics`). Each metric it names is aggregated in its own
fact, the facts are aligned, and only then is it computed, row by row, from the aligned values:
`+` and `-` exactly, a declared ratio by C2's one exact division. It is asked only at its declared
alignment grain (`METRIC_ALIGNMENT_AMBIGUOUS` otherwise) and gives `C5.align:<metric>`.

C1 validates the grain; it never authorizes an aggregation by itself. The metric's additivity
must be `additive`, declared or derived (a plain `SUM` or `COUNT` with no `DISTINCT`); any other
class is refused until its rule lands, and a metric with neither is refused as
`MISSING_ADDITIVITY_DECLARATION`. Each accepted metric gives one obligation,
`C1.aggregate:<metric>`, which the planner's aggregation step discharges (P2-12).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
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


class UnfilteredQuery(ValueError):
    """A bound query naming a member its caller may not see: it was not bound with this
    caller's `hidden`. An internal error, never shown to the caller."""


RULES: tuple[tuple[str, int], ...] = (
    ("C1", 1),
    ("C2", 1),
    ("C3", 1),
    ("C5", 1),
    ("C6", 1),
)  # implemented, in the order they run
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
    "holistic": Shape("holistic aggregate", "a later issue"),
    "approximate": Shape("approximate aggregate", "a later issue"),
    "semi_additive": Shape("other non-additive axes", "a later issue"),
    "period_end_ties": Shape("a declared tie-breaking rule", "a later issue"),
    "period_end_filter": Shape("a filter on a period-end axis", "P2-20"),
    "mixed_period_end": Shape("period end beside other metrics", "a later issue"),
    "non_additive": Shape("C2 non-additive", "a later issue"),
    "multi_fact": Shape("alignment beyond declared conformed dimensions", "a later issue"),
    "fanout": Shape("one-to-many grouping", "a later issue"),
    "calendar_bucket": Shape("model calendars", "a later issue"),
    "ordered": Shape("S9 windows", "P2-20"),
    "as_of_join": Shape("C6 as-of joins", "P2-20"),
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
    "calculated_field": Shape("S10", "#237"),
    "history": Shape("versioned history", "a later issue"),
    "metric_extension": Shape("unknown declaration", "a later issue"),
}
# A field or dataset declaration that changes the values read needs its own rule.
_FIELD_SHAPES: Mapping[str, str] = {
    "calculated": "calculated_field",
    "calendar": "calendar_bucket",
}  # `business_time` over an instant is `zoned_bucketing`, decided from its type above
_DATASET_SHAPES: Mapping[str, str] = {"history": "history"}
# A metric declaration that changes how it is computed needs its own rule. Any declaration not
# listed here or in `_PLAIN_DECLARATIONS` refuses the query (`metric_extension`).
_DECLARATION_SHAPES: Mapping[str, str] = {
    "window": "ordered",
    "allocation": "bridge_allocation",
    "context": "metric_context",
    "level_of_detail": "level_of_detail",
    "reaggregable_state": "reaggregable_state",
}
_PLAIN_DECLARATIONS = frozenset(
    {
        "components",  # with `recomputable`: checked as a ratio below (C2)
        "non_additive_axes",  # with `semi_additive`: checked as a period below (C2)
        "period_end",
        "version",
        "element",
        "additivity",
        "aggregate_class",
        "approximate",
        "currency",
        "unit",
        "home_dataset",
        "over_metrics",  # S7: checked below, computed after alignment (C5)
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
    unknown = shapes - set(SHAPES)
    if unknown:  # a shape no table row names is never answered
        raise SemanticError("UNSUPPORTED_OPERATION", {"operation": "metric_extension"})
    for shape, needs in SHAPES.items():
        if shape in shapes and needs.rule not in IMPLEMENTED:
            raise SemanticError("UNSUPPORTED_OPERATION", {"operation": shape})


def _refs(node: object) -> list[sql.Ref]:
    """Every reference in an expression tree."""
    if isinstance(node, sql.Ref):
        return [node]
    found: list[sql.Ref] = []
    if hasattr(node, "__dataclass_fields__"):
        for name in node.__dataclass_fields__:
            value = getattr(node, name)
            for item in value if isinstance(value, tuple) else (value,):
                if isinstance(item, tuple):
                    for inner in item:
                        found += _refs(inner)
                else:
                    found += _refs(item)
    return found


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
        sorted_rows: list[MemberId] = []
        for member, _, _ in canonical.get("order", ()):
            found = _member(member)
            if found.kind == "metric":
                self.metrics.append(found)
            else:
                sorted_rows.append(found)
        if self.mode != "aggregate":
            self.rows += [_Row(m, None, None, "sort") for m in sorted_rows]
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
        # A sort follows its group's path; a member grouped under two roles has no one path.
        roles: dict[MemberId, set[str | None]] = {}
        for row in self.grouped:
            roles.setdefault(row.member, set()).add(row.role)
        self.rows += [
            _Row(m, next(iter(roles[m])) if len(roles.get(m, ())) == 1 else None, None, "sort")
            for m in sorted_rows
        ]
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
        self.periods: dict[MemberId, tuple[MemberId, str]] = {}

    # names, as the model writes them

    def label(self, member: MemberId) -> str:
        return self.compiled.require(member).parsed.label

    def dataset(self, name: str) -> str:
        return self.graph.names.get(name, name)

    def key_label(self, dataset: str, key: str) -> str:
        member = MemberId("field", dataset, key)
        if member in self.compiled.members:
            return self.label(member)
        return f"{self.dataset(dataset)}.{self.graph.written.get((dataset, key), key)}"

    # policies, first

    def check_policies(self) -> None:
        for member in self.bound.members:
            if self.policies.protection(member, self.groups) == "hidden":
                raise UnfilteredQuery("the query was not bound with this caller's visibility")
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

    def field_shapes(self, fields: Iterable[MemberId]) -> set[str]:
        """The shapes the declarations of these fields and of their datasets need."""
        shapes: set[str] = set()
        for member in fields:
            payload = self.payloads.get(self.compiled.require(member).parsed.pointer, {})
            shapes |= {_FIELD_SHAPES[k] for k in payload if k in _FIELD_SHAPES}
            if member.dataset is not None:
                own = self.graph.payloads.get(member.dataset, {})
                shapes |= {_DATASET_SHAPES[k] for k in own if k in _DATASET_SHAPES}
        return shapes

    def additivity(self, member: MemberId) -> tuple[str, str]:
        """The metric's additivity class and whether it was `declared` or `derived`."""
        compiled = self.compiled.require(member)
        typed = compiled.typed
        if typed.type.aggregate_class == "sketch":
            return "approximate", "derived"
        if typed.type.aggregate_class == "holistic":
            node = typed.tree.node if typed.tree is not None else None
            distinct = (
                isinstance(node, sql.Call)
                and node.name == "COUNT"
                and node.distinct
                and len(node.arguments) == 1
                and node.filter is None
            )
            payload = self.payloads.get(compiled.parsed.pointer, {})
            plain = (
                payload.get("additivity") == "non_additive"
                and "non_additive_axes" not in payload
                and "period_end" not in payload
            )
            return ("distinct_count" if distinct and plain else "holistic"), "derived"
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

    # C2, semi-additive metrics

    def period(self, metric: MemberId, home: str) -> tuple[MemberId, str]:
        """The metric's one non-additive time axis and its behavior, when the query can be
        answered: `period_end` keeps each entity's latest snapshot per period; `forbidden`
        allows grouping by the axis's own values only. Anything else is refused."""
        payload = self.payloads.get(self.compiled.require(metric).parsed.pointer, {})
        declared = payload.get("non_additive_axes", ())
        axes = (
            [a for a in declared if isinstance(a, Mapping)] if isinstance(declared, tuple) else []
        )
        dimensions = [a for a in axes if "dimension" in a]
        entities_ok = all(a.get("behavior") == "additive" for a in axes if "entity" in a)
        behavior = str(dimensions[0].get("behavior")) if len(dimensions) == 1 else ""
        if not entities_ok or behavior not in ("period_end", "forbidden"):
            _refuse_pending({"semi_additive"})
        axis = MemberId("field", home, sql.normalize(str(dimensions[0]["dimension"])))
        if axis not in self.compiled.members:
            _refuse_pending({"semi_additive"})
        groups = [r for r in self.query.grouped if r.member == axis]
        if not groups or (behavior == "forbidden" and groups[0].bucket is not None):
            raise SemanticError(
                "ILLEGAL_TEMPORAL_REAGGREGATION",
                {"metric": self.label(metric), "axis": self.label(axis)},
            )
        if behavior == "period_end":
            # Two snapshots of one entity at one instant must be impossible: the key is exactly
            # the declared entity plus the axis, and the axis is never NULL.
            entity = self.entity_fields(axes, home)
            key = set(self.graph.keys.get(home, ()))
            nullable = self.compiled.require(axis).typed.type.nullable
            if entity is None or key != {*entity, axis.name} or axis.name in entity or nullable:
                _refuse_pending({"period_end_ties"})
            if any(r.member == axis for r in self.query.rows if r.slot == "filter"):
                _refuse_pending({"period_end_filter"})
        return axis, behavior

    def entity_fields(self, axes: list[Mapping[str, Any]], home: str) -> set[str] | None:
        """The fields of the entity the metric adds across: from the dataset's `entity_keys`,
        or the columns of a many-to-one relationship to the dataset of that entity."""
        names = [str(a["entity"]) for a in axes if "entity" in a]
        if len(names) != 1:
            return None
        payload = self.graph.payloads.get(home, {})
        for declared in payload.get("entity_keys", ()):
            if isinstance(declared, Mapping) and declared.get("entity") == names[0]:
                return {sql.normalize(str(f)) for f in declared.get("fields", ())}
        relationships = self.compiled.ossie.document.get("relationships", ())
        for relationship in relationships if isinstance(relationships, tuple) else ():
            target = sql.normalize(str(relationship["to"]))
            if (
                sql.normalize(str(relationship["from"])) == home
                and self.graph.payloads.get(target, {}).get("entity") == names[0]
            ):
                return {sql.normalize(str(c)) for c in relationship["from_columns"]}
        return None

    # C2, ratios

    def components(self, metric: MemberId, home: str) -> None:
        """A ratio is answered only when each component is a plain additive aggregate over
        the fields of its home dataset: never a ratio of ratios, never across facts."""
        typed = self.compiled.require(metric).typed
        trees = dict(typed.payload)
        for part in ("components.numerator", "components.denominator"):
            tree = trees.get(part)
            node = tree.node if tree is not None else None
            if not (
                isinstance(node, sql.Call)
                and node.name in _DERIVED_ADDITIVE
                and not node.distinct
                and tree is not None
                and tree.type.aggregate_class == "distributive"
            ):
                raise SemanticError("UNSUPPORTED_OPERATION", {"operation": "recomputable"})
            for ref in _refs(node):
                owner = ref.parts[0] if len(ref.parts) == 2 else metric.dataset
                if owner != home or MemberId("field", owner, ref.parts[-1]) not in (
                    self.compiled.members
                ):
                    raise SemanticError("UNSUPPORTED_OPERATION", {"operation": "multi_fact"})

    # C1

    def path(self, home: str, row: _Row, metric: MemberId, alone: bool = True) -> Path:
        """C4: the one path from the fact to the member. A role names one relationship, and
        exactly one many-to-one path may cross it; with no role, exactly one many-to-one path
        may exist. A member of the fact's own dataset, with no role, is read from the fact. With
        other facts (`alone` false), a member this one cannot reach at all is `multi_fact` (C5)."""
        target = row.member.dataset or home
        if row.role is None and target == home:
            return Path(())
        everything = self.graph.paths(home, target)
        if not everything and not alone:  # another fact's member: a conformed dimension (C5)
            return self._pending("multi_fact")
        if not everything:
            raise SemanticError(
                "ILLEGAL_GRAIN_TRANSITION",
                {"metric": self.label(metric), "grain": [self.label(row.member)]},
            )
        safe_everything = [p for p in everything if self.safe(p)]
        paths = everything
        if row.role is not None:
            paths = [p for p in everything if row.role in p.relationships]
            if not paths and target == home and not any(p.steps for p in safe_everything):
                self.role_invalid(row)  # the role crosses no relationship at all
            if not paths and safe_everything:
                self.ambiguous(home, target, safe_everything, extra=row.role)
            if not paths:
                paths = everything  # no many-to-one path at all: fan-out or a bridge, below
        safe = [p for p in paths if self.safe(p)]
        if len(safe) > 1:
            self.ambiguous(home, target, safe)
        if not safe:
            if any(self.graph.bridge(d) for p in paths for d in p.datasets):
                return self._pending("bridge_allocation")
            return self._pending("fanout")
        if any(step.temporal for step in safe[0].steps):
            return self._pending("as_of_join")
        return safe[0]

    def safe(self, path: Path) -> bool:
        return all(s.safe for s in path.steps) and not any(
            self.graph.bridge(d) for d in path.datasets
        )

    def ambiguous(
        self, home: str, target: str, paths: list[Path], extra: str | None = None
    ) -> None:
        """`AMBIGUOUS_RELATIONSHIP_PATH`, naming the relationships that tell the paths apart."""
        names = {self.graph.relationship_names[r] for p in paths for r in p.relationships}
        shared: set[str] = (
            set.intersection(*({*p.relationships} for p in paths)) if paths else set()
        )
        roles = names - {self.graph.relationship_names[r] for r in shared}
        if extra is not None:
            roles.add(self.graph.relationship_names.get(extra, extra))
        if len(roles) < 2:
            roles = names | roles
        raise SemanticError(
            "AMBIGUOUS_RELATIONSHIP_PATH",
            {"from": self.dataset(home), "to": self.dataset(target), "roles": sorted(roles)},
        )

    def role_invalid(self, row: _Row) -> None:
        """A role on a member the fact reads itself, crossing no relationship (P2-15)."""
        pointer = next(
            (p for p, m, role in self.bound.roles if m == row.member and role == row.role),
            "",
        )
        raise SemanticError(
            "QUERY_INVALID",
            {"problems": [{"pointer": pointer, "rule": "role"}], "problems_omitted": "0"},
        )

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
        # C6: an instant is cut into calendar units in its declared zone, never the host's.
        self.zones = {
            (r.member, r.bucket): self.zone_of(r.member)
            for r in rows
            if r.bucket
            and self.compiled.require(r.member).typed.type.logical.datatype == "DateTimeTz"
        }
        read = {r.member for r in rows} | {
            f for m in metrics for f in self.compiled.require(m).parsed.lineage
        }
        shapes |= self.field_shapes(sorted(read))
        self.derived = {m: self.over(m) for m in metrics if self.is_derived(m)}
        for metric in self.derived:
            shapes |= self.declaration_shapes(metric)
        metrics = sorted(
            {m for m in metrics if m not in self.derived}
            | {s for sources in self.derived.values() for s in sources}
        )
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
        if any(len(found) > 1 for found in homes.values()):
            shapes.add("multi_fact")  # one metric reading two datasets (C5)
        classes = {m: self.additivity(m) for m in metrics}
        answered = {"additive", "recomputable", "distinct_count", "semi_additive"}
        _refuse_pending(shapes | {c for c, _ in classes.values() if c not in answered})
        home_of = {m: next(iter(found)) for m, found in homes.items()}
        derived_classes = {m: self.additivity(m) for m in self.derived}
        for metric in self.derived:
            self.aligned(metric, derived_classes[metric][0])
        periods: dict[MemberId, tuple[MemberId, str]] = {}
        for metric, (kind, _) in classes.items():
            if kind == "recomputable":
                self.components(metric, home_of[metric])
            if kind == "semi_additive":
                periods[metric] = self.period(metric, home_of[metric])
        ends = {axis for axis, behavior in periods.values() if behavior == "period_end"}
        if ends and (
            len(ends) > 1 or any(periods.get(m, (None, ""))[1] != "period_end" for m in metrics)
        ):
            _refuse_pending({"mixed_period_end"})
        self.periods = periods
        grouped = {r.member for r in query.grouped}
        for row in rows:
            if row.slot == "sort" and row.member not in grouped:
                raise SemanticError(
                    "ILLEGAL_GRAIN_TRANSITION",
                    {"metric": self.label(metrics[0]), "grain": [self.label(row.member)]},
                )
        # C3: each home on its own, so every member is reached from every fact, or none is read.
        paths: dict[tuple[str, _Row], Path] = {}
        crossed: dict[str, set[str]] = {}
        self.substitutes: dict[tuple[str, _Row], MemberId] = {}
        alone = len(set(home_of.values())) == 1
        for home in sorted(set(home_of.values())):
            first = next(m for m in metrics if home_of[m] == home)
            for row in rows:
                found = self.conformed(home, row, first, alone)
                if found != row:
                    self.substitutes[home, row] = found.member
                paths[home, row] = self.path(home, found, first, alone=alone)
            crossed[home] = {
                home,
                *(d for (h, _), p in paths.items() if h == home for d in p.datasets),
            }
        self.check_datasets(set().union(*crossed.values()))
        classes = {**classes, **derived_classes}
        return self.report(home_of, metrics, classes, paths, crossed)

    # C6, zones

    def zone_of(self, member: MemberId) -> str:
        """The zone a query bucket of an instant applies: the field's own `time_zone`, then the
        value's, then the model's `default_time_zone`, as the compiler resolves one (P2-04)."""
        compiled = self.compiled.require(member)
        own = self.payloads.get(compiled.parsed.pointer, {}).get("time_zone")
        declared = own.get("name") if isinstance(own, Mapping) else None
        found = (
            declared
            or compiled.typed.type.logical.zone
            or self.payloads.get("", {}).get("default_time_zone")
        )
        if not found:  # nothing declares one: never the server's zone
            raise SemanticError(
                "MISSING_EXTENSION_DECLARATION",
                {"element": "field", "name": self.label(member), "declaration": "time_zone"},
            )
        return str(found)

    # C5, conformed dimensions

    def conformed_name(self, member: MemberId) -> str | None:
        payload = self.payloads.get(self.compiled.require(member).parsed.pointer, {})
        name = payload.get("conformed_dimension")
        return sql.normalize(str(name)) if name is not None else None

    def conformed(self, home: str, row: _Row, metric: MemberId, alone: bool) -> _Row:
        """The row as the fact `home` reads it: itself when the fact reaches it, or, with other
        facts, the fact's one field of the same declared conformed dimension and type."""
        target = row.member.dataset or home
        if alone or target == home or self.graph.paths(home, target):
            return row
        name = self.conformed_name(row.member)
        if name is None:  # never inferred: no declared conformance, no alignment
            raise SemanticError(
                "ILLEGAL_GRAIN_TRANSITION",
                {"metric": self.label(metric), "grain": [self.label(row.member)]},
            )
        reached = {home} | {
            d
            for d in self.graph.names
            if d != home and any(self.safe(p) for p in self.graph.paths(home, d))
        }
        # A field the caller may not see is not there at all, so it is never revealed (P2-08).
        candidates = [
            m
            for m in self.compiled.members
            if m.kind == "field"
            and m.dataset in reached
            and m != row.member
            and self.conformed_name(m) == name
            and self.policies.protection(m, self.groups) != "hidden"
        ]
        wanted = self.compiled.require(row.member).typed.type.logical
        if (
            len(candidates) != 1
            or row.role is not None
            or self.compiled.require(candidates[0]).typed.type.logical != wanted
        ):
            _refuse_pending({"multi_fact"})
        if self.policies.protection(candidates[0], self.groups) == "masked":
            raise SemanticError(
                "MEMBER_MASKED", {"member": self.label(candidates[0]), "operation": row.slot}
            )
        return _Row(candidates[0], None, row.bucket, row.slot)

    # S7, metrics over metrics

    def is_derived(self, metric: MemberId) -> bool:
        payload = self.payloads.get(self.compiled.require(metric).parsed.pointer, {})
        return "over_metrics" in payload

    def over(self, metric: MemberId) -> tuple[MemberId, ...]:
        """The metrics a metric over metrics names; each a plain metric of its own fact."""
        compiled = self.compiled.require(metric)
        over = self.payloads.get(compiled.parsed.pointer, {})["over_metrics"]
        sources = tuple(m for m in compiled.parsed.depends_on if m.kind == "metric")
        if (
            not sources
            or len(sources) != len(compiled.parsed.depends_on)
            or (isinstance(over, Mapping) and "ignore_filters" in over)
            or any(self.is_derived(m) for m in sources)
        ):
            _refuse_pending({"over_metrics"})
        return sources

    def aligned(self, metric: MemberId, kind: str) -> None:
        """Computable after alignment, from the metrics it names, at its declared grain only."""
        compiled = self.compiled.require(metric)
        trees = dict(compiled.typed.payload)
        sources = set(self.derived[metric])
        if kind == "recomputable":
            parts = ("components.numerator", "components.denominator")
            ends = [self.source_of(trees[part].node) for part in parts]
            expression = trees["over_metrics"].node
            if isinstance(expression, sql.Cast) and not expression.safe:
                expression = expression.operand
            # Its expression must be the division of its components: nothing else is computed.
            if (
                any(end not in sources for end in ends)
                or not isinstance(expression, sql.Binary)
                or expression.op != "/"
                or [self.source_of(expression.left), self.source_of(expression.right)] != ends
            ):
                _refuse_pending({"over_metrics"})
        elif kind != "additive" or not self.plain_sum(trees["over_metrics"].node, sources):
            _refuse_pending({"over_metrics"})
        over = self.payloads.get(compiled.parsed.pointer, {})["over_metrics"]
        assert isinstance(over, Mapping)  # `ossie-extensions` v1, checked on read
        align = over["align"]
        assert isinstance(align, Mapping)
        declared = sorted(sql.normalize(str(g)) for g in align["grain"])
        grouped = list(self.query.grouped)
        names = [self.conformed_name(r.member) for r in grouped]
        plain = all(r.bucket is None and r.role is None for r in grouped)
        if not plain or None in names or sorted(str(n) for n in names) != declared:
            raise SemanticError(
                "METRIC_ALIGNMENT_AMBIGUOUS", {"metric": self.label(metric), "missing": "grain"}
            )

    def source_of(self, node: object) -> MemberId | None:
        """The metric a bare reference names, or None."""
        if not isinstance(node, sql.Ref):
            return None
        written = ".".join(node.parts)
        return next(
            (
                m
                for m in self.compiled.members
                if m.kind == "metric" and sql.normalize(self.label(m)) == written
            ),
            None,
        )

    def plain_sum(self, node: object, sources: set[MemberId]) -> bool:
        """Only `+` and `-` of the metrics it names: exact, with no rounding to declare."""
        if isinstance(node, sql.Binary):
            return (
                node.op in ("+", "-")
                and self.plain_sum(node.left, sources)
                and self.plain_sum(node.right, sources)
            )
        return self.source_of(node) in sources

    def report(
        self,
        home_of: Mapping[MemberId, str],
        metrics: list[MemberId],
        classes: Mapping[MemberId, tuple[str, str]],
        paths: Mapping[tuple[str, _Row], Path],
        crossed: Mapping[str, set[str]],
    ) -> CorrectnessReport:
        written = self.graph.relationship_names
        requested = sorted(
            {
                (self.label(r.member), written.get(r.role, r.role) if r.role else None, r.bucket)
                for r in self.query.grouped
            },
            key=lambda e: (e[0], e[1] or "", e[2] or ""),
        )
        to_grain = tuple(
            label + (f"[{role}]" if role else "") + (f"@{bucket}" if bucket else "")
            for label, role, bucket in requested
        )
        natural = {h: [self.key_label(h, key) for key in self.graph.grain(h)] for h in crossed}
        zones: dict[tuple[str, str | None], str] = {
            (self.label(m), b): z for (m, b), z in self.zones.items()
        }
        labels = {label for label, _, _ in requested}

        def obligation(metric: MemberId, rule: str, kind: str, prefix: str) -> Obligation:
            home = home_of[metric]
            return Obligation(
                id=f"{prefix}:{self.label(metric)}",
                rule=rule,
                kind=kind,
                metric=self.label(metric),
                from_grain=tuple(natural[home]),
                to_grain=to_grain,
                members=tuple(sorted({self.label(metric), *labels, *natural[home]})),
                datasets=tuple(sorted(self.dataset(d) for d in crossed[home])),
            )

        obligations = [obligation(m, "C1", "aggregate", "C1.aggregate") for m in metrics]
        obligations += [
            obligation(m, "C2", "recompute", "C2.recompute")
            for m in metrics
            if classes[m][0] == "recomputable"
        ]
        extra = {"distinct_count": ("distinct", "C2.distinct")}
        for m in metrics:
            kind = classes[m][0]
            if kind in extra or self.periods.get(m, (None, ""))[1] == "period_end":
                name, prefix = extra.get(kind, ("period_end", "C2.period_end"))
                obligations.append(obligation(m, "C2", name, prefix))
        if len(crossed) > 1:  # more than one fact: each aggregated apart (C3)
            obligations += [obligation(m, "C3", "isolate", "C3.isolate") for m in metrics]
        for derived in sorted(self.derived, key=self.label):  # computed after alignment (C5)
            sources = self.derived[derived]
            reads = sorted({self.dataset(d) for m in sources for d in crossed[home_of[m]]})
            for rule, kind, prefix in (
                ("C5", "align", "C5.align"),
                ("C2", "recompute", "C2.recompute"),
            ):
                if kind == "recompute" and classes[derived][0] != "recomputable":
                    continue
                obligations.append(
                    Obligation(
                        id=f"{prefix}:{self.label(derived)}",
                        rule=rule,
                        kind=kind,
                        metric=self.label(derived),
                        from_grain=to_grain,
                        to_grain=to_grain,
                        members=tuple(
                            sorted({self.label(derived), *labels, *map(self.label, sources)})
                        ),
                        datasets=tuple(reads),
                    )
                )
        facts = {
            "mode": self.query.mode,
            "grain": [
                {"member": label, "role": role, "bucket": bucket}
                | ({"zone": zones[label, bucket]} if (label, bucket) in zones else {})
                for label, role, bucket in requested
            ],
            "metrics": [
                {
                    "metric": self.label(m),
                    "home": self.dataset(home_of[m]),
                    "grain": natural[home_of[m]],
                    "additivity": classes[m][0],
                    "additivity_source": classes[m][1],
                    "period": (
                        {"axis": self.label(self.periods[m][0]), "behavior": self.periods[m][1]}
                        if m in self.periods
                        else None
                    ),
                }
                for m in sorted(metrics, key=self.label)
            ],
            "paths": sorted(
                (
                    {
                        "member": self.label(row.member),
                        "slot": row.slot,
                        "role": self.graph.relationship_names.get(row.role, row.role)
                        if row.role
                        else None,
                        "from": self.dataset(home),
                        "relationships": [
                            self.graph.relationship_names[r] for r in path.relationships
                        ],
                    }
                    | (
                        {"as": self.label(self.substitutes[home, row])}
                        if (home, row) in self.substitutes
                        else {}
                    )
                    for (home, row), path in paths.items()
                ),
                key=lambda e: (
                    e["from"],
                    e["member"],
                    e["slot"],
                    e["role"] or "",
                    e["relationships"],
                ),
            ),
            "datasets": sorted({self.dataset(d) for found in crossed.values() for d in found}),
            "row_policies": [],
        }
        if self.derived:
            facts["derived"] = [
                {
                    "metric": self.label(m),
                    "over": [self.label(s) for s in self.derived[m]],
                    "additivity": classes[m][0],
                }
                for m in sorted(self.derived, key=self.label)
            ]
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
            bound=self.bound,
            compiled=self.compiled,
            caller_groups=self.groups,
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
