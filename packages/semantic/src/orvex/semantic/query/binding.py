"""Binding: from a `semantic-query` v1 text to one canonical, typed `BoundQuery` (P2-05b, #283).

`bind_query(model, query, *, instant, limits, hidden)` returns a `BoundQuery` or raises
`SemanticError`. `instant` is the timezone-aware query instant supplied by `server`; the host
clock is never read. `limits` holds the project's caps. `hidden` tells which members the
caller may not see (P2-11).

**Steps, in order.** A failing step stops binding and reports all its problems, or the first in
document order for codes that name one thing.
1. Decode strict JSON (a repeated key, `NaN` or `Infinity` is a problem), then validate the
   schema with full-match patterns, then the text bounds the schema cannot state: dates and
   timestamps that exist, offsets within ±14:00, identifiers by Ossie's rule (L86) →
   `QUERY_INVALID`, rule `syntax` or `schema`.
2. Names: every member, role and parameter exists → `MEMBER_NOT_FOUND`, naming only what the
   caller wrote. A member the model did not compile → `UNSUPPORTED_OPERATION`.
3. Kinds: each reference is the kind its slot needs → `QUERY_INVALID`, rule `kind`.
4. Parameters: a value outside its type or `allowed` → `PARAMETER_INVALID`.
5. `as_known_at` on a dataset without `history` → `AS_KNOWN_AT_UNSUPPORTED`.
6. Two metric filters on one dimension and scope depend on each other → `FILTER_CYCLE`.
7. A detail page above the project's cap → `QUERY_BUDGET_EXCEEDED` (`budget: result_rows`).

Relative dates resolve against `instant` in the member's zone, else the model's
`default_time_zone` (else `QUERY_INVALID`, rule `zone`), with the zones of the pinned `tzdata`
package (Owner decision of 2026-11-01, #283): the current period is 0, the range is `count`
whole periods ending at period `offset`, closed-open; `to_date` cuts the last period at the
instant's elapsed point; weeks start on Monday.

**Canonical form.** Identifiers normalized; defaults explicit; sets sorted and deduplicated;
ordered lists kept; exact values normalized (decimals without superfluous zeros, timestamps in
UTC, local timestamps as written); relative dates as an absolute `range` beside the original;
filters on one member in several scopes kept apart. `fingerprint` is `sha256:` over the RFC 8785
form of `{kind: "semantic_query", format: 1, value: canonical}`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from decimal import Decimal
from functools import cache
from importlib.resources import files
from types import MappingProxyType
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from jsonschema import Draft202012Validator, ValidationError
from jsonschema.protocols import Validator
from jsonschema.validators import extend
from orvex.semantic.canonical import fingerprint
from orvex.semantic.compiler.compiled import CompiledModel
from orvex.semantic.compiler.parsing import MemberId
from orvex.semantic.compiler.zones import is_zone
from orvex.semantic.errors import SemanticError
from orvex.semantic.ossie import sql
from orvex.semantic.ossie.extensions import full_match
from orvex.semantic.ossie.identifiers import MAX_IDENTIFIER, is_valid_identifier

SCHEMA_FILE = "semantic-query-v1.json"
MAX_PROBLEMS = 50
SCOPES = ("board", "page", "visual", "cross_filter", "drill")
TEMPORAL = frozenset({"Date", "DateTime", "DateTimeTz"})
_SUB_DAY = frozenset({"minute", "hour"})
_MAX_OFFSET = timedelta(hours=14)

Json = Mapping[str, Any]


@dataclass(frozen=True)
class Limits:
    """The project's caps, supplied by `server`."""

    detail_page_size: int


@dataclass(frozen=True)
class BoundQuery:
    canonical: Mapping[str, Any] = field(repr=False)  # read-only JSON-like values
    fingerprint: str
    instant: datetime
    members: tuple[MemberId, ...]  # every member the query references, sorted
    # Each role as written: (its JSON Pointer, the member it applies to, the normalized role),
    # in document order; outside the fingerprint, for errors that point at a role (P2-15).
    roles: tuple[tuple[str, MemberId, str], ...] = ()


# --- step 1: decode and validate ----------------------------------------------------------------


class _Repeated(ValueError):
    pass


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for key, value in pairs:
        if key in found:
            raise _Repeated(key)
        found[key] = value
    return found


def _constant(name: str) -> object:
    raise ValueError(name)


def _pattern(
    validator: Validator, pattern: str, instance: object, schema: Mapping[str, Any]
) -> Iterator[ValidationError]:
    if isinstance(instance, str) and not _compiled(pattern).search(instance):
        yield ValidationError(f"{instance!r} does not match {pattern!r}")


@cache
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(full_match(pattern))


_FullMatch = extend(Draft202012Validator, {"pattern": _pattern})  # type: ignore[no-untyped-call]


@cache
def _validator() -> Validator:
    schema = json.loads(files("orvex.semantic.query").joinpath(SCHEMA_FILE).read_bytes())
    _FullMatch.check_schema(schema)
    result: Validator = _FullMatch(schema)
    return result


def _pointer(path: Sequence[object]) -> str:
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in path)


def _pointer_order(pointer: str) -> tuple[tuple[int, int | str], ...]:
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in pointer.split("/")[1:])


def _invalid(problems: list[tuple[str, str]]) -> SemanticError:
    ordered = sorted(set(problems), key=lambda found: (_pointer_order(found[0]), found[1]))
    shown = ordered[:MAX_PROBLEMS]
    return SemanticError(
        "QUERY_INVALID",
        {
            "problems": [{"pointer": pointer, "rule": rule} for pointer, rule in shown],
            "problems_omitted": str(len(ordered) - len(shown)),
        },
    )


def _decode(query: str | bytes) -> Json:
    try:
        text = query.decode("utf-8") if isinstance(query, bytes) else query
        document = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    except _Repeated, ValueError, UnicodeDecodeError, RecursionError:
        raise _invalid([("", "syntax")]) from None
    if not isinstance(document, dict):
        raise _invalid([("", "schema")])
    problems = [(_pointer(e.absolute_path), "schema") for e in _validator().iter_errors(document)]
    if problems:
        raise _invalid(problems)
    problems = list(_text_bounds(document, ()))
    if problems:
        raise _invalid(problems)
    return document


_MEMBER_SLOTS = frozenset({"metric", "member", "by", "dimension"})
_MEMBER_LISTS = frozenset({"grain", "per", "members"})
_NAME_SLOTS = frozenset({"role", "context", "calendar", "segment", "hierarchy", "name"})


def _text_bounds(value: object, path: tuple[object, ...]) -> Iterator[tuple[str, str]]:
    """What the schema's shapes cannot say: values that exist, identifiers by Ossie's rule, and
    no lone surrogate anywhere (I-JSON)."""
    if isinstance(value, str):
        if _surrogate(value):
            yield _pointer(path), "schema"
        return
    if isinstance(value, dict):
        kind = value.get("type")
        if set(value) == {"type", "value"} and isinstance(kind, str):
            raw = value["value"]
            if not _exists(kind, raw) or (isinstance(raw, str) and _surrogate(raw)):
                yield _pointer((*path, "value")), "schema"
            return
        for key, item in value.items():
            here = (*path, key)
            if _surrogate(key):
                yield _pointer(here), "schema"
            if key in _MEMBER_SLOTS | _NAME_SLOTS and isinstance(item, str):
                if not _identifiers_valid(item, key in _NAME_SLOTS):
                    yield _pointer(here), "schema"
            elif key in _MEMBER_LISTS | {"apply"} and isinstance(item, list):
                for at, name in enumerate(item):
                    if not _identifiers_valid(str(name), key == "apply"):
                        yield _pointer((*here, at)), "schema"
            elif key == "as_known_at" and not _exists("timestamp", item):
                yield _pointer(here), "schema"
            else:
                yield from _text_bounds(item, here)
    elif isinstance(value, list):
        for at, item in enumerate(value):
            yield from _text_bounds(item, (*path, at))


def _surrogate(text: str) -> bool:
    return any(0xD800 <= ord(c) <= 0xDFFF for c in text)


_TIMESTAMP = re.compile(
    r"(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})T(?P<time>[0-9]{2}:[0-9]{2}:[0-9]{2})"
    r"(?:\.(?P<fraction>[0-9]{1,9}))?(?P<offset>Z|[+-][0-9]{2}:[0-9]{2})?"
)


def _exists(kind: str, value: object) -> bool:
    """A date or timestamp that exists, an hour below 24, an offset within ±14:00, an instant
    that has a UTC form."""
    text = str(value)
    try:
        if kind == "date":
            date.fromisoformat(text)
        elif kind in ("timestamp", "local_timestamp"):
            matched = _TIMESTAMP.fullmatch(text)
            if matched is None or matched.group("time").startswith("24"):
                return False
            moment = datetime.fromisoformat(f"{matched.group('date')}T{matched.group('time')}")
            offset = matched.group("offset")
            if offset is not None and offset != "Z":
                aware = datetime.fromisoformat(f"{moment.isoformat()}{offset}")
                if abs(aware.utcoffset() or timedelta()) > _MAX_OFFSET:
                    return False
                aware.astimezone(UTC)
    except ValueError, OverflowError:
        return False
    return True


def _split(reference: str) -> list[str] | None:
    """The identifiers of a reference as written, or None when it is not one or two."""
    parts = re.findall(r'"(?:[^"]|"")*"|[^."]+', reference)
    if ".".join(parts) != reference or not 1 <= len(parts) <= 2:
        return None
    return parts


def _identifiers_valid(reference: str, single: bool) -> bool:
    parts = _split(reference)
    if parts is None or (single and len(parts) != 1):
        return False
    return all(
        is_valid_identifier(part) and len(sql.normalize(part)) <= MAX_IDENTIFIER for part in parts
    )


# --- the model's names --------------------------------------------------------------------------


def _items(node: object, key: str) -> list[Json]:
    value = node.get(key) if isinstance(node, Mapping) else None
    return [i for i in value if isinstance(i, Mapping)] if isinstance(value, tuple) else []


class _Model:
    def __init__(self, compiled: CompiledModel) -> None:
        self.compiled = compiled
        ossie = compiled.ossie
        payloads = {
            e.pointer.rsplit("/custom_extensions/", 1)[0]: e.payload
            for e in ossie.product_extensions
        }
        model = payloads.get("", {})
        parsed = compiled.typed.parsed
        self.members: set[MemberId] = set(parsed.members) | set(parsed.excluded)
        document = ossie.document
        self.relationships = {
            sql.normalize(str(r["name"])) for r in _items(document, "relationships")
        }
        self.datasets: dict[str, Json] = {}
        self.dataset_names: dict[str, str] = {}
        self.keys: dict[str, list[str]] = {}
        for at, dataset in enumerate(_items(document, "datasets")):
            name = sql.normalize(str(dataset["name"]))
            self.datasets[name] = payloads.get(f"/datasets/{at}", {})
            self.dataset_names[name] = str(dataset["name"])
            key = dataset.get("primary_key")
            self.keys[name] = [sql.normalize(str(k)) for k in key] if isinstance(key, tuple) else []
        self.calendars = {sql.normalize(str(c["name"])) for c in _items(model, "calendars")}
        self.segments = {sql.normalize(str(c["name"])) for c in _items(model, "segments")}
        self.items = {
            sql.normalize(str(item["name"]))
            for group in _items(model, "calculation_groups")
            for item in _items(group, "items")
        }
        hierarchy = model.get("parent_child")
        self.hierarchies = (
            {sql.normalize(str(hierarchy["dataset"]))} if isinstance(hierarchy, Mapping) else set()
        )
        self.parameters = {sql.normalize(str(p["name"])): p for p in _items(model, "parameters")}
        zone = model.get("default_time_zone")
        self.default_zone = str(zone) if zone is not None else None

    def resolve(self, reference: str) -> MemberId | None:
        """A member as the expression language names it: `metric`, `dataset.field` or
        `dataset.metric`; None for anything else."""
        written = _split(reference)
        if written is None:
            return None
        parts = [sql.normalize(part) for part in written]
        if len(parts) == 1:
            found = MemberId("metric", None, parts[0])
            return found if found in self.members else None
        for kind in cast(tuple[Literal["field", "metric"], ...], ("field", "metric")):
            member = MemberId(kind, parts[0], parts[1])
            if member in self.members:
                return member
        return None

    def datatype(self, member: MemberId) -> str | None:
        return self.compiled.require(member).typed.type.logical.datatype

    def zone(self, member: MemberId) -> str | None:
        return self.compiled.require(member).typed.type.logical.zone or self.default_zone

    def datasets_of(self, member: MemberId) -> set[str]:
        compiled = self.compiled.require(member)
        found = {f.dataset for f in compiled.parsed.lineage if f.dataset is not None}
        if member.dataset is not None:
            found.add(member.dataset)
        return found


# --- references ---------------------------------------------------------------------------------

Slot = str  # "metric", "row", "temporal", "metric_or_row"


@dataclass(frozen=True)
class _Reference:
    pointer: str
    text: str
    slot: Slot


def _references(query: Json) -> Iterator[_Reference]:
    """Every member reference with the kind its slot needs, in document order."""
    for at, entry in enumerate(query.get("metrics", ())):
        yield _Reference(f"/metrics/{at}/metric", entry["metric"], "metric")
    for at, entry in enumerate(query.get("group_by", ())):
        slot = "temporal" if "bucket" in entry else "row"
        yield _Reference(f"/group_by/{at}/member", entry["member"], slot)
    for at, entry in enumerate(query.get("fields", ())):
        yield _Reference(f"/fields/{at}/member", entry["member"], "row")
    for pointer, entry in _all_filters(query):
        temporal = entry["predicate"]["op"] == "relative_date"
        yield _Reference(f"{pointer}/member", entry["member"], "temporal" if temporal else "row")
    for at, entry in enumerate(query.get("metric_filters", ())):
        here = f"/metric_filters/{at}"
        yield _Reference(f"{here}/metric", entry["metric"], "metric")
        yield _Reference(f"{here}/dimension", entry["dimension"], "row")
        for number, member in enumerate(entry.get("grain", ())):
            yield _Reference(f"{here}/grain/{number}", member, "row")
    top = query.get("top_n")
    if top is not None:
        yield _Reference("/top_n/by", top["by"], "metric")
        for number, member in enumerate(top.get("per", ())):
            yield _Reference(f"/top_n/per/{number}", member, "row")
    order_slot = "metric_or_row" if query["mode"] == "aggregate" else "row"
    for at, entry in enumerate(query.get("order", ())):
        yield _Reference(f"/order/{at}/by", entry["by"], order_slot)
    for at, entry in enumerate(query.get("context_modifiers", ())):
        for number, member in enumerate(entry["members"]):
            yield _Reference(f"/context_modifiers/{at}/members/{number}", member, "row")


def _roles(query: Json) -> Iterator[tuple[str, str]]:
    for key in ("group_by", "fields"):
        for at, entry in enumerate(query.get(key, ())):
            if "role" in entry:
                yield f"/{key}/{at}/role", entry["role"]
    for pointer, entry in _all_filters(query):
        if "role" in entry:
            yield f"{pointer}/role", entry["role"]


def _all_filters(query: Json) -> Iterator[tuple[str, Json]]:
    for at, entry in enumerate(query.get("filters", ())):
        yield f"/filters/{at}", entry
    for at, context in enumerate(query.get("contexts", ())):
        for number, entry in enumerate(context["filters"]):
            yield f"/contexts/{at}/filters/{number}", entry


# --- binding ------------------------------------------------------------------------------------


def bind_query(
    model: CompiledModel,
    query: str | bytes,
    *,
    instant: datetime,
    limits: Limits,
    hidden: Callable[[MemberId], bool],
) -> BoundQuery:
    """`hidden` tells which members the caller may not see (P2-11, #310), and is required: each
    is refused as `MEMBER_NOT_FOUND` with the caller's own text, exactly like a member that
    does not exist, before any other check can say more about it."""
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("the query instant must be timezone-aware")
    document = _decode(query)
    names = _Model(model)
    resolved = _resolve_names(document, names, hidden)
    _check_kinds(document, names, resolved)
    windows = _relative_windows(document, names, resolved, instant)
    parameters = _parameters(document, names)
    _check_as_known_at(document, names, resolved)
    binder = _Canonical(names, resolved, windows)
    canonical = binder.query(document, parameters)
    _check_metric_filters(document, binder)
    _check_limits(document, limits)
    return BoundQuery(
        canonical=cast(Mapping[str, Any], _frozen(canonical)),
        fingerprint=fingerprint("semantic_query", 1, canonical),
        instant=instant,
        members=tuple(sorted(set(resolved.values()))),
        roles=tuple(
            (pointer, resolved[pointer.rsplit("/", 1)[0] + "/member"], sql.normalize(role))
            for pointer, role in _roles(document)
        ),
    )


def _resolve_names(
    document: Json, names: _Model, hidden: Callable[[MemberId], bool]
) -> dict[str, MemberId]:
    resolved: dict[str, MemberId] = {}
    for reference in _references(document):
        member = names.resolve(reference.text)
        if member is None or hidden(member):
            raise SemanticError("MEMBER_NOT_FOUND", {"member": reference.text})
        names.compiled.require(member)  # a member the model did not compile is refused
        resolved[reference.pointer] = member
    for _, role in _roles(document):
        if sql.normalize(role) not in names.relationships:
            raise SemanticError("MEMBER_NOT_FOUND", {"member": role})
    for entry in document.get("parameters", ()):
        if sql.normalize(entry["name"]) not in names.parameters:
            raise SemanticError("MEMBER_NOT_FOUND", {"member": entry["name"]})
    return resolved


# A value's type must be one the member's type admits (#283: exact values, no implicit cast).
_VALUE_TYPES: Mapping[str, frozenset[str]] = {
    "String": frozenset({"text"}),
    "Integer": frozenset({"integer", "decimal"}),
    "Decimal": frozenset({"integer", "decimal"}),
    "Float": frozenset({"integer", "decimal"}),
    "Boolean": frozenset({"boolean"}),
    "Date": frozenset({"date"}),
    "DateTime": frozenset({"local_timestamp"}),
    "DateTimeTz": frozenset({"timestamp"}),
}


def _filter_values(predicate: Json, at: str) -> Iterator[tuple[str, Json]]:
    for key in ("values", "nodes"):
        for number, value in enumerate(predicate.get(key, ())):
            yield f"{at}/{key}/{number}", value
    for key in ("gte", "gt", "lte", "lt"):
        if key in predicate:
            yield f"{at}/{key}", predicate[key]


def _check_kinds(document: Json, names: _Model, resolved: Mapping[str, MemberId]) -> None:
    problems: list[tuple[str, str]] = []
    for reference in _references(document):
        member = resolved[reference.pointer]
        if (
            (reference.slot == "metric" and member.kind != "metric")
            or (reference.slot in ("row", "temporal") and member.kind != "field")
            or (reference.slot == "temporal" and names.datatype(member) not in TEMPORAL)
        ):
            problems.append((reference.pointer, "kind"))
    contexts = [sql.normalize(c["name"]) for c in document.get("contexts", ())]
    for at, name in enumerate(contexts):
        if name in contexts[:at]:
            problems.append((f"/contexts/{at}/name", "kind"))  # two contexts, one name
    for at, entry in enumerate(document.get("metrics", ())):
        if "context" in entry and sql.normalize(entry["context"]) not in contexts:
            problems.append((f"/metrics/{at}/context", "kind"))
        for number, item in enumerate(entry.get("apply", ())):
            if sql.normalize(item) not in names.items:
                problems.append((f"/metrics/{at}/apply/{number}", "kind"))
    for at, entry in enumerate(document.get("group_by", ())):
        calendar = entry.get("bucket", {}).get("calendar")
        if calendar is not None and sql.normalize(calendar) not in names.calendars:
            problems.append((f"/group_by/{at}/bucket/calendar", "kind"))
        member = resolved[f"/group_by/{at}/member"]
        unit = entry.get("bucket", {}).get("unit")
        if unit in _SUB_DAY and names.datatype(member) == "Date":
            problems.append((f"/group_by/{at}/bucket/unit", "kind"))
    for pointer, entry in _all_filters(document):
        predicate = entry["predicate"]
        member = resolved[f"{pointer}/member"]
        if member.kind == "field":
            admitted = _VALUE_TYPES.get(str(names.datatype(member)))
            for value_at, value in _filter_values(predicate, f"{pointer}/predicate"):
                if admitted is not None and value["type"] not in admitted:
                    problems.append((value_at, "kind"))
        if predicate["op"] == "in_segment" and (
            sql.normalize(predicate["segment"]) not in names.segments
        ):
            problems.append((f"{pointer}/predicate/segment", "kind"))
        if predicate["op"] == "descendants_of" and (
            sql.normalize(predicate["hierarchy"]) not in names.hierarchies
        ):
            problems.append((f"{pointer}/predicate/hierarchy", "kind"))
        if predicate["op"] == "relative_date":
            if predicate["unit"] in _SUB_DAY and names.datatype(member) == "Date":
                problems.append((f"{pointer}/predicate/unit", "kind"))
            elif names.datatype(member) in TEMPORAL and names.zone(member) is None:
                problems.append((f"{pointer}/predicate", "zone"))
    if problems:
        raise _invalid(problems)


_PARAMETER_TYPES = {
    "number": ("integer", "decimal"),
    "date": ("date",),
    "member": ("text",),
    "metric": ("text",),
}


def _parameters(document: Json, names: _Model) -> list[list[object]]:
    """Every model parameter with its value: the query's, else the declared default. A name given
    twice with two values is refused."""
    given: dict[str, str] = {}
    for entry in document.get("parameters", ()):
        name = sql.normalize(entry["name"])
        declared = names.parameters[name]
        kind = str(declared["type"])
        value = entry["value"]
        text = str(value["value"])
        if value["type"] not in _PARAMETER_TYPES[kind] or not _parameter_ok(kind, text, names):
            raise _parameter_invalid(declared)
        canonical = _canonical_text(kind, text)
        if given.get(name, canonical) != canonical:
            raise _parameter_invalid(declared)
        given[name] = canonical
    bound: list[list[object]] = []
    for name, declared in sorted(names.parameters.items()):
        kind = str(declared["type"])
        text = given.get(name, _canonical_text(kind, str(declared["default"])))
        allowed = declared.get("allowed")
        if isinstance(allowed, tuple) and text not in {
            _canonical_text(kind, str(a)) for a in allowed
        }:
            raise _parameter_invalid(declared)
        bound.append([name, kind, text])
    return bound


def _parameter_invalid(declared: Json) -> SemanticError:
    return SemanticError(
        "PARAMETER_INVALID", {"parameter": str(declared["name"]), "expected": str(declared["type"])}
    )


def _parameter_ok(kind: str, text: str, names: _Model) -> bool:
    if kind == "number":
        return re.fullmatch(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?", text) is not None
    if kind == "date":
        return _exists("date", text)
    member = names.resolve(text)
    return member is not None and (member.kind == "metric") == (kind == "metric")


def _canonical_text(kind: str, text: str) -> str:
    if kind == "number" and re.fullmatch(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?", text):
        return _number(text)
    if kind in ("member", "metric"):
        parts = _split(text)
        return ".".join(sql.normalize(p) for p in parts) if parts else text
    return text


def _check_as_known_at(document: Json, names: _Model, resolved: Mapping[str, MemberId]) -> None:
    if "as_known_at" not in document:
        return
    datasets = sorted({d for member in resolved.values() for d in names.datasets_of(member)})
    for dataset in datasets:
        if "history" not in names.datasets.get(dataset, {}):
            shown = names.dataset_names.get(dataset, dataset)
            raise SemanticError("AS_KNOWN_AT_UNSUPPORTED", {"dataset": shown})


def _check_metric_filters(document: Json, binder: _Canonical) -> None:
    """A metric filter is evaluated in the context of every other filter: two different ones
    on one dimension (the resolved member) and scope each depend on the other (#238 rule 3).
    Identical ones are one filter. The error names the metrics as the caller wrote them."""
    seen: dict[str, tuple[str, str]] = {}
    for at, entry in enumerate(document.get("metric_filters", ())):
        bound = binder.metric_filter(at, entry)
        key = json.dumps([bound[0], bound[2]])
        written = json.dumps(bound, sort_keys=True)
        if key in seen and seen[key][1] != written:
            raise SemanticError("FILTER_CYCLE", {"filters": [seen[key][0], entry["metric"]]})
        seen.setdefault(key, (entry["metric"], written))


def _check_limits(document: Json, limits: Limits) -> None:
    page = document.get("page")
    if page is not None and int(page["size"]) > limits.detail_page_size:
        raise SemanticError(
            "QUERY_BUDGET_EXCEEDED",
            {"budget": "result_rows", "limit": str(limits.detail_page_size)},
        )


# --- exact values -------------------------------------------------------------------------------


def _number(text: str) -> str:
    """The exact text of a number with no superfluous zeros: `1.50` → `1.5`, `-0.0` → `0`.
    Text arithmetic only, so no digit is ever rounded."""
    negative = text.startswith("-")
    whole, _, fraction = text.lstrip("-").partition(".")
    whole = whole.lstrip("0") or "0"
    fraction = fraction.rstrip("0")
    digits = whole + (f".{fraction}" if fraction else "")
    return "0" if digits == "0" else ("-" if negative else "") + digits


def _utc(text: str) -> str:
    """An instant in UTC, its fraction kept to the nanosecond, without trailing zeros."""
    matched = _TIMESTAMP.fullmatch(text)
    assert matched is not None  # checked by the text bounds
    offset = matched.group("offset")
    moment = datetime.fromisoformat(
        f"{matched.group('date')}T{matched.group('time')}"
        + ("+00:00" if offset in (None, "Z") else offset)
    ).astimezone(UTC)
    fraction = (matched.group("fraction") or "").rstrip("0")
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + (f".{fraction}" if fraction else "") + "Z"


def _local(text: str) -> str:
    matched = _TIMESTAMP.fullmatch(text)
    assert matched is not None
    fraction = (matched.group("fraction") or "").rstrip("0")
    return f"{matched.group('date')}T{matched.group('time')}" + (f".{fraction}" if fraction else "")


def _value(value: Json) -> list[object]:
    """A value in canonical form: integers and decimals are one exact `number` (the member's
    type applies the scale); timestamps in UTC; local timestamps as written."""
    kind, raw = value["type"], value["value"]
    if kind in ("integer", "decimal"):
        return ["number", _number(str(raw))]
    if kind == "timestamp":
        return [kind, _utc(str(raw))]
    if kind == "local_timestamp":
        return [kind, _local(str(raw))]
    return [kind, raw]


def _value_order(value: list[object]) -> tuple[object, ...]:
    kind, raw = value
    if kind == "number":
        return (0, Decimal(str(raw)), "")
    if kind == "boolean":
        return (1, bool(raw), "")
    return (2, str(kind), str(raw))


def _values(values: Sequence[Json]) -> list[list[object]]:
    unique: dict[str, list[object]] = {}
    for value in values:
        normalized = _value(value)
        unique[json.dumps(normalized)] = normalized
    return sorted(unique.values(), key=_value_order)


# --- relative dates (Owner decision of 2026-11-01, #283) -----------------------------------------


@cache
def _zone(name: str) -> tzinfo:
    """A zone of the pinned `tzdata` package, never the host's database."""
    if not is_zone(name):
        raise ValueError(name)
    path = files("tzdata").joinpath("zoneinfo", *name.split("/"))
    with path.open("rb") as data:
        return ZoneInfo.from_file(data, key=name)


def _period_start(moment: datetime, unit: str) -> datetime:
    """The start of the period holding `moment`, in its own wall time."""
    if unit == "minute":
        return moment.replace(second=0, microsecond=0)
    if unit == "hour":
        return moment.replace(minute=0, second=0, microsecond=0)
    day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    if unit == "day":
        return day
    if unit == "week":
        return day - timedelta(days=day.weekday())  # ISO weeks start on Monday
    if unit == "month":
        return day.replace(day=1)
    if unit == "quarter":
        return day.replace(month=(day.month - 1) // 3 * 3 + 1, day=1)
    return day.replace(month=1, day=1)


_MONTHS = {"month": 1, "quarter": 3, "year": 12}
_STEPS = {
    "minute": timedelta(minutes=1),
    "hour": timedelta(hours=1),
    "day": timedelta(days=1),
    "week": timedelta(weeks=1),
}


def _shift(start: datetime, unit: str, periods: int) -> datetime:
    if unit in _STEPS:
        return start + _STEPS[unit] * periods
    index = start.year * 12 + start.month - 1 + _MONTHS[unit] * periods
    return start.replace(year=index // 12, month=index % 12 + 1)


def _days_in(year: int, month: int) -> int:
    following = date(year + month // 12, month % 12 + 1, 1)
    return (following - timedelta(days=1)).day


def _cut(last: datetime, current: datetime, now: datetime, unit: str) -> datetime:
    """The point of the last period at the same calendar position as `now` in the current one:
    the same month within the quarter or year, the same day (at most the month's last), the
    same time of day."""
    if unit in _STEPS:
        return last + (now - current)
    month_index = last.month - 1 + (now.month - current.month)
    year, month = last.year + month_index // 12, month_index % 12 + 1
    day = min(now.day, _days_in(year, month))
    return now.replace(year=year, month=month, day=day)


def relative_range(
    instant: datetime, zone: str, unit: str, offset: int, count: int, to_date: bool
) -> tuple[datetime, datetime]:
    """The closed-open range of a relative date: `count` whole periods ending at period
    `offset`, the current one being 0; `to_date` cuts the last period at the instant's calendar
    position. Calendar units step in wall time; minutes and hours step in elapsed time, so a
    daylight-saving change never stretches or empties an hour. Returns aware datetimes in the
    zone."""
    local_zone = _zone(zone)
    now = instant.astimezone(local_zone)
    if unit in _SUB_DAY:
        current = _period_start(now, unit).astimezone(UTC)
        utc_now = instant.astimezone(UTC)
        last = current + _STEPS[unit] * offset
        start = last - _STEPS[unit] * (count - 1)
        end = last + _STEPS[unit]
        if to_date:
            end = min(end, last + (utc_now - current))
        return start.astimezone(local_zone), end.astimezone(local_zone)
    wall = now.replace(tzinfo=None)
    current = _period_start(wall, unit)
    last = _shift(current, unit, offset)
    start = _shift(last, unit, 1 - count)
    end = _shift(last, unit, 1)
    if to_date:
        end = min(end, _cut(last, current, wall, unit))
    return start.replace(tzinfo=local_zone), end.replace(tzinfo=local_zone)


def _relative_windows(
    document: Json, names: _Model, resolved: Mapping[str, MemberId], instant: datetime
) -> dict[str, dict[str, object]]:
    """Each relative date resolved to its absolute range, or `QUERY_INVALID` `schema` when the
    range falls outside the calendar's years."""
    windows: dict[str, dict[str, object]] = {}
    problems: list[tuple[str, str]] = []
    for pointer, entry in _all_filters(document):
        predicate = entry["predicate"]
        if predicate["op"] != "relative_date":
            continue
        member = resolved[f"{pointer}/member"]
        zone = names.zone(member)
        assert zone is not None  # checked with the kinds
        try:
            windows[pointer] = _window(predicate, str(names.datatype(member)), zone, instant)
        except ValueError, OverflowError:
            problems.append((f"{pointer}/predicate", "schema"))
    if problems:
        raise _invalid(problems)
    return windows


def _window(predicate: Json, datatype: str, zone: str, instant: datetime) -> dict[str, object]:
    to_date = bool(predicate.get("to_date", False))
    start, end = relative_range(
        instant, zone, predicate["unit"], int(predicate["offset"]), int(predicate["count"]), to_date
    )
    if datatype == "Date":
        # Whole days: a cut inside a day keeps that day.
        cut_day = end.date() if end.time() == datetime.min.time() else end.date() + timedelta(1)
        low, high = ["date", start.date().isoformat()], ["date", cut_day.isoformat()]
    elif datatype == "DateTime":
        low = ["local_timestamp", _local(start.replace(tzinfo=None).isoformat())]
        high = ["local_timestamp", _local(end.replace(tzinfo=None).isoformat())]
    else:
        low = ["timestamp", _utc(start.astimezone(UTC).isoformat().replace("+00:00", "Z"))]
        high = ["timestamp", _utc(end.astimezone(UTC).isoformat().replace("+00:00", "Z"))]
    original = {
        "unit": predicate["unit"],
        "offset": _number(predicate["offset"]),
        "count": predicate["count"],
        "to_date": to_date,
    }
    return {"op": "range", "gte": low, "lt": high, "relative_date": original, "zone": zone}


# --- the canonical form -------------------------------------------------------------------------


def _unique_sorted(items: Sequence[object]) -> list[object]:
    unique = {json.dumps(item, sort_keys=True): item for item in items}
    return [unique[key] for key in sorted(unique)]


class _Canonical:
    def __init__(
        self,
        names: _Model,
        resolved: Mapping[str, MemberId],
        windows: Mapping[str, dict[str, object]],
    ) -> None:
        self.names = names
        self.resolved = resolved
        self.windows = windows

    def member(self, pointer: str) -> list[str]:
        member = self.resolved[pointer]
        return [member.kind, member.dataset or "", member.name]

    def query(self, document: Json, parameters: list[list[object]]) -> dict[str, object]:
        mode = document["mode"]
        found: dict[str, object] = {"kind": "semantic_query", "version": 1, "mode": mode}
        found["filters"] = self.filters(document.get("filters", ()), "/filters")
        found["parameters"] = parameters
        found["as_known_at"] = _utc(document["as_known_at"]) if "as_known_at" in document else None
        found["context_modifiers"] = _unique_sorted(
            [
                [
                    entry["op"],
                    SCOPES.index(entry["scope"]),
                    _unique_sorted(
                        [
                            self.member(f"/context_modifiers/{at}/members/{n}")
                            for n in range(len(entry["members"]))
                        ]
                    ),
                ]
                for at, entry in enumerate(document.get("context_modifiers", ()))
            ]
        )
        found["order"] = [
            [self.member(f"/order/{at}/by"), e.get("direction", "asc"), e.get("nulls", "last")]
            for at, e in enumerate(document.get("order", ()))
        ]
        if mode == "aggregate":
            found |= self.aggregate(document)
        else:
            found |= self.detail(document)
        return found

    def aggregate(self, document: Json) -> dict[str, object]:
        return {
            "metrics": [
                [
                    self.member(f"/metrics/{at}/metric"),
                    sql.normalize(e["context"]) if "context" in e else None,
                    [sql.normalize(item) for item in e.get("apply", ())],
                ]
                for at, e in enumerate(document["metrics"])
            ],
            "group_by": [
                [
                    self.member(f"/group_by/{at}/member"),
                    sql.normalize(e["role"]) if "role" in e else None,
                    [
                        e["bucket"]["unit"],
                        sql.normalize(e["bucket"]["calendar"])
                        if "calendar" in e["bucket"]
                        else None,
                    ]
                    if "bucket" in e
                    else None,
                ]
                for at, e in enumerate(document.get("group_by", ()))
            ],
            "metric_filters": _unique_sorted(
                [
                    self.metric_filter(at, e)
                    for at, e in enumerate(document.get("metric_filters", ()))
                ]
            ),
            "contexts": _unique_sorted(
                [
                    [
                        sql.normalize(c["name"]),
                        self.filters(c["filters"], f"/contexts/{at}/filters"),
                    ]
                    for at, c in enumerate(document.get("contexts", ()))
                ]
            ),
            "coverage": document.get("coverage"),
            "limit": document.get("limit"),
            "top_n": self.top_n(document.get("top_n")),
        } | (  # only when asked: `none` and absent are the same query, with the same fingerprint
            {"totals": document["totals"]} if document.get("totals", "none") != "none" else {}
        )

    def metric_filter(self, at: int, entry: Json) -> list[object]:
        here = f"/metric_filters/{at}"
        grain = [self.member(f"{here}/grain/{n}") for n in range(len(entry.get("grain", ())))]
        grain = grain or [self.member(f"{here}/dimension")]
        condition = entry["condition"]
        bound = (
            [condition["op"], _value(condition["value"])]
            if "value" in condition
            else [condition["op"], condition["n"]]
        )
        return [
            SCOPES.index(entry["scope"]),
            self.member(f"{here}/metric"),
            self.member(f"{here}/dimension"),
            _unique_sorted(grain),
            bound,
        ]

    def top_n(self, top: Json | None) -> object:
        if top is None:
            return None
        per = [self.member(f"/top_n/per/{n}") for n in range(len(top.get("per", ())))]
        return {
            "by": self.member("/top_n/by"),
            "n": top["n"],
            "per": _unique_sorted(per),
            "others": top["others"],
            "ties": "member_value_ascending",
        }

    def detail(self, document: Json) -> dict[str, object]:
        fields_ = [
            [self.member(f"/fields/{at}/member"), sql.normalize(e["role"]) if "role" in e else None]
            for at, e in enumerate(document["fields"])
        ]
        # The engine appends each read dataset's declared key as the final tie-breaker.
        ordered = [self.member(f"/order/{at}/by") for at in range(len(document["order"]))]
        tie_breaker: list[list[str]] = []
        for at in range(len(document["fields"])):
            dataset = self.resolved[f"/fields/{at}/member"].dataset or ""
            for key in self.names.keys[dataset]:
                candidate = ["field", dataset, key]
                if candidate not in ordered and candidate not in tie_breaker:
                    tie_breaker.append(candidate)
        page = document["page"]
        return {
            "fields": fields_,
            "tie_breaker": tie_breaker,
            "page": {"size": page["size"], "after": page.get("after")},
        }

    def filters(self, filters: Sequence[Json], base: str) -> list[dict[str, Any]]:
        """Sorted by scope, then member, then predicate (#283 §2); duplicates merged."""
        bound = [self.filter(entry, f"{base}/{at}") for at, entry in enumerate(filters)]
        unique = {json.dumps(entry, sort_keys=True): entry for entry in bound}
        return sorted(
            unique.values(),
            key=lambda e: (
                e["scope"],
                json.dumps(e["member"]),
                json.dumps(e["role"]),
                json.dumps(e["predicate"], sort_keys=True),
            ),
        )

    def filter(self, entry: Json, pointer: str) -> dict[str, Any]:
        return {
            "scope": SCOPES.index(entry["scope"]),
            "member": self.member(f"{pointer}/member"),
            "role": sql.normalize(entry["role"]) if "role" in entry else None,
            "predicate": self.predicate(entry["predicate"], pointer),
        }

    def predicate(self, predicate: Json, pointer: str) -> dict[str, object]:
        op = predicate["op"]
        if op == "in":
            include = bool(predicate.get("include_null", False))
            return {"op": op, "values": _values(predicate["values"]), "include_null": include}
        if op == "not_in":
            # NULLs stay unless excluded (#238 rule 6); never a bare SQL NOT IN.
            exclude = bool(predicate.get("exclude_null", False))
            return {"op": op, "values": _values(predicate["values"]), "exclude_null": exclude}
        if op == "range":
            bounds = [b for b in ("gte", "gt", "lte", "lt") if b in predicate]
            return {"op": op} | {bound: _value(predicate[bound]) for bound in bounds}
        if op == "relative_date":
            return self.windows[pointer]
        if op == "in_segment":
            return {"op": op, "segment": sql.normalize(predicate["segment"])}
        if op == "descendants_of":
            return {
                "op": op,
                "hierarchy": sql.normalize(predicate["hierarchy"]),
                "nodes": _values(predicate["nodes"]),
            }
        return {"op": op}


def _frozen(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({k: _frozen(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_frozen(v) for v in value)
    return value
