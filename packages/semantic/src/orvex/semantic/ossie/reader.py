"""Read one Ossie document into an `OssieModel`, or raise `SemanticError` (P2-03, ADR-0030).

The reading stages stay apart (ADR-0030 §2). Each step below runs on the format-independent
values `documents.parse` returns; a failing step stops the read and reports all its problems,
ordered by line, then pointer, at most 50 (`semantic-error/v1`):

1. safe parsing (`syntax`);
2. the version, through the registry (`UNSUPPORTED_OSSIE_VERSION`; `schema` when it is missing
   or not a version);
3. the Ossie JSON Schema of the matched registry entry (`schema`);
4. names: lengths (`schema`) and uniqueness (`duplicate_name`);
5. references (`reference`), and OSSIE_SQL_2026 texts outside the subset's grammar
   (`expression`), as the official validator rejects them;
6. the product's extensions (`extension`, `extension_duplicate`, `reference`,
   `extension_conflict`); other vendors' entries are listed and never interpreted;
7. dialects: two entries for one dialect (`schema`), then the deterministic selection of each
   field's and metric's expression.

What Ossie accepts loads (Owner decision of 2026-11-01). Where the product needs more than Ossie
requires to compile or execute safely, the read records an `Advisory` instead of an error: a
relationship whose `to_columns` cover no key of `to` (the official validator's warning), two
entries for one dialect, a product-dialect entry without a derived declaration, a member with no
expression the product can select, valid OSSIE_SQL_2026 that the product does not implement. The
stage that would use the element refuses it then.

No error carries text from the document: a pointer holds only keys the Ossie schema declares
and indexes, and a line number.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from types import MappingProxyType
from typing import Any, cast

from jsonschema import Draft202012Validator
from orvex.semantic.errors import SemanticError
from orvex.semantic.ossie import extensions, identifiers, sql
from orvex.semantic.ossie.documents import MAX_BYTES, Format, ParseProblem, Path, parse
from orvex.semantic.ossie.identifiers import normalize
from orvex.semantic.ossie.model import (
    Advisory,
    ElementKind,
    ForeignExtension,
    OssieModel,
    ProductExtension,
    SelectedExpression,
    freeze,
)
from orvex.semantic.ossie.registry import Entry, Registry, default_registry

MAX_PROBLEMS = 50
PORTABLE = "OSSIE_SQL_2026"
_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]{0,63}")  # semantic-error/v1 `Version`
_SUFFIXES: dict[str, Format] = {".ossie.yaml": "yaml", ".ossie.json": "json"}

Json = dict[str, Any]


def format_of(path: str) -> Format:
    for suffix, written in _SUFFIXES.items():
        if path.endswith(suffix):
            return written
    raise ValueError(f"an Ossie document ends in one of {sorted(_SUFFIXES)}")


# --- problems ---------------------------------------------------------------------------------


@cache
def _schema_keys(entry_id: str, registry: Registry) -> frozenset[str]:
    keys: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                keys.update(properties)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    entry = next(entry for entry in registry.entries if entry.id == entry_id)
    walk(entry.schema)
    return frozenset(keys)


def _escape(part: str | int) -> str:
    return str(part).replace("~", "~0").replace("/", "~1")


class _Problems:
    """The problems of one step. A pointer keeps only keys the schema declares and indexes:
    it stops before the first other key, so no text of the document reaches it."""

    def __init__(self, path: str, lines: Mapping[Path, int], known: frozenset[str]) -> None:
        self.file = path
        self.lines = lines
        self.known = known
        self.found: set[tuple[int, str, str]] = set()

    def add(self, at: Path, rule: str) -> None:
        safe: list[str | int] = []
        for part in at:
            if isinstance(part, str) and part not in self.known:
                break
            safe.append(part)
        line = 1
        for end in range(len(at), -1, -1):
            if at[:end] in self.lines:
                line = self.lines[at[:end]]
                break
        pointer = "".join("/" + _escape(part) for part in safe)
        self.found.add((line, pointer, rule))

    def raise_if_any(self) -> None:
        if not self.found:
            return
        ordered = sorted(self.found)
        shown = ordered[:MAX_PROBLEMS]
        raise SemanticError(
            "OSSIE_INVALID",
            {
                "file": self.file,
                "problems": [
                    {"pointer": pointer, "line": str(line), "rule": rule}
                    for line, pointer, rule in shown
                ],
                "problems_omitted": str(len(ordered) - len(shown)),
            },
        )


# --- the document's index -------------------------------------------------------------------


def _list(node: object, key: str) -> list[Any]:
    value = node.get(key) if isinstance(node, dict) else None
    return value if isinstance(value, list) else []


@dataclass
class _Dataset:
    path: Path
    node: Json
    fields: dict[str, Json] = field(default_factory=dict)
    metrics: dict[str, Json] = field(default_factory=dict)
    keys: list[frozenset[str]] = field(default_factory=list)


@dataclass
class _Index:
    datasets: dict[str, _Dataset]
    metrics: dict[str, Json]
    relationships: dict[str, Json]

    @classmethod
    def of(cls, document: Json) -> _Index:
        datasets: dict[str, _Dataset] = {}
        for at, node in enumerate(_list(document, "datasets")):
            dataset = _Dataset(("datasets", at), node)
            dataset.fields = {normalize(f["name"]): f for f in _list(node, "fields")}
            dataset.metrics = {normalize(m["name"]): m for m in _list(node, "metrics")}
            written = [node["primary_key"]] if "primary_key" in node else []
            dataset.keys = [
                frozenset(normalize(name) for name in key)
                for key in written + _list(node, "unique_keys")
            ]
            datasets.setdefault(normalize(node["name"]), dataset)
        return cls(
            datasets,
            {normalize(m["name"]): m for m in _list(document, "metrics")},
            {normalize(r["name"]): r for r in _list(document, "relationships")},
        )

    def dataset(self, name: object) -> _Dataset | None:
        return self.datasets.get(normalize(name)) if isinstance(name, str) else None

    def field_ref(self, ref: object, scope: Sequence[_Dataset | None]) -> bool:
        """`dataset.field`, or `field` naming a field of exactly one dataset in scope."""
        if not isinstance(ref, str):
            return False
        parts = [normalize(part) for part in ref.split(".")]
        if len(parts) == 2:
            dataset = self.datasets.get(parts[0])
            return dataset is not None and parts[1] in dataset.fields
        return sum(1 for d in scope if d is not None and parts[0] in d.fields) == 1

    def metric_ref(self, ref: object) -> bool:
        """`metric` for a model metric, `dataset.metric` for a dataset metric."""
        if not isinstance(ref, str):
            return False
        parts = [normalize(part) for part in ref.split(".")]
        if len(parts) == 1:
            return parts[0] in self.metrics
        dataset = self.datasets.get(parts[0])
        return dataset is not None and parts[1] in dataset.metrics


@dataclass(frozen=True)
class _Element:
    path: Path
    kind: ElementKind
    node: Json
    dataset: _Dataset | None = None  # the dataset a dataset, field or dataset metric belongs to
    sides: tuple[_Dataset | None, ...] = ()  # a relationship's `from` and `to`


def _elements(document: Json, index: _Index) -> Iterator[_Element]:
    yield _Element((), "model", document)
    for at, node in enumerate(_list(document, "datasets")):
        dataset = index.dataset(node["name"])
        path: Path = ("datasets", at)
        yield _Element(path, "dataset", node, dataset)
        for number, member in enumerate(_list(node, "fields")):
            yield _Element((*path, "fields", number), "field", member, dataset)
        for number, member in enumerate(_list(node, "metrics")):
            yield _Element((*path, "metrics", number), "metric", member, dataset)
    for at, node in enumerate(_list(document, "metrics")):
        yield _Element(("metrics", at), "metric", node)
    for at, node in enumerate(_list(document, "relationships")):
        sides = (index.dataset(node["from"]), index.dataset(node["to"]))
        yield _Element(("relationships", at), "relationship", node, sides=sides)


def _ossie_expressions(member: Json, path: Path) -> list[tuple[Path, str]]:
    """The authoritative OSSIE_SQL_2026 texts of a field or metric: `sql`, or else every
    `OSSIE_SQL_2026` dialect entry (an entry beside `sql` is non-authoritative, spec)."""
    expression = member["expression"]
    if "sql" in expression:
        return [((*path, "expression", "sql"), expression["sql"])]
    return [
        ((*path, "expression", "dialects", at, "expression"), entry["expression"])
        for at, entry in enumerate(_list(expression, "dialects"))
        if entry["dialect"] == PORTABLE
    ]


# --- steps ----------------------------------------------------------------------------------


def _parse(content: bytes, path: str, written: Format) -> tuple[Json | object, dict[Path, int]]:
    problems = _Problems(path, {}, frozenset())
    if len(content) > MAX_BYTES:
        problems.add((), "syntax")
        problems.raise_if_any()
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        problems.lines = {(): content.count(b"\n", 0, error.start) + 1}
        problems.add((), "syntax")
        problems.raise_if_any()
    try:
        parsed = parse(text, written)
    except ParseProblem as problem:
        # The schema is unknown before the version is read: a pointer here keeps indexes only.
        problems.lines = {problem.path: problem.line or 1}
        problems.add(problem.path, "syntax")
        problems.raise_if_any()
        raise AssertionError("unreachable") from None
    return parsed.data, parsed.lines


def _entry(data: object, path: str, lines: Mapping[Path, int], registry: Registry) -> Entry:
    problems = _Problems(path, lines, frozenset({"version"}))
    if not isinstance(data, dict):
        problems.add((), "schema")
    elif not isinstance(data.get("version"), str):
        problems.add(("version",) if "version" in data else (), "schema")
    problems.raise_if_any()
    assert isinstance(data, dict)
    version: str = data["version"]
    entry = registry.active(version)
    if entry is not None:
        return entry
    if not _VERSION.fullmatch(version):
        problems.add(("version",), "schema")
        problems.raise_if_any()
    raise SemanticError(
        "UNSUPPORTED_OSSIE_VERSION",
        {"found": version, "supported": registry.newest_stable().specification_version},
    )


@cache
def _schema_validator(entry_id: str, registry: Registry) -> Draft202012Validator:
    entry = next(entry for entry in registry.entries if entry.id == entry_id)
    return Draft202012Validator(entry.schema)


def _schema(document: Json, problems: _Problems, entry: Entry, registry: Registry) -> None:
    for error in _schema_validator(entry.id, registry).iter_errors(document):
        at: Path = tuple(error.absolute_path)
        if error.validator == "additionalProperties" and isinstance(error.instance, dict):
            # Located at each unknown member, so the line is its own.
            declared = error.schema.get("properties", {}) if isinstance(error.schema, dict) else {}
            for key in error.instance:
                if key not in declared:
                    problems.add((*at, key), "schema")
            continue
        problems.add(at, "schema")
    problems.raise_if_any()


def _names(document: Json, problems: _Problems) -> None:
    def unique(members: Iterator[tuple[Path, str]], sql_names: bool = True) -> None:
        seen: set[str] = set()
        for at, name in members:
            too_long = identifiers.identifier_length(name) > identifiers.MAX_IDENTIFIER
            # Datasets, fields and metrics are named in expressions: ANSI SQL names (L86).
            if too_long or (sql_names and not identifiers.is_valid_identifier(name)):
                problems.add(at, "schema")
            key = normalize(name)
            if key in seen:
                problems.add(at, "duplicate_name")
            seen.add(key)

    def named(key: str, node: Json, prefix: Path = ()) -> Iterator[tuple[Path, str]]:
        for at, member in enumerate(_list(node, key)):
            yield (*prefix, key, at, "name"), member["name"]

    # The model and relationships are never named inside an expression.
    unique(iter([(("name",), document["name"])]), sql_names=False)
    unique(named("datasets", document))
    for at, dataset in enumerate(_list(document, "datasets")):
        path: Path = ("datasets", at)
        # A dataset's fields and dataset metrics share one namespace (spec §Metric Scoping).
        unique(iter([*named("fields", dataset, path), *named("metrics", dataset, path)]))
    unique(named("metrics", document))
    unique(named("relationships", document), sql_names=False)
    problems.raise_if_any()


def _references(document: Json, index: _Index, problems: _Problems) -> list[Advisory]:
    advisories: list[Advisory] = []
    for at, dataset in enumerate(_list(document, "datasets")):
        own = index.dataset(dataset["name"])
        assert own is not None
        path: Path = ("datasets", at)
        for number, name in enumerate(dataset.get("primary_key") or []):
            if normalize(name) not in own.fields:
                problems.add((*path, "primary_key", number), "reference")
        for number, key in enumerate(_list(dataset, "unique_keys")):
            for place, name in enumerate(key):
                if normalize(name) not in own.fields:
                    problems.add((*path, "unique_keys", number, place), "reference")
    for at, relationship in enumerate(_list(document, "relationships")):
        path = ("relationships", at)
        sides = {
            "from": index.dataset(relationship["from"]),
            "to": index.dataset(relationship["to"]),
        }
        for side, dataset in sides.items():
            if dataset is None:
                problems.add((*path, side), "reference")
                continue
            for number, name in enumerate(relationship[f"{side}_columns"]):
                if normalize(name) not in dataset.fields:
                    problems.add((*path, f"{side}_columns", number), "reference")
        from_columns, to_columns = relationship["from_columns"], relationship["to_columns"]
        if len(from_columns) != len(to_columns):
            problems.add((*path, "to_columns"), "reference")
        target = sides["to"]
        covered = frozenset(normalize(name) for name in to_columns)
        # `from` is the many side only if `to` is unique. Ossie only warns, so the document
        # loads; joining through the relationship needs a key or a declared as-of match.
        if target is not None and not any(key <= covered for key in target.keys):
            advisories.append(Advisory(_pointer((*path, "to_columns")), "to_columns_cover_no_key"))
    metrics: list[tuple[Path, Json, _Dataset | None]] = [
        (("metrics", at), metric, None) for at, metric in enumerate(_list(document, "metrics"))
    ]
    for at, dataset in enumerate(_list(document, "datasets")):
        own = index.dataset(dataset["name"])
        metrics += [
            (("datasets", at, "metrics", number), metric, own)
            for number, metric in enumerate(_list(dataset, "metrics"))
        ]
    fields: list[tuple[Path, Json]] = [
        (("datasets", at, "fields", number), member)
        for at, dataset in enumerate(_list(document, "datasets"))
        for number, member in enumerate(_list(dataset, "fields"))
    ]
    for path, member in fields:
        # A field expression reads source columns, which the document does not declare: only
        # its syntax is checked here, as the official validator checks it.
        for text_at, text in _ossie_expressions(member, path):
            _parse_or_record(text, text_at, problems, advisories)
    for path, metric, own in metrics:
        for text_at, text in _ossie_expressions(metric, path):
            tree = _parse_or_record(text, text_at, problems, advisories)
            if tree is not None and not all(
                _resolves(ref.parts, own, index) for ref in sql.references(tree)
            ):
                problems.add(text_at, "reference")
    problems.raise_if_any()
    return advisories


def _parse_or_record(
    text: str, at: Path, problems: _Problems, advisories: list[Advisory]
) -> sql.Node | None:
    """The tree of an OSSIE_SQL_2026 text. Not one SQL expression at all, or one the
    specification forbids → `expression`, a breach of Ossie. SQL the product cannot read or
    does not implement → the `unreadable_expression` advisory: compiling the member refuses it,
    and the document still loads (ADR-0030 §2)."""
    try:
        return sql.parse(text)
    except sql.SqlSyntaxError:
        problems.add(at, "expression")
    except sql.SqlUnsupported:
        advisories.append(Advisory(_pointer(at), "unreadable_expression"))
    return None


def _resolves(parts: tuple[str, ...], own: _Dataset | None, index: _Index) -> bool:
    """A metric expression names fields only (spec L424): unqualified fields of its dataset
    in a dataset metric, `dataset.field` in a model metric. A metric is never named."""
    if own is not None:
        return len(parts) == 1 and parts[0] in own.fields
    if len(parts) != 2:
        return False
    dataset = index.datasets.get(parts[0])
    return dataset is not None and parts[1] in dataset.fields


@dataclass
class _Extensions:
    product: list[ProductExtension] = field(default_factory=list)
    foreign: list[ForeignExtension] = field(default_factory=list)


def _extensions(
    document: Json,
    index: _Index,
    problems: _Problems,
    vendor_names: Sequence[str],
) -> _Extensions:
    found = _Extensions()
    # A dataset's declared validity, by dataset; datasets come before relationships.
    validities: dict[int, Json] = {}
    for element in _elements(document, index):
        ours = 0
        for at, entry in enumerate(_list(element.node, "custom_extensions")):
            path: Path = (*element.path, "custom_extensions", at)
            pointer = "".join("/" + _escape(part) for part in path)
            vendor, data = entry["vendor_name"], entry["data"]
            if vendor not in vendor_names:
                digest = hashlib.sha256(data.encode("utf-8")).hexdigest()
                found.foreign.append(ForeignExtension(pointer, vendor, digest))
                continue
            ours += 1
            if ours > 1:
                problems.add(path, "extension_duplicate")
                continue
            at_data: Path = (*path, "data")
            try:
                payload = parse(data, "json").data
            except ParseProblem:
                problems.add(at_data, "extension")
                continue
            if (
                not isinstance(payload, dict)
                or not extensions.validator().is_valid(payload)
                or payload["element"] != element.kind
            ):
                problems.add(at_data, "extension")
                continue
            rule = _payload_rule(element, payload, index, validities)
            if rule is not None:
                problems.add(at_data, rule)
                continue
            validity = (payload.get("time_roles") or {}).get("validity")
            if element.kind == "dataset" and validity is not None and element.dataset is not None:
                validities[id(element.dataset)] = validity
            found.product.append(
                ProductExtension(
                    pointer, element.kind, vendor, cast(Mapping[str, object], freeze(payload))
                )
            )
    problems.raise_if_any()
    return found


def _payload_rule(
    element: _Element,
    payload: Json,
    index: _Index,
    validities: Mapping[int, Json],
) -> str | None:
    """The first rule a valid payload breaks against its document, if any."""
    if element.kind == "field" and extensions.breaks_datatype_rules(
        payload, element.node.get("datatype")
    ):
        return "extension"
    if element.kind == "metric" and "aggregate_class" in payload:
        # Each readable OSSIE_SQL_2026 text is checked on its own; an unsupported one is an
        # advisory, and the product's compiler checks other forms on its typed tree.
        for _, text in _ossie_expressions(element.node, element.path):
            try:
                tree = sql.parse(text)
            except sql.SqlSyntaxError, sql.SqlUnsupported:
                continue
            if sql.aggregate_class(tree) != payload["aggregate_class"]:
                return "extension"
    if not all(_payload_references(element, payload, index)):
        return "reference"
    if element.kind == "dataset" and element.dataset is not None:
        for key in payload.get("entity_keys") or []:
            fields = frozenset(normalize(name) for name in key["fields"])
            if not key.get("coarser", False) and fields not in element.dataset.keys:
                return "extension_conflict"
    if (
        element.kind == "relationship"
        and "temporal" in payload
        and not _coherent_temporal(element, payload["temporal"], index, validities)
    ):
        return "extension_conflict"
    if element.kind == "relationship" and payload.get("cardinality") == "one_to_one":
        source = element.sides[0]
        columns = frozenset(normalize(name) for name in element.node["from_columns"])
        if source is None or not any(key <= columns for key in source.keys):
            return "extension_conflict"
    return None


def _coherent_temporal(
    element: _Element, temporal: Json, index: _Index, validities: Mapping[int, Json]
) -> bool:
    """A declared as-of match must be coherent (#278, Owner decision of 2026-11-01):
    `event_time` a field of `from`; `valid_from` and `valid_to` two fields of `to`, outside
    `to_columns`; any `time_roles.validity` of `to` naming the same fields. When `to_columns`
    cover no key of `to`, a key must contain `valid_from` and be covered by `to_columns` plus
    `valid_from`. It never proves that exactly one version is valid for each row: overlaps,
    gaps and multiple matches are P2-20's, at execution."""
    source, target = element.sides
    if source is None or target is None:
        return True  # an unknown dataset is a `reference` problem of step 5

    def field_of(ref: object, dataset: _Dataset) -> str | None:
        if not isinstance(ref, str):
            return None
        parts = [normalize(part) for part in ref.split(".")]
        if len(parts) == 2 and index.datasets.get(parts[0]) is not dataset:
            return None
        return parts[-1] if parts[-1] in dataset.fields else None

    columns = frozenset(normalize(name) for name in element.node["to_columns"])
    event = field_of(temporal["event_time"], source)
    start = field_of(temporal["valid_from"], target)
    end = field_of(temporal["valid_to"], target)
    if event is None or start is None or end is None or start == end:
        return False
    if start in columns or end in columns:
        return False
    declared = validities.get(id(target))
    if declared is not None and (
        normalize(declared["from"]) != start or normalize(declared["to"]) != end
    ):
        return False
    if any(key <= columns for key in target.keys):
        return True
    return any(start in key and key <= columns | {start} for key in target.keys)


def _payload_references(element: _Element, payload: Json, index: _Index) -> Iterator[bool]:
    """Whether each name a payload uses to point into the document resolves. Names that live
    in namespaces the payloads declare themselves (entities, conformed dimensions, calendars,
    filters, parameters) are resolved by P2-04, which builds those namespaces."""

    def own(name: object, dataset: _Dataset | None = element.dataset) -> bool:
        return dataset is not None and isinstance(name, str) and normalize(name) in dataset.fields

    def in_dataset(node: Json, *names: str) -> Iterator[bool]:
        if "dataset" in node:
            dataset = index.dataset(node["dataset"])
            yield dataset is not None
            yield from (own(node[name], dataset) for name in names if name in node)

    if element.kind == "model":
        for calendar in payload.get("calendars") or []:
            yield from in_dataset(calendar, "date", "business_day")
        if "parent_child" in payload:
            yield from in_dataset(payload["parent_child"], "ancestor", "descendant")
        for path in payload.get("drill_paths") or []:
            yield from (index.field_ref(level, ()) for level in path["levels"])
        for segment in payload.get("segments") or []:
            if "dimension" in segment:
                yield index.field_ref(segment["dimension"], ())
            if "metric" in segment:
                yield index.metric_ref(segment["metric"])
        for cohort in payload.get("cohorts") or []:
            yield index.field_ref(cohort["event"], ())
        for preaggregation in payload.get("preaggregations") or []:
            if "base_dataset" in preaggregation:
                yield index.dataset(preaggregation["base_dataset"]) is not None
            yield from (index.metric_ref(m) for m in preaggregation.get("metrics") or [])
    elif element.kind == "dataset":
        for key in payload.get("entity_keys") or []:
            yield from (own(name) for name in key["fields"])
        roles = payload.get("time_roles") or {}
        yield from (own(roles[name]) for name in ("event", "snapshot") if name in roles)
        validity = roles.get("validity") or {}
        yield from (own(validity[name]) for name in ("from", "to") if name in validity)
        history = payload.get("history") or {}
        yield from (own(name) for name in history.get("entity_key") or [])
        yield from (
            own(history[name]) for name in ("version", "recorded_at", "deleted") if name in history
        )
        bridge = payload.get("bridge") or {}
        for side in ("left", "right"):
            if side in bridge:
                yield normalize(bridge[side]) in index.relationships
        if "weight" in bridge:
            yield own(bridge["weight"])
    elif element.kind == "metric":
        home = index.dataset(payload["home_dataset"]) if "home_dataset" in payload else None
        if "home_dataset" in payload:
            yield home is not None
        if "allocation" in payload:
            yield index.dataset(payload["allocation"]["bridge"]) is not None
        if "replacement" in (payload.get("deprecation") or {}):
            yield index.metric_ref(payload["deprecation"]["replacement"])
        if "of" in (payload.get("window") or {}):
            yield index.metric_ref(payload["window"]["of"])
        scope = (element.dataset or home,)
        for ref in (payload.get("period_end") or {}).get("order_by") or []:
            yield index.field_ref(ref, scope)
    elif element.kind == "relationship":
        temporal = payload.get("temporal") or {}
        for name in ("event_time", "valid_from", "valid_to"):
            if name in temporal:
                yield index.field_ref(temporal[name], element.sides)


def _dialects(
    document: Json,
    index: _Index,
    problems: _Problems,
    engines: Mapping[str, str],
    vendor_names: Sequence[str],
    derived: Mapping[str, Mapping[str, object]],
) -> tuple[list[SelectedExpression], list[Advisory]]:
    """One expression is selected per field and metric. An entry under the product's own token
    is a product construct, explicit only on a metric whose product entry declares a derived form
    (`over_metrics` or `window`, ossie-extensions v1); there it must state the payload's
    expression (`extension_conflict`). Anywhere else it is never selected (Owner policy of
    2026-11-01). Ossie allows any dialect entry, so what cannot be selected is an advisory."""
    advisories: list[Advisory] = []
    members: list[tuple[Path, Json, str | None]] = []
    shared = {engines.get(normalize(d["name"])) for d in _list(document, "datasets")}
    for at, dataset in enumerate(_list(document, "datasets")):
        engine = engines.get(normalize(dataset["name"]))
        for key in ("fields", "metrics"):
            members += [
                (("datasets", at, key, number), member, engine)
                for number, member in enumerate(_list(dataset, key))
            ]
    # A model metric may span datasets: an engine token applies only when all share one engine.
    model_engine = next(iter(shared)) if len(shared) == 1 else None
    members += [
        (("metrics", at), metric, model_engine)
        for at, metric in enumerate(_list(document, "metrics"))
    ]
    for path, member, _ in members:
        seen: set[str] = set()
        for at, entry in enumerate(_list(member["expression"], "dialects")):
            at_dialect: Path = (*path, "expression", "dialects", at, "dialect")
            if entry["dialect"] in seen:
                advisories.append(Advisory(_pointer(at_dialect), "duplicate_dialect"))
            seen.add(entry["dialect"])
            if entry["dialect"] in vendor_names:
                payload = derived.get(_pointer(path))
                if payload is None:
                    advisories.append(Advisory(_pointer(at_dialect), "undeclared_product_dialect"))
                else:
                    over = cast(Mapping[str, object], payload.get("over_metrics") or {})
                    if over.get("expression", entry["expression"]) != entry["expression"]:
                        # The payload and the dialect entry state one expression twice.
                        at_text: Path = (*path, "expression", "dialects", at, "expression")
                        problems.add(at_text, "extension_conflict")
    problems.raise_if_any()
    selected: list[SelectedExpression] = []
    for path, member, engine in members:
        product = vendor_names if _pointer(path) in derived else ()
        choice = _select(member["expression"], path, (PORTABLE, *product, engine))
        if choice is None:
            advisories.append(Advisory(_pointer(path), "no_executable_expression"))
        else:
            selected.append(choice)
    return selected, advisories


def _pointer(path: Path) -> str:
    return "".join("/" + _escape(part) for part in path)


def _select(expression: Json, path: Path, order: Sequence[str | None]) -> SelectedExpression | None:
    """`sql` first; else the dialect entries in `order`: OSSIE_SQL_2026 (a portable form always
    wins), the product's token on a declared derived metric, then the dataset's engine token.
    Any other entry is kept in the document and never selected here."""
    element = "".join("/" + _escape(part) for part in path)
    if "sql" in expression:
        return SelectedExpression(element, f"{element}/expression/sql", PORTABLE, expression["sql"])
    entries = _list(expression, "dialects")
    for token in order:
        matching = [at for at, entry in enumerate(entries) if entry["dialect"] == token]
        if len(matching) > 1:
            return None  # two entries for one dialect: neither is chosen
        if token is not None and matching:
            (at,) = matching
            pointer = f"{element}/expression/dialects/{at}/expression"
            return SelectedExpression(element, pointer, token, entries[at]["expression"])
    return None


# --- the reader -----------------------------------------------------------------------------


def read(
    content: bytes,
    path: str,
    vendor_names: Sequence[str],
    engines: Mapping[str, str] | None = None,
    registry: Registry | None = None,
) -> OssieModel:
    """Read one document. `path` is project-relative and its suffix chooses the parser.
    `vendor_names` are the product's current vendor name, then its previous ones (`server`
    reads them from `identity.toml`). `engines` maps a dataset's name to its engine's dialect
    token, which must be one the registry entry recognizes."""
    written = format_of(path)
    if not vendor_names:
        raise ValueError("the product's vendor name is required")
    registry = registry or default_registry()
    data, lines = _parse(content, path, written)
    entry = _entry(data, path, lines, registry)
    document = cast(Json, data)
    tokens = entry.dialects(vendor_names)
    engines_by_name = {normalize(name): token for name, token in (engines or {}).items()}
    unknown = sorted(set(engines_by_name.values()) - tokens)
    if unknown:
        raise ValueError(f"engine dialect tokens {unknown} are not in registry entry {entry.id}")
    problems = _Problems(path, lines, _schema_keys(entry.id, registry))
    _schema(document, problems, entry, registry)
    _names(document, problems)
    index = _Index.of(document)
    advisories = _references(document, index, problems)
    found = _extensions(document, index, problems, vendor_names)
    derived = {
        extension.pointer.rsplit("/custom_extensions/", 1)[0]: extension.payload
        for extension in found.product
        if "over_metrics" in extension.payload or "window" in extension.payload
    }
    selected, unselected = _dialects(
        document, index, problems, engines_by_name, vendor_names, derived
    )
    return OssieModel(
        path=path,
        format=written,
        content=bytes(content),
        identity=hashlib.sha256(content).hexdigest(),
        registry_entry=entry.id,
        registry_state=entry.state,
        reader=entry.reader,
        capabilities=entry.capabilities,
        document=cast(Mapping[str, object], freeze(document)),
        product_extensions=tuple(found.product),
        foreign_extensions=tuple(found.foreign),
        expressions=tuple(selected),
        advisories=tuple(advisories + unselected),
        lines=MappingProxyType({_pointer(path): line for path, line in lines.items()}),
    )
