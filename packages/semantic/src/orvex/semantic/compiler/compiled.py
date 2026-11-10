"""The compiled model and its fingerprints (P2-04e, #291; #280 is the source of truth).

`compile_model(model, catalog, vendor_names)` returns an immutable `CompiledModel` or raises
`SemanticError`. It runs P2-04b's parser, P2-04c's and P2-04d's types and units, then:

**Only executable entries compile** (ADR-0030 §1, §6). A model read through a `read_only` or
`rejected` registry entry, or an `experimental` one without the project's opt-in, is refused:
`OSSIE_INVALID`, rule `unsupported_capability`, at `/version`.

**Declarations (#280 §3).** A metric in one of these classes, decided from its typed tree and
never from names, does not compile without the declarations it needs →
`MISSING_EXTENSION_DECLARATION` (`element`, `name`, `declaration`):
- a holistic or sketch aggregate → `additivity` and `aggregate_class`;
- an approximate (sketch) aggregate → also `approximate`;
- a ratio of aggregates, or `AVG` over exact inputs → `additivity` (`recomputable` with
  `components`, or `non_additive`), `zero_denominator` and `rounding`;
- an aggregate over a dataset with a `snapshot` or `validity` time role → `additivity`; a
  `semi_additive` one → its `non_additive_axes`, and `period_end` for a period-end axis;
- a metric that reads a declared bridge, or datasets connected only through one → `allocation`.

When several are missing, the first in `DECLARATION_ORDER` is reported. Any other member
lacking a declaration in `ANSWERABILITY` compiles with it in `missing`; P2-10 exposes it and
P2-11 onward refuses a query that needs it.

**Fingerprints (#280 §4, ADR-0030 §6).** RFC 8785 documents of normalized identifiers and typed
trees, never text: unordered collections sorted, ordered ones kept.
- A member's `semantic_fingerprint` covers its typed tree (catalog types, units and zones
  included), its selected dialect, its meaning-bearing declarations and its dependencies'
  fingerprints (a Merkle tree).
- The model's `semantic_fingerprint` covers every member, dataset and relationship, the model
  payload, the members not compiled, the reader's id and capabilities version, the compiler
  rules version and the pinned data versions (tzdata, the units registry, the ISO 4217
  snapshot). It excludes `description`, `label`, `ai_context`, `format`, `owner` and
  `certification`, and the registry entry's `id`, which is recorded beside it.
- `model_fingerprint` covers all of that plus the excluded descriptive members and the foreign
  entries (vendor and the SHA-256 of their data).
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from decimal import Decimal
from types import MappingProxyType

from orvex.semantic.canonical import fingerprint
from orvex.semantic.compiler.catalog import SourceCatalog
from orvex.semantic.compiler.parsing import MemberId, ParsedMember, ParsedModel, parse_model
from orvex.semantic.compiler.typecheck import (
    NodeType,
    TypedMember,
    TypedModel,
    TypedNode,
    type_model,
)
from orvex.semantic.errors import SemanticError
from orvex.semantic.ossie import sql
from orvex.semantic.ossie.model import Advisory, ForeignExtension, OssieModel

COMPILER_RULES = 1
FINGERPRINT_FORMAT = 1

DECLARATION_ORDER = (
    "additivity",
    "aggregate_class",
    "approximate",
    "components",
    "zero_denominator",
    "rounding",
    "non_additive_axes",
    "period_end",
    "allocation",
)
ANSWERABILITY = ("rounding", "zero_denominator", "numeric", "time_zone", "additivity", "datatype")
DESCRIPTIVE = frozenset({"description", "label", "ai_context"})
DESCRIPTIVE_PAYLOAD = frozenset({"format", "owner", "certification"})
_MEANS = frozenset({"AVG", "MEDIAN", "PERCENTILE_CONT"})

Json = Mapping[str, object]


@dataclass(frozen=True)
class CompiledMember:
    id: MemberId
    parsed: ParsedMember
    typed: TypedMember
    missing: tuple[str, ...]  # answerability: declarations a query may need (#280 §3)
    semantic_fingerprint: str


@dataclass(frozen=True)
class CompiledModel:
    registry_entry: str  # recorded, outside the identity (ADR-0030 §6)
    reader: str
    capabilities: int
    compiler_rules: int
    tzdata: str
    units: str
    currencies: str
    members: Mapping[MemberId, CompiledMember]
    excluded: Mapping[MemberId, str]  # member -> the operation that refuses it
    advisories: tuple[Advisory, ...]  # P2-03's, every one kept
    foreign_extensions: tuple[ForeignExtension, ...]
    typed: TypedModel
    semantic_fingerprint: str
    model_fingerprint: str
    ossie: OssieModel = field(repr=False, compare=False)  # the read document, for later stages

    def require(self, member: MemberId) -> CompiledMember:
        """The compiled member; `UNSUPPORTED_OPERATION` when it was not compiled and
        `MEMBER_NOT_FOUND` when the model has no such member."""
        if member not in self.members and member not in self.excluded:
            name = f"{member.dataset}.{member.name}" if member.dataset else member.name
            raise SemanticError("MEMBER_NOT_FOUND", {"member": name})
        self.typed.parsed.require(member)
        return self.members[member]

    def reads(self, member: MemberId) -> tuple[MemberId, ...]:
        """`member` and every field and metric it reads, transitively, in a fixed order: the
        member first, then each dependency depth-first in its sorted `depends_on` (#426). A
        declaration any of them lacks is the question's, wherever the expression is inlined."""
        found: dict[MemberId, None] = {}
        pending = [member]
        while pending:
            current = pending.pop()
            if current in found:
                continue
            found[current] = None
            pending.extend(reversed(self.require(current).parsed.depends_on))
        return tuple(found)

    def lacking(
        self, member: MemberId, besides: tuple[str, ...] = ()
    ) -> tuple[tuple[MemberId, str], ...]:
        """Each declaration `member` or anything it reads lacks, but those in `besides`, with
        the member lacking it, in the order of `reads`."""
        return tuple(
            (read, missing)
            for read in self.reads(member)
            for missing in self.require(read).missing
            if missing not in besides
        )


# --- JSON documents -------------------------------------------------------------------------


_SAFE_INTEGER = 2**53 - 1


def _json(value: object) -> object:
    """A document value as plain JSON: tuples become lists, mappings dicts. A number the
    canonical form does not hold (a decimal, an integer beyond ±(2^53-1)) becomes its text
    (#280 §4), tagged so that it never equals a string."""
    if isinstance(value, Mapping):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_json(item) for item in value]
    if isinstance(value, Decimal):
        return {"decimal": str(value)}
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > _SAFE_INTEGER:
        return {"decimal": str(value)}
    return value


# Payload collections whose order carries no meaning (sets of names, axes or named objects); every
# other list, such as `order_by`, drill-path `levels`, funnel `steps` or `context`, is ordered.
UNORDERED = frozenset(
    """
    allowed dimensions entities entity_key entity_keys evidence fields grain ignore_filters
    metrics non_additive_axes partition_by per_entities targets calculation_groups calendars
    cohorts drill_paths funnels parameters preaggregations segments
    """.split()  # noqa: SIM905 - a word list reads better than one string per line
)


def _payload_document(payload: Json) -> object:
    """A payload without its descriptive members, its unordered collections sorted."""

    def walk(value: object, key: str | None) -> object:
        if isinstance(value, Mapping):
            return {str(k): walk(v, str(k)) for k, v in value.items()}
        if isinstance(value, tuple | list):
            items = [walk(item, None) for item in value]
            if key in UNORDERED:
                items.sort(key=lambda item: json.dumps(item, sort_keys=True))
            return items
        return _json(value)

    return walk({k: v for k, v in payload.items() if k not in DESCRIPTIVE_PAYLOAD}, None)


def _without(value: object, excluded: frozenset[str]) -> object:
    """`value` without the descriptive members, at any depth."""
    if isinstance(value, Mapping):
        return {
            str(key): _without(item, excluded)
            for key, item in value.items()
            if str(key) not in excluded
        }
    if isinstance(value, tuple | list):
        return [_without(item, excluded) for item in value]
    return _json(value)


def _key(member: MemberId) -> list[str]:
    return [member.kind, member.dataset or "", member.name]


def _type_document(node_type: NodeType) -> dict[str, object]:
    logical = node_type.logical
    unit = node_type.unit
    return {
        "datatype": logical.datatype,
        "precision": logical.precision,
        "scale": logical.scale,
        "width": logical.width,
        "signed": logical.signed,
        "zone": logical.zone,
        "semantic_type": node_type.semantic_type,
        "nullable": node_type.nullable,
        "exactness": node_type.exactness,
        "kind": node_type.kind,
        "aggregate_class": node_type.aggregate_class,
        "volatility": node_type.volatility,
        "overflow_check": node_type.overflow_check,
        "constant": node_type.constant,
        "bucket_zone": node_type.bucket_zone,
        "buckets_instant": node_type.buckets_instant,
        "unit": None
        if unit is None
        else {
            "factor": f"{unit.factor.numerator}/{unit.factor.denominator}",
            "dimensions": [[name, power] for name, power in unit.dimensions],
        },
    }


def _node_document(node: sql.Node) -> dict[str, object]:
    """The node's own members; a child is `#`, a tuple of children its length (the children
    themselves follow in the typed tree, in a fixed order)."""
    found: dict[str, object] = {"node": type(node).__name__}
    assert is_dataclass(node)
    for item in fields(node):
        value = getattr(node, item.name)
        if is_dataclass(value):
            found[item.name] = "#"
        elif isinstance(value, tuple) and value and not isinstance(value[0], str | int):
            found[item.name] = len(value)
        else:
            found[item.name] = _json(value)
    return found


def _tree_document(tree: TypedNode) -> dict[str, object]:
    return {
        "node": _node_document(tree.node),
        "type": _type_document(tree.type),
        "children": [_tree_document(child) for child in tree.children],
    }


# --- the model's elements ---------------------------------------------------------------------


def position(model: OssieModel, pointer: str) -> tuple[int, tuple[tuple[int, int | str], ...]]:
    """Document order of an element in either format: its line, then its pointer read by
    segment (`/metrics/2` before `/metrics/10`)."""
    segments = tuple(
        (0, int(part)) if part.isdigit() else (1, part) for part in pointer.split("/")[1:]
    )
    return model.line(pointer), segments


def _items(node: object, key: str) -> list[Json]:
    value = node.get(key) if isinstance(node, Mapping) else None
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, tuple) else []


def _elements(document: Json) -> Iterator[tuple[str, list[str], Json]]:
    """(pointer, normalized key, node) of the model and every dataset, field, metric and
    relationship."""
    yield "", ["model"], document
    for at, dataset in enumerate(_items(document, "datasets")):
        name = sql.normalize(str(dataset["name"]))
        yield f"/datasets/{at}", ["dataset", name], dataset
        for kind in ("fields", "metrics"):
            for number, member in enumerate(_items(dataset, kind)):
                key = [kind[:-1], name, sql.normalize(str(member["name"]))]
                yield f"/datasets/{at}/{kind}/{number}", key, member
    for at, metric in enumerate(_items(document, "metrics")):
        yield f"/metrics/{at}", ["metric", "", sql.normalize(str(metric["name"]))], metric
    for at, relationship in enumerate(_items(document, "relationships")):
        name = sql.normalize(str(relationship["name"]))
        yield f"/relationships/{at}", ["relationship", name], relationship


def _names(value: object) -> list[str]:
    return [sql.normalize(str(item)) for item in value] if isinstance(value, tuple) else []


# Child collections, fingerprinted as elements of their own; product payloads are read apart and
# foreign ones are listed by their hash.
_CHILDREN = frozenset({"fields", "metrics", "datasets", "relationships", "custom_extensions"})
_STRUCTURE = frozenset(
    {"name", "expression", "custom_extensions", "fields", "metrics", "datasets", "relationships"}
)


def _ossie_document(kind: str, node: Json) -> dict[str, object]:
    """The element's Ossie members that carry meaning, normalized."""
    kept = {k: v for k, v in node.items() if k not in _STRUCTURE and k not in DESCRIPTIVE}
    found = _without(kept, DESCRIPTIVE)
    assert isinstance(found, dict)
    if kind == "dataset":
        if "primary_key" in node:
            found["primary_key"] = sorted(_names(node["primary_key"]))
        if "unique_keys" in node:
            keys = node["unique_keys"]
            found["unique_keys"] = sorted(
                sorted(_names(key)) for key in (keys if isinstance(keys, tuple) else ())
            )
    if kind == "relationship":
        for side in ("from", "to"):
            if side in node:
                found[side] = sql.normalize(str(node[side]))
        pairs = zip(_names(node.get("from_columns")), _names(node.get("to_columns")), strict=False)
        found.pop("from_columns", None)
        found.pop("to_columns", None)
        found["columns"] = sorted([list(pair) for pair in pairs])
    return found


# --- declarations -----------------------------------------------------------------------------


def _walk(tree: TypedNode | None) -> Iterator[TypedNode]:
    if tree is None:
        return
    yield tree
    for child in tree.children:
        yield from _walk(child)


def nonzero_literal(node: sql.Node) -> bool:
    """A numeric literal other than zero, signed or not: a divisor that needs no zero policy."""
    if isinstance(node, sql.Unary) and node.op in ("-", "+"):
        return nonzero_literal(node.operand)
    return isinstance(node, sql.Number) and Decimal(node.text) != 0


def zero_literal(node: sql.Node) -> bool:
    """A numeric literal equal to zero, signed or not: a divisor that never gives a value."""
    if isinstance(node, sql.Unary) and node.op in ("-", "+"):
        return zero_literal(node.operand)
    return isinstance(node, sql.Number) and Decimal(node.text) == 0


def _is_ratio(node: TypedNode) -> bool:
    if not (isinstance(node.node, sql.Binary) and node.node.op == "/"):
        return False
    return all(
        child.type.kind in ("aggregate", "metric") and not child.type.constant
        for child in node.children
    )


def _is_exact_mean(node: TypedNode) -> bool:
    return (
        isinstance(node.node, sql.Call)
        and node.node.name == "AVG"
        and bool(node.children)
        and node.children[0].type.exactness == "exact"
        and node.children[0].type.logical.datatype in ("Integer", "Decimal")
    )


class _Model:
    def __init__(self, model: OssieModel, parsed: ParsedModel, typed: TypedModel) -> None:
        self.model = model
        self.parsed = parsed
        self.typed = typed
        self.payloads: dict[str, Json] = {}
        for extension in model.product_extensions:
            element = extension.pointer.rsplit("/custom_extensions/", 1)[0]
            self.payloads[element] = extension.payload
        datasets = _items(model.document, "datasets")
        self.dataset_payloads = {
            sql.normalize(str(d["name"])): self.payloads.get(f"/datasets/{at}", {})
            for at, d in enumerate(datasets)
        }
        self.bridges = {name for name, p in self.dataset_payloads.items() if "bridge" in p}
        self.links: dict[str, set[str]] = {name: set() for name in self.dataset_payloads}
        for relationship in _items(model.document, "relationships"):
            left = sql.normalize(str(relationship["from"]))
            right = sql.normalize(str(relationship["to"]))
            self.links.setdefault(left, set()).add(right)
            self.links.setdefault(right, set()).add(left)
        self.selected = {e.element: e for e in model.expressions}
        self.ratios: dict[MemberId, bool] = {}

    def payload(self, member: ParsedMember) -> Json:
        return self.payloads.get(member.pointer, {})

    def context(self, member: ParsedMember, payload: Json) -> object:
        """What a member's meaning rests on beyond its own expressions: the datasets it reads
        (source, keys, payload), the relationships when it reads more than one, the reader's
        identity, the compiler rules and the pinned data versions."""
        datasets = self.datasets_of(member, payload)
        read = [
            {
                "key": key,
                "ossie": _ossie_document("dataset", node),
                "payload": _payload_document(self.payloads.get(pointer, {})),
            }
            for pointer, key, node in sorted(
                (
                    e
                    for e in _elements(self.model.document)
                    if e[1][0] == "dataset" and e[1][1] in datasets
                ),
                key=lambda e: e[1],
            )
        ]
        relationships = (
            [
                {
                    "key": key,
                    "ossie": _ossie_document("relationship", node),
                    "payload": _payload_document(self.payloads.get(pointer, {})),
                }
                for pointer, key, node in sorted(
                    (e for e in _elements(self.model.document) if e[1][0] == "relationship"),
                    key=lambda e: e[1],
                )
            ]
            if len(datasets) > 1
            else []
        )
        return {
            "identity": _identity(self.model),
            "data": {
                "tzdata": self.typed.tzdata,
                "units": self.typed.units,
                "currencies": self.typed.currencies,
            },
            "datasets": read,
            "relationships": relationships,
        }

    # the compile-time classes

    def datasets_of(self, member: ParsedMember, payload: Json) -> set[str]:
        found = {field.dataset for field in member.lineage if field.dataset is not None}
        if member.id.dataset is not None:
            found.add(member.id.dataset)
        if "home_dataset" in payload:
            found.add(sql.normalize(str(payload["home_dataset"])))
        return found

    def only_through_a_bridge(self, datasets: set[str]) -> bool:
        """Datasets connected to each other, but only through a declared bridge."""
        if len(datasets) < 2:
            return False

        def reached(start: str, avoid: set[str]) -> set[str]:
            seen, pending = {start}, [start]
            while pending:
                for following in self.links.get(pending.pop(), set()) - avoid - seen:
                    seen.add(following)
                    pending.append(following)
            return seen

        start = min(datasets)
        return datasets <= reached(start, set()) and not datasets <= reached(start, self.bridges)

    def required(self, member: ParsedMember, typed: TypedMember) -> set[str]:
        payload = self.payload(member)
        nodes = list(_walk(typed.tree)) + [n for _, tree in typed.payload for n in _walk(tree)]
        needs: set[str] = set()
        if typed.type.aggregate_class in ("holistic", "sketch"):
            needs |= {"additivity", "aggregate_class"}
        sketch = any(
            isinstance(n.node, sql.Call) and sql.AGGREGATES.get(n.node.name) == "sketch"
            for n in nodes
        )
        if sketch:
            needs |= {"additivity", "aggregate_class", "approximate"}
        # `components` with `recomputable`, `non_additive_axes` with `semi_additive` and
        # `period_end` with a period-end axis are the payload's own conditions, which P2-03
        # already enforces (`ossie-extensions/v1`); a payload that reaches here meets them.
        if self.is_ratio(member.id):
            needs |= {"additivity", "zero_denominator", "rounding"}
        datasets = self.datasets_of(member, payload)
        roles = [self.dataset_payloads.get(d, {}).get("time_roles") for d in datasets]
        if any(isinstance(r, Mapping) and ("snapshot" in r or "validity" in r) for r in roles):
            needs.add("additivity")
        if datasets & self.bridges or self.only_through_a_bridge(datasets):
            needs.add("allocation")
        return needs

    def is_ratio(self, member_id: MemberId) -> bool:
        """A ratio of aggregates or an exact mean, in the member's own expressions or in a
        metric it is computed from: a ratio stays a ratio one level up (#280 §3)."""
        if member_id not in self.ratios:
            typed = self.typed.members[member_id]
            own = any(
                _is_ratio(n) or _is_exact_mean(n)
                for tree in (typed.tree, *(t for _, t in typed.payload))
                for n in _walk(tree)
            )
            self.ratios[member_id] = own or any(
                dependency.kind == "metric"
                and dependency in self.typed.members
                and self.is_ratio(dependency)
                for dependency in self.parsed.members[member_id].depends_on
            )
        return self.ratios[member_id]

    def refuse_ambiguous(self, member: ParsedMember, typed: TypedMember) -> None:
        payload = self.payload(member)
        needs = self.required(member, typed)
        ratio = self.is_ratio(member.id)
        for declaration in DECLARATION_ORDER:
            if declaration not in needs:
                continue
            present = declaration in payload
            if declaration == "additivity" and ratio:
                # A ratio is recomputed from its components or declared non-additive.
                present = payload.get("additivity") in ("recomputable", "non_additive")
            if not present:
                raise SemanticError(
                    "MISSING_EXTENSION_DECLARATION",
                    {"element": "metric", "name": member.label, "declaration": declaration},
                )

    # answerability

    def missing(self, member: ParsedMember, typed: TypedMember) -> tuple[str, ...]:
        payload = self.payload(member)
        nodes = list(_walk(typed.tree)) + [n for _, tree in typed.payload for n in _walk(tree)]
        found: set[str] = set()
        divisions = [n for n in nodes if isinstance(n.node, sql.Binary) and n.node.op == "/"]
        means = [
            n
            for n in nodes
            if isinstance(n.node, sql.Call)
            and n.node.name in _MEANS
            and any(c.type.logical.datatype in ("Integer", "Decimal") for c in n.children)
        ]
        exact_divisions = [n for n in divisions if n.type.logical.datatype == "Decimal"]
        if (exact_divisions or means) and "rounding" not in payload:
            found.add("rounding")
        if "zero_denominator" not in payload and any(
            isinstance(n.node, sql.Binary) and not nonzero_literal(n.node.right) for n in divisions
        ):
            found.add("zero_denominator")
        logical = typed.type.logical
        if typed.tree is None and (
            (logical.datatype == "Decimal" and (logical.precision is None or logical.scale is None))
            or (logical.datatype == "Integer" and logical.width is None)
        ):
            found.add("numeric")
        if any(n.type.buckets_instant and n.type.bucket_zone is None for n in nodes):
            found.add("time_zone")
        if member.id.kind == "metric" and "additivity" not in payload:
            found.add("additivity")
        if typed.tree is None and logical.datatype is None:
            found.add("datatype")
        return tuple(d for d in ANSWERABILITY if d in found)


# --- compiling --------------------------------------------------------------------------------


def _refuse_unexecutable(model: OssieModel, experimental: bool) -> None:
    if model.registry_state == "stable" or (
        model.registry_state == "experimental" and experimental
    ):
        return
    raise SemanticError(
        "OSSIE_INVALID",
        {
            "file": model.path,
            "problems": [
                {
                    "pointer": "/version",
                    "line": str(model.line("/version")),
                    "rule": "unsupported_capability",
                }
            ],
            "problems_omitted": "0",
        },
    )


def compile_model(
    model: OssieModel,
    catalog: SourceCatalog,
    vendor_names: Sequence[str],
    *,
    experimental: bool = False,
) -> CompiledModel:
    """Compile a read model against its source catalog. `experimental` is the project's opt-in
    to `experimental` registry entries, supplied by `server`."""
    _refuse_unexecutable(model, experimental)
    parsed = parse_model(model, vendor_names)
    typed = type_model(model, parsed, catalog)
    state = _Model(model, parsed, typed)
    # The first ambiguous metric in document order is reported.
    for member_id in sorted(
        typed.members,
        key=lambda m: position(model, parsed.members[m].pointer),
    ):
        if member_id.kind == "metric":
            state.refuse_ambiguous(parsed.members[member_id], typed.members[member_id])

    compiled: dict[MemberId, CompiledMember] = {}

    def member(member_id: MemberId) -> CompiledMember:
        # Dependencies first: the graph is acyclic (P2-04b).
        if member_id in compiled:
            return compiled[member_id]
        parsed_member = parsed.members[member_id]
        typed_member = typed.members[member_id]
        dependencies = {
            "/".join(_key(dependency)): member(dependency).semantic_fingerprint
            for dependency in parsed_member.depends_on
            if dependency in typed.members
        }
        missing = state.missing(parsed_member, typed_member)
        payload = state.payload(parsed_member)
        choice = state.selected.get(parsed_member.pointer)
        node = next(n for p, _, n in _elements(model.document) if p == parsed_member.pointer)
        document = {
            "member": _key(member_id),
            "context": state.context(parsed_member, payload),
            "language": parsed_member.language,
            "dialect": choice.dialect if choice is not None else None,
            "tree": _tree_document(typed_member.tree) if typed_member.tree is not None else None,
            "text": choice.text if typed_member.tree is None and choice is not None else None,
            "type": _type_document(typed_member.type),
            "ossie": _ossie_document(member_id.kind, node),
            "payload": _payload_document(payload),
            "payload_trees": {label: _tree_document(tree) for label, tree in typed_member.payload},
            "depends_on": dependencies,
            "missing": list(missing),
        }
        compiled[member_id] = CompiledMember(
            member_id,
            parsed_member,
            typed_member,
            missing,
            fingerprint("orvex.semantic.member", FINGERPRINT_FORMAT, document),
        )
        return compiled[member_id]

    for member_id in sorted(typed.members):
        member(member_id)

    semantic = fingerprint(
        "orvex.semantic.model", FINGERPRINT_FORMAT, _model_document(state, compiled)
    )
    whole = fingerprint(
        "orvex.semantic.model.all",
        FINGERPRINT_FORMAT,
        {
            "semantic": semantic,
            "rest": _rest_document(model, state),
            "foreign": _foreign_document(model),
        },
    )
    return CompiledModel(
        registry_entry=model.registry_entry,
        reader=model.reader,
        capabilities=model.capabilities,
        compiler_rules=COMPILER_RULES,
        tzdata=typed.tzdata,
        units=typed.units,
        currencies=typed.currencies,
        members=MappingProxyType(dict(sorted(compiled.items()))),
        excluded=parsed.excluded,
        advisories=model.advisories,
        foreign_extensions=model.foreign_extensions,
        typed=typed,
        semantic_fingerprint=semantic,
        model_fingerprint=whole,
        ossie=model,
    )


def _identity(model: OssieModel) -> dict[str, object]:
    return {
        "reader": model.reader,
        "capabilities": model.capabilities,
        "compiler_rules": COMPILER_RULES,
    }


def _model_document(state: _Model, compiled: Mapping[MemberId, CompiledMember]) -> object:
    model = state.model
    elements: dict[str, list[dict[str, object]]] = {"dataset": [], "relationship": []}
    for pointer, key, node in _elements(model.document):
        if key[0] in elements:
            elements[key[0]].append(
                {
                    "key": key,
                    "ossie": _ossie_document(key[0], node),
                    "payload": _payload_document(state.payloads.get(pointer, {})),
                }
            )
    for found in elements.values():
        found.sort(key=lambda item: json.dumps(item["key"]))
    return {
        "identity": _identity(model),
        "data": {
            "tzdata": state.typed.tzdata,
            "units": state.typed.units,
            "currencies": state.typed.currencies,
        },
        "model": {
            "ossie": _ossie_document("model", model.document),
            "payload": _payload_document(state.payloads.get("", {})),
        },
        "datasets": elements["dataset"],
        "relationships": elements["relationship"],
        "members": [
            [_key(member), compiled[member].semantic_fingerprint] for member in sorted(compiled)
        ],
        "excluded": [
            [_key(member), operation] for member, operation in sorted(state.parsed.excluded.items())
        ],
        "model_expressions": [
            [label, _tree_document(tree)] for label, tree in sorted(state.typed.expressions)
        ],
    }


def _rest_document(model: OssieModel, state: _Model) -> object:
    """Everything of each element that the semantic fingerprint leaves out: names as written,
    every dialect's text (those not selected included), descriptions, labels, `ai_context` and
    the descriptive payload members."""
    found = []
    for pointer, key, node in _elements(model.document):
        kept = {k: v for k, v in node.items() if k not in _CHILDREN}
        payload = state.payloads.get(pointer, {})
        found.append(
            {
                "key": key,
                "ossie": _json(kept),
                "payload": {k: _json(v) for k, v in payload.items() if k in DESCRIPTIVE_PAYLOAD},
            }
        )
    return sorted(found, key=lambda item: json.dumps(item["key"]))


def _foreign_document(model: OssieModel) -> object:
    keys = {pointer: key for pointer, key, _ in _elements(model.document)}
    return sorted(
        [
            keys.get(extension.pointer.rsplit("/custom_extensions/", 1)[0], ["model"]),
            extension.vendor_name,
            extension.data_sha256,
        ]
        for extension in model.foreign_extensions
    )
