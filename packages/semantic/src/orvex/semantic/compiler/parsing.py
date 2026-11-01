"""Parse every expression of a model, resolve its references, detect cycles and record lineage
(P2-04b, #288; #280 is the source of truth).

`parse_model(model, vendor_names)` returns a `ParsedModel` or raises `SemanticError`.

Each field and metric has one expression, the one P2-03 selected:
- an **Ossie expression** (`sql` or `OSSIE_SQL_2026`): parsed by `ossie.sql`; its references
  follow the specification's scoping, which P2-03 already checked;
- a **product expression**, under the product's dialect token on a declared derived metric:
  the subset plus references to fields (`dataset.field`) and metrics (`metric`,
  `dataset.metric`), as `ossie-extensions/v1` defines them;
- an **opaque expression**, under the dataset's engine token: never interpreted. Its lineage is
  every field of its dataset (every field of the model for a model metric). When the parser can
  read it, P2-03's rule still applies: it must not name a metric.

The payloads' product expressions are parsed and resolved too: `calculated.expression`,
`over_metrics.expression`, `components`, `window.of`, calculation items, segment conditions and
funnel steps.

**What is not compiled** (P2-03's advisories, ADR-0030 §2): a member with no selected
expression, or whose expression uses SQL the product does not implement, is excluded with a
fixed operation name, and so is every member that depends on it. Using an excluded member is
refused with `UNSUPPORTED_OPERATION` (`ParsedModel.require`). Nothing is skipped silently.

**Errors:**
- a product expression that cannot be parsed → `OSSIE_INVALID` `expression`;
- a name that resolves to nothing, or an opaque metric text that names a metric →
  `OSSIE_INVALID` `reference`, with pointer and line as P2-03 reports them;
- a cycle through metrics or calculated fields → `METRIC_CYCLE`.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from orvex.semantic.errors import SemanticError
from orvex.semantic.ossie import sql
from orvex.semantic.ossie.model import Advisory, OssieModel

PORTABLE = "OSSIE_SQL_2026"
MAX_PROBLEMS = 50
EXPRESSION_DIALECT = "expression_dialect"

Language = Literal["ossie", "product", "opaque"]
Json = Mapping[str, object]


@dataclass(frozen=True)
class MemberId:
    """A field or a metric. `dataset` is None for a model metric; names are normalized."""

    kind: Literal["field", "metric"]
    dataset: str | None
    name: str

    def __lt__(self, other: MemberId) -> bool:
        # A deterministic order: model metrics (no dataset) first.
        return (self.kind, self.dataset or "", self.name) < (
            other.kind,
            other.dataset or "",
            other.name,
        )


@dataclass(frozen=True)
class ParsedMember:
    id: MemberId
    label: str  # as written: `dataset.name`, or `name` for a model metric
    pointer: str
    language: Language
    tree: sql.Node | None  # None for an opaque expression
    depends_on: tuple[MemberId, ...]  # fields and metrics it reads directly, sorted
    lineage: tuple[MemberId, ...]  # the fields it finally reads, sorted


@dataclass(frozen=True)
class ParsedModel:
    members: Mapping[MemberId, ParsedMember]
    excluded: Mapping[MemberId, str]  # member -> the operation that refuses it
    advisories: tuple[Advisory, ...]  # P2-03's, carried unchanged (to_columns_cover_no_key, …)

    def require(self, member: MemberId) -> ParsedMember:
        """The parsed member, or `UNSUPPORTED_OPERATION` when it was not compiled."""
        operation = self.excluded.get(member)
        if operation is not None:
            raise SemanticError("UNSUPPORTED_OPERATION", {"operation": operation})
        return self.members[member]


# --- the model's names --------------------------------------------------------------------------


def _list(node: object, key: str) -> list[Json]:
    value = node.get(key) if isinstance(node, Mapping) else None
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, tuple) else []


def _name(node: Json) -> str:
    return str(node["name"])


@dataclass
class _Names:
    datasets: dict[str, str]  # normalized -> as written
    fields: dict[str, dict[str, MemberId]]  # dataset -> name -> member
    dataset_metrics: dict[str, dict[str, MemberId]]
    model_metrics: dict[str, MemberId]
    labels: dict[MemberId, str]

    @classmethod
    def of(cls, document: Json) -> _Names:
        names = cls({}, {}, {}, {}, {})
        for dataset in _list(document, "datasets"):
            key = sql.normalize(_name(dataset))
            names.datasets[key] = _name(dataset)
            names.fields[key] = {}
            names.dataset_metrics[key] = {}
            for kind, table in (("fields", names.fields), ("metrics", names.dataset_metrics)):
                for member in _list(dataset, kind):
                    member_id = MemberId(
                        "field" if kind == "fields" else "metric", key, sql.normalize(_name(member))
                    )
                    table[key][member_id.name] = member_id
                    names.labels[member_id] = f"{_name(dataset)}.{_name(member)}"
        for metric in _list(document, "metrics"):
            member_id = MemberId("metric", None, sql.normalize(_name(metric)))
            names.model_metrics[member_id.name] = member_id
            names.labels[member_id] = _name(metric)
        return names

    def all_fields(self, dataset: str | None) -> tuple[MemberId, ...]:
        keys = [dataset] if dataset is not None else sorted(self.fields)
        return tuple(sorted(f for key in keys for f in self.fields[key].values()))


# --- resolution -------------------------------------------------------------------------------


def _ossie_target(ref: sql.Ref, owner: MemberId, names: _Names) -> MemberId | None:
    """A reference in an Ossie metric expression: an unqualified field of the metric's own
    dataset, or `dataset.field` in a model metric (spec §Metric Scoping)."""
    if owner.dataset is not None:
        return names.fields[owner.dataset].get(ref.parts[0]) if len(ref.parts) == 1 else None
    if len(ref.parts) != 2 or ref.parts[0] not in names.fields:
        return None
    return names.fields[ref.parts[0]].get(ref.parts[1])


def _product_target(ref: sql.Ref, own_row: str | None, names: _Names) -> MemberId | None:
    """A reference of the product language (`ossie-extensions/v1`): `metric` names a model
    metric, `dataset.field` or `dataset.metric` a member of that dataset. In a `calculated`
    expression (`own_row`), an unqualified name is a field of the same row."""
    if len(ref.parts) == 1:
        if own_row is not None:
            return names.fields[own_row].get(ref.parts[0])
        return names.model_metrics.get(ref.parts[0])
    dataset = ref.parts[0]
    if dataset not in names.fields:
        return None
    return names.fields[dataset].get(ref.parts[1]) or names.dataset_metrics[dataset].get(
        ref.parts[1]
    )


class _Problems:
    def __init__(self, model: OssieModel) -> None:
        self.model = model
        self.found: set[tuple[int, str, str]] = set()

    def add(self, pointer: str, rule: str) -> None:
        self.found.add((self.model.line(pointer), pointer, rule))

    def raise_if_any(self) -> None:
        if not self.found:
            return
        ordered = sorted(self.found)
        shown = ordered[:MAX_PROBLEMS]
        raise SemanticError(
            "OSSIE_INVALID",
            {
                "file": self.model.path,
                "problems": [
                    {"pointer": pointer, "line": str(line), "rule": rule}
                    for line, pointer, rule in shown
                ],
                "problems_omitted": str(len(ordered) - len(shown)),
            },
        )


# --- the members ------------------------------------------------------------------------------


@dataclass
class _Draft:
    id: MemberId
    pointer: str
    language: Language
    tree: sql.Node | None
    reads: set[MemberId]
    opaque_lineage: tuple[MemberId, ...] = ()
    excluded: str | None = None


def _members(document: Json) -> Iterator[tuple[str, Json, str | None, str]]:
    """(pointer, node, dataset key or None, kind) for every field and metric."""
    for at, dataset in enumerate(_list(document, "datasets")):
        key = sql.normalize(_name(dataset))
        for kind in ("fields", "metrics"):
            for number, member in enumerate(_list(dataset, kind)):
                yield f"/datasets/{at}/{kind}/{number}", member, key, kind
    for at, metric in enumerate(_list(document, "metrics")):
        yield f"/metrics/{at}", metric, None, "metrics"


def _payloads(model: OssieModel) -> dict[str, tuple[str, Json]]:
    """Element pointer -> (data pointer, the product payload)."""
    found: dict[str, tuple[str, Json]] = {}
    for extension in model.product_extensions:
        element = extension.pointer.rsplit("/custom_extensions/", 1)[0]
        found[element] = (f"{extension.pointer}/data", extension.payload)
    return found


def _parse_product(text: object, at: str, problems: _Problems) -> sql.Node | None:
    try:
        return sql.parse(str(text))
    except sql.SqlSyntaxError, sql.SqlUnsupported:
        problems.add(at, "expression")
        return None


def parse_model(model: OssieModel, vendor_names: Sequence[str]) -> ParsedModel:
    document = model.document
    names = _Names.of(document)
    problems = _Problems(model)
    payloads = _payloads(model)
    selected = {expression.element: expression for expression in model.expressions}
    drafts: dict[MemberId, _Draft] = {}

    for pointer, node, dataset, kind in _members(document):
        member_id = MemberId(
            "field" if kind == "fields" else "metric", dataset, sql.normalize(_name(node))
        )
        choice = selected.get(pointer)
        if choice is None:
            # P2-03's no_executable_expression (and the dialect advisories behind it).
            drafts[member_id] = _Draft(
                member_id, pointer, "opaque", None, set(), (), EXPRESSION_DIALECT
            )
            continue
        if choice.dialect == PORTABLE:
            draft = _ossie_member(member_id, pointer, choice.text, names)
        elif choice.dialect in vendor_names:
            data_at, _ = payloads[pointer]
            tree = _parse_product(choice.text, data_at, problems)
            draft = _Draft(member_id, pointer, "product", tree, set())
            if tree is not None:
                draft.excluded = sql.unsupported(tree)
                draft.reads |= _resolve_product(tree, None, names, data_at, problems)
        else:
            draft = _opaque_member(member_id, pointer, choice.text, names, problems, choice.pointer)
        drafts[member_id] = draft
        if pointer in payloads:
            data_at, payload = payloads[pointer]
            draft.reads |= _payload_reads(payload, member_id, names, data_at, problems)

    data_at_model, model_payload = payloads.get("", ("", MappingProxyType({})))
    _model_payload_reads(model_payload, names, data_at_model, problems)
    problems.raise_if_any()
    _refuse_cycles(drafts, names)
    return _finish(drafts, names, model.advisories)


def _ossie_member(member_id: MemberId, pointer: str, text: str, names: _Names) -> _Draft:
    try:
        tree = sql.parse(text)
    except sql.SqlUnsupported as unsupported:
        # P2-03's unreadable_expression: valid SQL the product does not implement.
        return _Draft(member_id, pointer, "ossie", None, set(), (), unsupported.operation)
    # A syntax error was refused by P2-03 (`OSSIE_INVALID` `expression`); none reaches here.
    operation = sql.unsupported(tree)
    draft = _Draft(member_id, pointer, "ossie", tree, set(), (), operation)
    if member_id.kind == "metric":
        draft.reads = {
            target
            for ref in sql.references(tree)
            if (target := _ossie_target(ref, member_id, names)) is not None
        }
    return draft


def _opaque_member(
    member_id: MemberId,
    pointer: str,
    text: str,
    names: _Names,
    problems: _Problems,
    text_at: str,
) -> _Draft:
    """An engine-dialect expression is never interpreted; P2-03's rule that a metric never
    names a metric still applies to the text the parser can read."""
    draft = _Draft(member_id, pointer, "opaque", None, set(), names.all_fields(member_id.dataset))
    if member_id.kind != "metric":
        return draft
    try:
        tree = sql.parse(text)
    except sql.SqlSyntaxError, sql.SqlUnsupported:
        return draft
    for ref in sql.references(tree):
        if _ossie_target(ref, member_id, names) is None and _names_a_metric(ref, member_id, names):
            problems.add(text_at, "reference")
    return draft


def _names_a_metric(ref: sql.Ref, owner: MemberId, names: _Names) -> bool:
    if len(ref.parts) == 1:
        own = names.dataset_metrics.get(owner.dataset or "", {})
        return ref.parts[0] in names.model_metrics or ref.parts[0] in own
    return (
        ref.parts[0] in names.dataset_metrics
        and ref.parts[1] in names.dataset_metrics[ref.parts[0]]
    )


def _resolve_product(
    tree: sql.Node, own_row: str | None, names: _Names, at: str, problems: _Problems
) -> set[MemberId]:
    reads: set[MemberId] = set()
    for ref in sql.references(tree):
        target = _product_target(ref, own_row, names)
        if target is None:
            problems.add(at, "reference")
        else:
            reads.add(target)
    return reads


def _metric_ref(ref: object, names: _Names) -> MemberId | None:
    parts = tuple(sql.normalize(part) for part in str(ref).split("."))
    if len(parts) == 1:
        return names.model_metrics.get(parts[0])
    return names.dataset_metrics.get(parts[0], {}).get(parts[1])


def _payload_reads(
    payload: Json, member_id: MemberId, names: _Names, at: str, problems: _Problems
) -> set[MemberId]:
    """The members a field's or metric's payload expressions read."""
    reads: set[MemberId] = set()
    texts: list[object] = []
    own_row: str | None = None
    calculated = payload.get("calculated")
    if isinstance(calculated, Mapping):
        texts.append(calculated["expression"])
        own_row = member_id.dataset
    over = payload.get("over_metrics")
    if isinstance(over, Mapping):
        texts.append(over["expression"])
    components = payload.get("components")
    if isinstance(components, Mapping):
        texts += [components["numerator"], components["denominator"]]
    for text in texts:
        tree = _parse_product(text, at, problems)
        if tree is not None:
            reads |= _resolve_product(tree, own_row, names, at, problems)
    window = payload.get("window")
    if isinstance(window, Mapping):
        target = _metric_ref(window["of"], names)
        if target is None:
            problems.add(at, "reference")
        else:
            reads.add(target)
    return reads


def _model_payload_reads(payload: Json, names: _Names, at: str, problems: _Problems) -> None:
    """Calculation items, segment conditions and funnel steps: parsed and resolved here; their
    semantics are their capabilities' issues (S1, S10, S11)."""
    texts: list[object] = []
    for group in _list(payload, "calculation_groups"):
        texts += [item["expression"] for item in _list(group, "items")]
    texts += [segment["condition"] for segment in _list(payload, "segments")]
    for funnel in _list(payload, "funnels"):
        steps = funnel.get("steps")
        texts += list(steps) if isinstance(steps, tuple) else []
    for text in texts:
        tree = _parse_product(text, at, problems)
        if tree is not None:
            _resolve_product(tree, None, names, at, problems)


# --- cycles, exclusion and lineage --------------------------------------------------------------


def _refuse_cycles(drafts: Mapping[MemberId, _Draft], names: _Names) -> None:
    """A cycle through metrics or calculated fields → `METRIC_CYCLE`, naming its members in
    the order they depend on each other, from the smallest back to itself."""
    state: dict[MemberId, int] = {}  # 1 visiting, 2 done
    for start in sorted(drafts):
        if state.get(start):
            continue
        stack: list[tuple[MemberId, Iterator[MemberId]]] = [
            (start, iter(sorted(drafts[start].reads)))
        ]
        path = [start]
        state[start] = 1
        while stack:
            current, pending = stack[-1]
            following = next(pending, None)
            if following is None:
                state[current] = 2
                stack.pop()
                path.pop()
                continue
            if following not in drafts or state.get(following) == 2:
                continue
            if state.get(following) == 1:
                cycle = path[path.index(following) :]
                first = cycle.index(min(cycle))
                cycle = cycle[first:] + cycle[:first]
                closed = [*cycle, cycle[0]]  # the path returns to where it started
                raise SemanticError(
                    "METRIC_CYCLE", {"cycle": [names.labels[member] for member in closed]}
                )
            state[following] = 1
            path.append(following)
            stack.append((following, iter(sorted(drafts[following].reads))))


def _finish(
    drafts: Mapping[MemberId, _Draft], names: _Names, advisories: tuple[Advisory, ...]
) -> ParsedModel:
    excluded: dict[MemberId, str] = {}
    lineage: dict[MemberId, tuple[MemberId, ...]] = {}

    def settle(member: MemberId) -> None:
        # Dependencies first; the graph is acyclic (checked above).
        if member in lineage or member in excluded:
            return
        draft = drafts[member]
        if draft.excluded is not None:
            excluded[member] = draft.excluded
            return
        found: set[MemberId] = set(draft.opaque_lineage)
        if member.kind == "field" and not draft.reads:
            found.add(member)
        for read in sorted(draft.reads):
            if read not in drafts:
                continue
            settle(read)
            if read in excluded:
                excluded[member] = excluded[read]
                return
            found |= set(lineage[read])
        lineage[member] = tuple(sorted(found))

    for member in sorted(drafts):
        settle(member)
    members = {
        member: ParsedMember(
            member,
            names.labels[member],
            draft.pointer,
            draft.language,
            draft.tree,
            tuple(sorted(draft.reads)),
            lineage[member],
        )
        for member, draft in drafts.items()
        if member in lineage
    }
    return ParsedModel(MappingProxyType(members), MappingProxyType(excluded), advisories)
