"""Evidence verification (P2-26, #351): what the model declares about the data, checked on
one snapshot of the lake, and what a question's plan relies on.

Each declared piece of evidence is checked by one read-only statement (`adapters.duckdb`) on
one snapshot, every table read whole, with no row policy: a check is about the data, and its
outcome is counts, never a value. The outcome is a record bound to the exact snapshot it read
and to the declaration it checked (a fingerprint of the model elements the check depends on):
on any other snapshot, or after the declaration changed, the evidence is `declared` again.

What each check counts as violations:
- `unique`, a dataset's primary key: key values more than one row shares;
- `non_overlap`, a dataset's validity: pairs of rows of one entity whose closed-open intervals
  overlap;
- `cardinality`, a relationship's `to` side (and `from` for `one_to_one`): key values more than
  one row shares that a row of the other side refers to; for a temporal relationship,
  overlapping versions of a referenced key;
- `referential_integrity`, a relationship's `from` side: distinct keys, none `NULL`, with no
  `to` row;
- `consistent`, an aggregate table that names its `base_dataset`: the groups it is missing,
  has in addition, or stores differently.

`append_only` compares two snapshots, which one statement on one snapshot cannot read: it is
never checked here, so it stays `declared`, never `verified` on a guess.

A question relies on the evidence its answer depends on, from its plan (`relied`): the
cardinality of every relationship it joins through, the key of a period-end fact and of a
detail page's base, and the consistency of the aggregate table it reads. Violated evidence
it relies on refuses it before anything is read (`EVIDENCE_VIOLATED`).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import pyarrow as pa
from orvex.semantic.adapters.duckdb import (
    EvidenceCheck,
    consistency_check,
    orphan_check,
    overlap_check,
    repeat_check,
    unique_check,
)
from orvex.semantic.canonical import fingerprint
from orvex.semantic.compiler.compiled import CompiledModel
from orvex.semantic.compiler.parsing import MemberId
from orvex.semantic.errors import SemanticError
from orvex.semantic.ossie import sql
from orvex.semantic.planning import PlanError
from orvex.semantic.receipt.building import EvidenceUse

DATASET_KINDS = ("unique", "non_overlap", "append_only")
RELATIONSHIP_KINDS = ("cardinality", "referential_integrity")
CONSISTENT = "consistent"
BATCH_SIZE = 16

Status = Literal["verified", "violated"]


@dataclass(frozen=True, slots=True)
class Declaration:
    """One declared piece of evidence of one model element."""

    element: Literal["dataset", "relationship"]
    name: str  # the element's name, normalized
    kind: str
    fingerprint: str  # of every model element its check depends on
    dataset: str  # as written: the dataset the check reads, which a receipt and an error name
    fields: tuple[str, ...]  # as written: the fields the check reads there

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.element, self.name, self.kind)


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """The outcome of one check, on one snapshot, of one declaration."""

    element: str
    name: str  # normalized
    kind: str
    snapshot: int
    declaration: str  # the fingerprint of the declaration checked
    status: Status
    violations: Mapping[str, int]  # by kind: `duplicates`, `missing`, ... (all zero: verified)
    at: datetime
    run: str | None = None  # the run that triggered it, when one did

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.element, self.name, self.kind)


@dataclass(frozen=True, slots=True)
class Skipped:
    """A declaration left `declared`: `budget` (the check did not finish within its time) or
    `unsupported` (no statement on one snapshot checks it). Builders are told."""

    declaration: Declaration
    reason: Literal["budget", "unsupported"]


@dataclass(frozen=True, slots=True)
class Verification:
    records: tuple[EvidenceRecord, ...]
    skipped: tuple[Skipped, ...] = ()


# --- what the model declares -----------------------------------------------------------------


def _numbers_as_text(value: object) -> Any:  # noqa: ANN401 - a JSON document
    """A model fragment as fingerprintable JSON: a float as its shortest text, never rounded."""
    if isinstance(value, Mapping):
        return {str(k): _numbers_as_text(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_numbers_as_text(v) for v in value]
    if isinstance(value, float):
        return repr(value)
    return value


def _items(node: object, key: str) -> list[Any]:
    value = node.get(key, ()) if isinstance(node, Mapping) else ()
    return list(value) if isinstance(value, list | tuple) else []


@dataclass
class _Model:
    """The model elements evidence is declared on, by normalized name."""

    compiled: CompiledModel
    datasets: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    relationships: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    payloads: dict[str, Mapping[str, Any]] = field(default_factory=dict)  # by JSON pointer
    pointers: dict[tuple[str, str], str] = field(default_factory=dict)  # (element, name)

    def __post_init__(self) -> None:
        document = self.compiled.ossie.document
        self.payloads = {
            e.pointer.rsplit("/custom_extensions/", 1)[0]: e.payload
            for e in self.compiled.ossie.product_extensions
        }
        for at, node in enumerate(_items(document, "datasets")):
            name = sql.normalize(str(node["name"]))
            self.datasets[name] = node
            self.pointers[("dataset", name)] = f"/datasets/{at}"
        for at, node in enumerate(_items(document, "relationships")):
            name = sql.normalize(str(node["name"]))
            self.relationships[name] = node
            self.pointers[("relationship", name)] = f"/relationships/{at}"

    def payload(self, element: str, name: str) -> Mapping[str, Any]:
        return self.payloads.get(self.pointers.get((element, name), ""), {})

    def member(self, dataset: str, name: object) -> MemberId:
        member = MemberId("field", dataset, sql.normalize(str(name)))
        if member not in self.compiled.members:
            raise _Unchecked
        return member

    def written(self, member: MemberId) -> str:
        return self.compiled.require(member).parsed.label

    def part(self, element: str, name: str) -> dict[str, Any]:
        node = (self.datasets if element == "dataset" else self.relationships)[name]
        return {"node": node, "payload": self.payload(element, name)}

    def fingerprint(self, element: str, name: str, kind: str) -> str:
        """Every model element the check reads, as declared: the element, the datasets it
        reads and, for consistency, every metric. Anything else changed keeps the record."""
        parts: dict[str, Any] = {element: self.part(element, name)}
        if element == "relationship":
            node = self.relationships[name]
            for side in ("from", "to"):
                parts[side] = self.part("dataset", sql.normalize(str(node[side])))
        if kind == CONSISTENT:
            base = sql.normalize(
                str(self.payload("dataset", name)["aggregate_table"]["base_dataset"])
            )
            parts["base"] = self.part("dataset", base)
            parts["metrics"] = _items(self.compiled.ossie.document, "metrics")
        document = {"element": element, "name": name, "kind": kind, "model": parts}
        return fingerprint("evidence_declaration", 1, _numbers_as_text(document))


class _Unchecked(Exception):
    """A declaration no statement on one snapshot checks."""


def declarations(compiled: CompiledModel) -> tuple[Declaration, ...]:
    """Every piece of evidence the model declares, and `consistent` for every aggregate table
    that names its base, in a fixed order."""
    model = _Model(compiled)
    found: list[Declaration] = []
    for name, node in sorted(model.datasets.items()):
        payload = model.payload("dataset", name)
        kinds = [str(k) for k in _items(payload, "evidence")]
        table = payload.get("aggregate_table")
        if isinstance(table, Mapping) and table.get("base_dataset") is not None:
            kinds.append(CONSISTENT)
        for kind in kinds:
            found.append(
                Declaration(
                    "dataset",
                    name,
                    kind,
                    model.fingerprint("dataset", name, kind),
                    str(node["name"]),
                    _dataset_fields(model, name, kind),
                )
            )
    for name, node in sorted(model.relationships.items()):
        payload = model.payload("relationship", name)
        for kind in (str(k) for k in _items(payload, "evidence")):
            side = "from" if kind == "referential_integrity" else "to"
            columns = node["from_columns"] if side == "from" else node["to_columns"]
            found.append(
                Declaration(
                    "relationship",
                    name,
                    kind,
                    model.fingerprint("relationship", name, kind),
                    str(model.datasets[sql.normalize(str(node[side]))]["name"]),
                    tuple(str(c) for c in columns),
                )
            )
    return tuple(found)


def _dataset_fields(model: _Model, name: str, kind: str) -> tuple[str, ...]:
    try:
        if kind == "unique":
            return tuple(model.written(m) for m in _key(model, name))
        if kind == "non_overlap":
            entity, start, end = _validity(model, name)
            return tuple(model.written(m) for m in (*entity, start, end))
    except _Unchecked:
        pass
    return ()


def _key(model: _Model, dataset: str) -> tuple[MemberId, ...]:
    key = _items(model.datasets[dataset], "primary_key")
    if not key:
        raise _Unchecked
    return tuple(model.member(dataset, k) for k in key)


def _validity(model: _Model, dataset: str) -> tuple[tuple[MemberId, ...], MemberId, MemberId]:
    """A dataset's entity and its validity's bounds: the entity is `history.entity_key`, or
    else the primary key without the validity's start."""
    payload = model.payload("dataset", dataset)
    validity = (payload.get("time_roles") or {}).get("validity")
    if not isinstance(validity, Mapping):
        raise _Unchecked
    start = model.member(dataset, validity["from"])
    end = model.member(dataset, validity["to"])
    declared = (payload.get("history") or {}).get("entity_key")
    if declared:
        entity = tuple(model.member(dataset, k) for k in declared)
    else:
        entity = tuple(k for k in _key(model, dataset) if k != start)
    if not entity:
        raise _Unchecked
    return entity, start, end


# --- the checks ------------------------------------------------------------------------------


def _pairs(model: _Model, node: Mapping[str, Any]) -> tuple[tuple[MemberId, MemberId], ...]:
    source, target = sql.normalize(str(node["from"])), sql.normalize(str(node["to"]))
    return tuple(
        (model.member(source, a), model.member(target, b))
        for a, b in zip(node["from_columns"], node["to_columns"], strict=True)
    )


def _statements(model: _Model, declaration: Declaration) -> tuple[EvidenceCheck, ...]:
    """The statements checking one declaration (two for a `one_to_one` cardinality), or
    `_Unchecked`."""
    compiled, name, kind = model.compiled, declaration.name, declaration.kind
    if declaration.element == "dataset":
        if kind == "unique":
            return (unique_check(compiled, name, _key(model, name)),)
        if kind == "non_overlap":
            entity, start, end = _validity(model, name)
            return (overlap_check(compiled, name, entity, start, end),)
        if kind == CONSISTENT:
            return (_consistency(model, name),)
        raise _Unchecked  # append_only: two snapshots, never one statement
    node = model.relationships[name]
    payload = model.payload("relationship", name)
    source, target = sql.normalize(str(node["from"])), sql.normalize(str(node["to"]))
    pairs = _pairs(model, node)
    if kind == "referential_integrity":
        return (orphan_check(compiled, source, target, pairs),)
    temporal = payload.get("temporal")
    if isinstance(temporal, Mapping):  # one valid version of each referenced key (C6)
        start = _ref(model, target, temporal["valid_from"])
        end = _ref(model, target, temporal["valid_to"])
        entity = tuple(b for _, b in pairs)
        return (overlap_check(compiled, target, entity, start, end, (source, pairs)),)
    checks = [repeat_check(compiled, target, source, pairs)]
    if payload.get("cardinality") == "one_to_one":
        checks.append(repeat_check(compiled, source, target, tuple((b, a) for a, b in pairs)))
    return tuple(checks)


def _ref(model: _Model, dataset: str, reference: object) -> MemberId:
    """A temporal bound (`name` or `dataset.name`) as a field of `dataset`, or `_Unchecked`."""
    owner, _, name = str(reference).rpartition(".")
    if owner and sql.normalize(owner) != dataset:
        raise _Unchecked
    return model.member(dataset, name)


def _consistency(model: _Model, table: str) -> EvidenceCheck:
    """`consistent`: the table's grain mapped to its base, one field of each conformed
    dimension on each side, and every measure that stores a base metric as the sum of one
    column, as P2-25's proof reads them. Anything else: not checkable, never consistent."""
    from orvex.semantic.correctness.materialization import stored_measures

    compiled = model.compiled
    payload = model.payload("dataset", table)
    aggregate = payload["aggregate_table"]
    base = sql.normalize(str(aggregate["base_dataset"]))
    if base not in model.datasets:
        raise _Unchecked

    def conformed(dataset: str) -> dict[str, list[MemberId]]:
        found: dict[str, list[MemberId]] = {}
        for member in compiled.members:
            if member.kind != "field" or member.dataset != dataset:
                continue
            declared = model.payloads.get(compiled.require(member).parsed.pointer, {})
            name = declared.get("conformed_dimension")
            if name is not None:
                found.setdefault(sql.normalize(str(name)), []).append(member)
        return found

    own, based = conformed(table), conformed(base)
    grain = []
    for dimension in _items(payload.get("grain"), "dimensions"):
        name = sql.normalize(str(dimension))
        if len(own.get(name, ())) != 1 or len(based.get(name, ())) != 1:
            raise _Unchecked
        grain.append((based[name][0], own[name][0]))
    measures = tuple(sorted(stored_measures(compiled, table, aggregate, base).items()))
    if not measures:
        raise _Unchecked
    return consistency_check(compiled, base, table, tuple(grain), measures)


def verify(
    compiled: CompiledModel,
    read: Callable[[Any], pa.RecordBatchReader],
    snapshot: int,
    *,
    at: datetime,
    run: str | None = None,
    budget_ms: int | None = None,
    only: Iterable[str] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Verification:
    """Every declaration of `compiled` (or of the datasets and relationships in `only`, by
    normalized name) checked on `snapshot` through `read` (`store/v1` `ReadRequest`s), within
    `budget_ms` for all of them. A check that does not finish in what is left of the budget,
    or has none left, leaves its evidence `declared`; any other failure is raised."""
    from orvex.contracts.store.v1 import Parameter, ReadRequest

    model = _Model(compiled)
    chosen = None if only is None else {sql.normalize(n) for n in only}
    started = clock()
    records: list[EvidenceRecord] = []
    skipped: list[Skipped] = []
    for declaration in declarations(compiled):
        if chosen is not None and declaration.name not in chosen:
            continue
        try:
            statements = _statements(model, declaration)
        except _Unchecked, SemanticError, PlanError:  # nothing this adapter writes checks it
            skipped.append(Skipped(declaration, "unsupported"))
            continue
        violations: dict[str, int] = {}
        try:
            for statement in statements:
                left = None if budget_ms is None else budget_ms - (clock() - started) * 1000
                if left is not None and left < 1:
                    raise _OverBudget
                request = ReadRequest(
                    kind="read",
                    sql=statement.sql,
                    parameters=[Parameter.model_validate(dict(p)) for p in statement.parameters],
                    snapshot=snapshot,
                    batch_size=BATCH_SIZE,
                    timeout_ms=None if left is None else max(1, int(left)),
                )
                try:
                    table = read(request).read_all()
                except Exception as error:
                    if _stopped(error):
                        raise _OverBudget from error
                    raise
                for name in statement.counts:
                    count = table.column(name)[0].as_py()
                    violations[name.lstrip("#")] = violations.get(name.lstrip("#"), 0) + int(count)
        except _OverBudget:
            skipped.append(Skipped(declaration, "budget"))
            continue
        records.append(
            EvidenceRecord(
                declaration.element,
                declaration.name,
                declaration.kind,
                snapshot,
                declaration.fingerprint,
                "violated" if any(violations.values()) else "verified",
                violations,
                at,
                run,
            )
        )
    return Verification(tuple(records), tuple(skipped))


class _OverBudget(Exception):
    pass


def _stopped(error: Exception) -> bool:
    """A read stopped by an execution limit: the store's time limit or DuckDB's memory."""
    code = getattr(error, "code", None)
    details = getattr(error, "details", None)
    text = str(details.get("duckdb", "")) if isinstance(details, Mapping) else ""
    return code == "STORE_TIMEOUT" or (
        code == "STORE_QUERY_FAILED" and text.startswith("Out of Memory Error")
    )


# --- what a question relies on ---------------------------------------------------------------


class Ledger:
    """The records of one snapshot that still check the model's declarations as they are now.
    A record of another snapshot, or of a changed declaration, is no record: `declared`."""

    def __init__(
        self, compiled: CompiledModel, records: Iterable[EvidenceRecord], snapshot: int
    ) -> None:
        self.declared = {d.key: d for d in declarations(compiled)}
        self.records: dict[tuple[str, str, str], EvidenceRecord] = {}
        for record in records:
            declaration = self.declared.get(record.key)
            if declaration is None or record.snapshot != snapshot:
                continue
            if record.declaration != declaration.fingerprint:
                continue
            self.records[record.key] = record

    def verified(self, key: tuple[str, str, str]) -> bool:
        record = self.records.get(key)
        return record is not None and record.status == "verified"

    def uses(self, relied: Iterable[tuple[str, str, str]]) -> list[EvidenceUse]:
        """The receipt's `evidence[]` for what a plan relies on, or `EVIDENCE_VIOLATED` for the
        first violated one, before anything is read. Evidence the model does not declare is
        not evidence, and is not listed."""
        found: dict[tuple[str, ...], EvidenceUse] = {}
        for key in relied:
            declaration = self.declared.get(key)
            if declaration is None:
                continue
            record = self.records.get(key)
            if record is not None and record.status == "violated":
                raise SemanticError(
                    "EVIDENCE_VIOLATED",
                    {
                        "dataset": declaration.dataset,
                        "evidence": declaration.kind,
                        "fields": list(declaration.fields),  # a violated check had them
                        "violations": str(sum(record.violations.values())),
                    },
                )
            use = (
                EvidenceUse(
                    declaration.dataset,
                    declaration.kind,
                    "verified",
                    verified_by_run=record.run,
                    verified_at=record.at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                )
                if record is not None
                else EvidenceUse(declaration.dataset, declaration.kind, "declared")
            )
            found[(use.dataset, use.kind, use.status, use.verified_by_run or "")] = use
        return list(found.values())


def relied(plan: Any) -> list[tuple[str, str, str]]:  # noqa: ANN401 - a `SemanticPlan`
    """The evidence a plan's answer depends on, from its steps, and nothing it does not:
    - the `cardinality` of every relationship it joins through (C1, C3, C6): a key repeated
      among the rows it refers to multiplies them, or matches an event twice. A join keeps
      a row with no match in the `NULL` group, so `referential_integrity` changes no answer,
      and a repeated key no row refers to multiplies nothing;
    - the key (`unique`) of a period-end fact (C2) and of a detail page's base (P2-24), whose
      rows are ranked or paged by it;
    - the consistency of the aggregate table it reads (C8)."""
    datasets = {s.alias: s.dataset for s in plan.sources}
    found: list[tuple[str, str, str]] = [
        ("relationship", sql.normalize(join.relationship), "cardinality") for join in plan.joins
    ]
    for source in plan.sources:
        if source.latest is not None:
            found.append(("dataset", source.dataset, "unique"))
    if plan.mode == "detail":
        found.append(("dataset", datasets["t0"], "unique"))
    if plan.rollup is not None:
        found.append(("dataset", plan.rollup.dataset, CONSISTENT))
    return list(dict.fromkeys(found))
