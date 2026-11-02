"""Binding: from a `semantic-query` v1 text to one canonical, typed `BoundQuery` (P2-05b, #283).

`bind_query(model, query, *, instant, limits)` returns a `BoundQuery` or raises
`SemanticError`. `instant` is the timezone-aware query instant supplied by `server`; the host
clock is never read. `limits` holds the project's caps.

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
from collections.abc import Iterator, Mapping, Sequence
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


def _invalid(problems: list[tuple[str, str]]) -> SemanticError:
    ordered = sorted(set(problems))
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
    """What the schema's shapes cannot say: values that exist, identifiers by Ossie's rule."""
    if isinstance(value, dict):
        kind = value.get("type")
        if set(value) == {"type", "value"} and isinstance(kind, str):
            if not _exists(kind, value["value"]):
                yield _pointer((*path, "value")), "schema"
            return
        for key, item in value.items():
            here = (*path, key)
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


def _exists(kind: str, value: object) -> bool:
    text = str(value)
    try:
        if kind == "date":
            date.fromisoformat(text)
        elif kind in ("timestamp", "local_timestamp"):
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            offset = parsed.utcoffset()
            if offset is not None and abs(offset) > _MAX_OFFSET:
                return False
    except ValueError:
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
        self.keys: dict[str, list[str]] = {}
        for at, dataset in enumerate(_items(document, "datasets")):
            name = sql.normalize(str(dataset["name"]))
            self.datasets[name] = payloads.get(f"/datasets/{at}", {})
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
        `dataset.metric`."""
        parts = [sql.normalize(part) for part in _split(reference) or []]
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
    yield from _filter_references(query.get("filters", ()), "/filters")
    for at, context in enumerate(query.get("contexts", ())):
        yield from _filter_references(context["filters"], f"/contexts/{at}/filters")
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


def _filter_references(filters: Sequence[Json], at: str) -> Iterator[_Reference]:
    for number, entry in enumerate(filters):
        temporal = entry["predicate"]["op"] == "relative_date"
        slot = "temporal" if temporal else "row"
        yield _Reference(f"{at}/{number}/member", entry["member"], slot)


def _roles(query: Json) -> Iterator[tuple[str, str]]:
    for key in ("group_by", "fields"):
        for at, entry in enumerate(query.get(key, ())):
            if "role" in entry:
                yield f"/{key}/{at}/role", entry["role"]
    filters = [("/filters", query.get("filters", ()))] + [
        (f"/contexts/{at}/filters", c["filters"]) for at, c in enumerate(query.get("contexts", ()))
    ]
    for base, entries in filters:
        for at, entry in enumerate(entries):
            if "role" in entry:
                yield f"{base}/{at}/role", entry["role"]


def _all_filters(query: Json) -> Iterator[tuple[str, Json]]:
    for at, entry in enumerate(query.get("filters", ())):
        yield f"/filters/{at}", entry
    for at, context in enumerate(query.get("contexts", ())):
        for number, entry in enumerate(context["filters"]):
            yield f"/contexts/{at}/filters/{number}", entry


# --- binding ------------------------------------------------------------------------------------


def bind_query(
    model: CompiledModel, query: str | bytes, *, instant: datetime, limits: Limits
) -> BoundQuery:
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("the query instant must be timezone-aware")
    document = _decode(query)
    names = _Model(model)
    resolved = _resolve_names(document, names)
    _check_kinds(document, names, resolved)
    parameters = _parameters(document, names)
    _check_as_known_at(document, names, resolved)
    _check_metric_filters(document)
    _check_limits(document, limits)
    binder = _Canonical(names, resolved, instant)
    canonical = binder.query(document, parameters)
    return BoundQuery(
        canonical=cast(Mapping[str, Any], _frozen(canonical)),
        fingerprint=fingerprint("semantic_query", 1, canonical),
        instant=instant,
        members=tuple(sorted(set(resolved.values()))),
    )


def _resolve_names(document: Json, names: _Model) -> dict[str, MemberId]:
    resolved: dict[str, MemberId] = {}
    for reference in _references(document):
        member = names.resolve(reference.text)
        if member is None:
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
    contexts = {sql.normalize(c["name"]) for c in document.get("contexts", ())}
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
        if predicate["op"] == "in_segment" and (
            sql.normalize(predicate["segment"]) not in names.segments
        ):
            problems.append((f"{pointer}/predicate/segment", "kind"))
        if predicate["op"] == "descendants_of" and (
            sql.normalize(predicate["hierarchy"]) not in names.hierarchies
        ):
            problems.append((f"{pointer}/predicate/hierarchy", "kind"))
        if predicate["op"] == "relative_date":
            member = resolved[f"{pointer}/member"]
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
    """Every model parameter with its value: the query's, else the declared default."""
    given = {sql.normalize(e["name"]): e["value"] for e in document.get("parameters", ())}
    bound: list[list[object]] = []
    for name, declared in sorted(names.parameters.items()):
        kind = str(declared["type"])
        if name in given:
            value = given[name]
            text = str(value["value"])
            if value["type"] not in _PARAMETER_TYPES[kind] or not _parameter_ok(kind, text, names):
                raise SemanticError(
                    "PARAMETER_INVALID", {"parameter": str(declared["name"]), "expected": kind}
                )
        else:
            text = str(declared["default"])
        allowed = declared.get("allowed")
        if isinstance(allowed, tuple) and _canonical_text(kind, text) not in {
            _canonical_text(kind, str(a)) for a in allowed
        }:
            raise SemanticError(
                "PARAMETER_INVALID", {"parameter": str(declared["name"]), "expected": kind}
            )
        bound.append([name, kind, _canonical_text(kind, text)])
    return bound


def _parameter_ok(kind: str, text: str, names: _Model) -> bool:
    if kind in ("member", "metric"):
        member = names.resolve(text)
        return member is not None and (member.kind == "metric") == (kind == "metric")
    return True


def _canonical_text(kind: str, text: str) -> str:
    if kind == "number":
        try:
            return _decimal(text)
        except ArithmeticError:
            return text
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
            raise SemanticError("AS_KNOWN_AT_UNSUPPORTED", {"dataset": dataset})


def _check_metric_filters(document: Json) -> None:
    """A metric filter is evaluated in the context of every other filter: two on one dimension
    and scope each depend on the other (#238 rule 3)."""
    seen: dict[tuple[str, str], str] = {}
    for entry in document.get("metric_filters", ()):
        key = (entry["scope"], sql.normalize(entry["dimension"]))
        if key in seen:
            raise SemanticError("FILTER_CYCLE", {"filters": [seen[key], entry["metric"]]})
        seen[key] = entry["metric"]


def _check_limits(document: Json, limits: Limits) -> None:
    page = document.get("page")
    if page is not None and int(page["size"]) > limits.detail_page_size:
        raise SemanticError(
            "QUERY_BUDGET_EXCEEDED",
            {"budget": "result_rows", "limit": str(limits.detail_page_size)},
        )


# --- the canonical form -------------------------------------------------------------------------


def _decimal(text: str) -> str:
    value = Decimal(text)
    if value == 0:
        return "0"
    normalized = format(value.normalize(), "f")
    return normalized


def _utc(text: str) -> str:
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
    return parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _value(value: Json) -> list[object]:
    kind, raw = value["type"], value["value"]
    if kind in ("integer", "decimal"):
        return [kind, _decimal(str(raw))]
    if kind == "timestamp":
        return [kind, _utc(str(raw))]
    return [kind, raw]


def _value_order(value: list[object]) -> tuple[object, ...]:
    kind, raw = value
    if kind in ("integer", "decimal"):
        return (0, Decimal(str(raw)), str(kind))
    if kind == "boolean":
        return (1, bool(raw), "")
    return (2, str(kind), str(raw))


def _values(values: Sequence[Json]) -> list[list[object]]:
    unique: dict[str, list[object]] = {}
    for value in values:
        normalized = _value(value)
        unique[json.dumps(normalized)] = normalized
    return sorted(unique.values(), key=_value_order)


@cache
def _zone(name: str) -> tzinfo:
    """A zone of the pinned `tzdata` package, never the host's database."""
    if not is_zone(name):
        raise ValueError(name)
    path = files("tzdata").joinpath("zoneinfo", *name.split("/"))
    with path.open("rb") as data:
        return ZoneInfo.from_file(data, key=name)


def _period_start(moment: datetime, unit: str) -> datetime:
    """The start of the period holding `moment` (a naive local time)."""
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


def _shift(start: datetime, unit: str, periods: int) -> datetime:
    if unit in ("minute", "hour", "day", "week"):
        step = {"minute": timedelta(minutes=1), "hour": timedelta(hours=1)}.get(
            unit, timedelta(days=1 if unit == "day" else 7)
        )
        return start + step * periods
    months = {"month": 1, "quarter": 3, "year": 12}[unit] * periods
    index = start.year * 12 + start.month - 1 + months
    return start.replace(year=index // 12, month=index % 12 + 1)


def relative_range(
    instant: datetime, zone: str, unit: str, offset: int, count: int, to_date: bool
) -> tuple[datetime, datetime]:
    """The closed-open local range of a relative date (Owner decision of 2026-11-01, #283):
    `count` whole periods ending at period `offset`, the current one being 0; `to_date` cuts the
    last period at the instant's elapsed point. Naive local wall times."""
    local = instant.astimezone(_zone(zone)).replace(tzinfo=None)
    current = _period_start(local, unit)
    last = _shift(current, unit, offset)
    start = _shift(last, unit, 1 - count)
    end = _shift(last, unit, 1)
    if to_date:
        end = min(end, last + (local - current))
    return start, end


class _Canonical:
    def __init__(self, names: _Model, resolved: Mapping[str, MemberId], instant: datetime) -> None:
        self.names = names
        self.resolved = resolved
        self.instant = instant

    def member(self, pointer: str) -> list[str]:
        member = self.resolved[pointer]
        return [member.kind, member.dataset or "", member.name]

    def query(self, document: Json, parameters: list[list[object]]) -> dict[str, object]:
        mode = document["mode"]
        found: dict[str, object] = {"kind": "semantic_query", "version": 1, "mode": mode}
        filters = self.filters(document.get("filters", ()), "/filters")
        found["filters"] = filters
        found["parameters"] = parameters
        found["as_known_at"] = _utc(document["as_known_at"]) if "as_known_at" in document else None
        found["context_modifiers"] = sorted(
            (
                [
                    entry["op"],
                    SCOPES.index(entry["scope"]),
                    sorted(
                        {
                            json.dumps(self.member(f"/context_modifiers/{at}/members/{n}"))
                            for n in range(len(entry["members"]))
                        }
                    ),
                ]
                for at, entry in enumerate(document.get("context_modifiers", ()))
            ),
            key=json.dumps,
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
        found: dict[str, object] = {
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
            "metric_filters": sorted(
                (
                    self.metric_filter(at, e)
                    for at, e in enumerate(document.get("metric_filters", ()))
                ),
                key=json.dumps,
            ),
            "contexts": sorted(
                (
                    [
                        sql.normalize(c["name"]),
                        self.filters(c["filters"], f"/contexts/{at}/filters"),
                    ]
                    for at, c in enumerate(document.get("contexts", ()))
                ),
                key=json.dumps,
            ),
            "coverage": document.get("coverage"),
            "limit": document.get("limit"),
            "top_n": self.top_n(document.get("top_n")),
        }
        return found

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
            sorted(grain, key=json.dumps),
            bound,
        ]

    def top_n(self, top: Json | None) -> object:
        if top is None:
            return None
        per = [self.member(f"/top_n/per/{n}") for n in range(len(top.get("per", ())))]
        return {
            "by": self.member("/top_n/by"),
            "n": top["n"],
            "per": sorted(per, key=json.dumps),
            "others": top["others"],
            "ties": "member_value_ascending",
        }

    def detail(self, document: Json) -> dict[str, object]:
        fields_ = [
            [self.member(f"/fields/{at}/member"), sql.normalize(e["role"]) if "role" in e else None]
            for at, e in enumerate(document["fields"])
        ]
        # The engine appends each read dataset's declared key as the final tie-breaker.
        ordered = {
            json.dumps(self.member(f"/order/{at}/by")) for at in range(len(document["order"]))
        }
        tie_breaker: list[list[str]] = []
        for field_entry in fields_:
            dataset = cast(list[str], field_entry[0])[1]
            for key in self.names.keys.get(dataset, []):
                candidate = ["field", dataset, key]
                if json.dumps(candidate) not in ordered and candidate not in tie_breaker:
                    tie_breaker.append(candidate)
        page = document["page"]
        return {
            "fields": fields_,
            "tie_breaker": tie_breaker,
            "page": {"size": page["size"], "after": page.get("after")},
        }

    def filters(self, filters: Sequence[Json], base: str) -> list[object]:
        bound = [self.filter(entry, f"{base}/{at}") for at, entry in enumerate(filters)]
        unique = {json.dumps(entry, sort_keys=True): entry for entry in bound}
        return sorted(unique.values(), key=lambda e: json.dumps(e, sort_keys=True))

    def filter(self, entry: Json, pointer: str) -> dict[str, object]:
        member = self.resolved[f"{pointer}/member"]
        return {
            "scope": SCOPES.index(entry["scope"]),
            "member": self.member(f"{pointer}/member"),
            "role": sql.normalize(entry["role"]) if "role" in entry else None,
            "predicate": self.predicate(entry["predicate"], member),
        }

    def predicate(self, predicate: Json, member: MemberId) -> dict[str, object]:
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
            return self.relative(predicate, member)
        if op == "in_segment":
            return {"op": op, "segment": sql.normalize(predicate["segment"])}
        if op == "descendants_of":
            return {
                "op": op,
                "hierarchy": sql.normalize(predicate["hierarchy"]),
                "nodes": _values(predicate["nodes"]),
            }
        return {"op": op}

    def relative(self, predicate: Json, member: MemberId) -> dict[str, object]:
        zone = self.names.zone(member)
        assert zone is not None  # checked with the kinds
        datatype = self.names.datatype(member)
        original = {
            "unit": predicate["unit"],
            "offset": _decimal(predicate["offset"]),
            "count": predicate["count"],
            "to_date": predicate.get("to_date", False),
        }
        start, end = relative_range(
            self.instant,
            zone,
            predicate["unit"],
            int(predicate["offset"]),
            int(predicate["count"]),
            bool(original["to_date"]),
        )
        if datatype == "Date":
            # Whole days: a cut inside a day keeps that day.
            last = end.date() if end.time() == datetime.min.time() else end.date() + timedelta(1)
            low, high = ["date", start.date().isoformat()], ["date", last.isoformat()]
        elif datatype == "DateTime":
            low = ["local_timestamp", start.isoformat(timespec="microseconds")]
            high = ["local_timestamp", end.isoformat(timespec="microseconds")]
        else:
            local = _zone(zone)
            low = ["timestamp", _utc(start.replace(tzinfo=local).isoformat())]
            high = ["timestamp", _utc(end.replace(tzinfo=local).isoformat())]
        return {"op": "range", "gte": low, "lt": high, "relative_date": original, "zone": zone}


def _frozen(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({k: _frozen(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_frozen(v) for v in value)
    return value
