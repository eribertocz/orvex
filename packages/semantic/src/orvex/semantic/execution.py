"""Execution and the one entry point to data (P2-12, #311; laws 3 and 4).

`answer(query, compiled, policies, context, engine)` is the only public path from a question
to data: it binds the query with the caller's visibility, runs `analyze`, `plan` and the DuckDB
adapter, then reads **once**, through `engine.read`, on the snapshot `engine.snapshot` names.
`semantic` never imports `store`: the composing module supplies the read function, the value
encoder and the snapshot it fixed when the question arrived.

- **The snapshot stays pinned for the whole read.** The read names it, and `store/v1` serves
  every batch of one read from it (ADR-0025). Without a snapshot there is no read: a missing or
  expired one fails with the store's stable error, never with current data.
- **Nothing partial.** The whole stream is read before a result exists; a failure on any batch
  (a timeout, an expired snapshot) leaves no result.
- **Two identities.** `execution_fingerprint` and the result's canonical bytes are the
  deterministic identity: same model, query, permissions, snapshot, engine and adapter, same
  values. `receipt_id` also covers timings and metadata, and may differ between two runs.
- No result is reused across callers: there is no cache (one would need `may_reuse`, P2-09).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
from orvex.semantic import zonedata
from orvex.semantic.adapters.duckdb import PhysicalQuery, compile_plan
from orvex.semantic.compiler.compiled import CompiledModel
from orvex.semantic.compiler.parsing import MemberId
from orvex.semantic.correctness import CorrectnessReport, analyze
from orvex.semantic.errors import SemanticError
from orvex.semantic.ossie import sql
from orvex.semantic.planning import PlanError, SemanticPlan, plan
from orvex.semantic.policies import PolicyModel, read_caller
from orvex.semantic.query.binding import SCOPES, BoundQuery, Limits, bind_query
from orvex.semantic.receipt import (
    DataRead,
    ExecutionUse,
    Key,
    ModelUse,
    ReceiptRecord,
    build_receipt,
)
from orvex.semantic.result import SemanticResult, ValueEncoder, build_result
from orvex.semantic.result.descriptors import arrow_type

BATCH_SIZE = 65_536


@dataclass(frozen=True)
class Engine:
    """What the composing module supplies for one question."""

    read: Callable[[Any], pa.RecordBatchReader]  # `store/v1`: one `ReadRequest`, read once
    encoder: ValueEncoder  # `store/v1`'s exact JSON encoding of Arrow values
    snapshot: Mapping[str, object]  # `store/v1` `Snapshot`, fixed when the question arrived
    engine_version: str
    result_key: Key
    seal_key: Key
    instant: datetime  # the query instant (P2-05)
    limits: Limits
    timeout_ms: int | None = None
    clock: Callable[[], float] = time.monotonic
    now: Callable[[], datetime] = lambda: datetime.now(UTC)


@dataclass(frozen=True)
class Answer:
    result: SemanticResult  # carries its receipt reference
    receipt: ReceiptRecord
    report: CorrectnessReport
    plan: SemanticPlan
    physical: PhysicalQuery


def _plain(value: object) -> Any:  # noqa: ANN401 - a JSON document
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


def _instant(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _label(report: CorrectnessReport, key: Any) -> str:  # noqa: ANN401
    kind, dataset, name = key
    member = MemberId("field" if kind == "field" else "metric", dataset or None, name)
    compiled: CompiledModel = report.compiled
    return compiled.require(member).parsed.label


def receipt_question(report: CorrectnessReport) -> dict[str, Any]:
    """The bound query as the receipt records it: members by their model labels, values in
    the query's own form (`receipt/v1` `Question`)."""
    canonical = report.bound.canonical

    def value(entry: Any) -> dict[str, object]:  # noqa: ANN401
        kind, raw = entry
        return {"type": "decimal" if kind == "number" else kind, "value": raw}

    def predicate(found: Mapping[str, Any]) -> dict[str, object]:
        shown: dict[str, object] = {"op": found["op"]}
        for key in ("include_null", "exclude_null"):
            if found.get(key):
                shown[key] = True
        if "values" in found:
            shown["values"] = [value(v) for v in found["values"]]
        for key in ("gte", "gt", "lte", "lt"):
            if key in found:
                shown[key] = value(found[key])
        return shown

    roles = {
        sql.normalize(str(r["name"])): str(r["name"])
        for r in report.compiled.ossie.document.get("relationships", ())
    }
    question: dict[str, Any] = {"mode": canonical["mode"]}
    question["metrics"] = [{"metric": _label(report, m)} for m, _, _ in canonical["metrics"]]
    group_by = []
    for member, role, bucket in canonical["group_by"]:
        entry: dict[str, Any] = {"member": _label(report, member)}
        if role:
            entry["role"] = roles[role]
        if bucket:
            entry["bucket"] = {"unit": bucket[0]}
        group_by.append(entry)
    if group_by:
        question["group_by"] = group_by
    filters = []
    for found in canonical["filters"]:
        entry = {"scope": SCOPES[found["scope"]], "member": _label(report, found["member"])}
        if found["role"]:
            entry["role"] = roles[found["role"]]
        entry["predicate"] = predicate(found["predicate"])
        filters.append(entry)
    if filters:
        question["filters"] = filters
    order = [
        {"by": _label(report, m), "direction": direction, "nulls": nulls}
        for m, direction, nulls in canonical["order"]
    ]
    if order:
        question["order"] = order
    if canonical.get("limit") is not None:
        question["limit"] = canonical["limit"]
    return question


def _check_zones(physical: PhysicalQuery, engine: Engine) -> None:
    """Every zone the statement converts to, checked on this engine against the pinned
    `tzdata` before the question's read (C6): once per zone and connection."""
    if not physical.zones:
        return
    from orvex.contracts.store.v1 import Parameter, ReadRequest

    snapshot = engine.snapshot.get("id") if isinstance(engine.snapshot, Mapping) else None
    if not isinstance(snapshot, int) or isinstance(snapshot, bool):
        raise PlanError("no snapshot was fixed for this question: nothing is read")

    def count(zone: str) -> int:
        request = ReadRequest(
            kind="read",
            sql=zonedata.CHECK_SQL,
            parameters=[Parameter.model_validate(p) for p in zonedata.parameters(zone)],
            snapshot=snapshot,
            batch_size=BATCH_SIZE,
            timeout_ms=engine.timeout_ms,
        )
        table = engine.read(request).read_all()
        if [(f.name, f.type) for f in table.schema] != [("#different", pa.int64())]:
            raise PlanError("the zone check returned another shape")
        found = pc.max(table.column("#different")).as_py()  # one scalar: a count
        return int(found) if found is not None else 0

    owner = getattr(engine.read, "__self__", engine.read)
    try:
        zonedata.verify(physical.zones, owner, count)
    except zonedata.ZoneDataMismatch:
        raise SemanticError("UNSUPPORTED_OPERATION", {"operation": "time_zone_data"}) from None


def _read(physical: PhysicalQuery, engine: Engine) -> pa.Table:
    from orvex.contracts.store.v1 import Parameter, ReadRequest

    snapshot = engine.snapshot.get("id") if isinstance(engine.snapshot, Mapping) else None
    if not isinstance(snapshot, int) or isinstance(snapshot, bool):
        raise PlanError("no snapshot was fixed for this question: nothing is read")
    request = ReadRequest(
        kind="read",
        sql=physical.sql,
        parameters=[Parameter.model_validate(dict(p)) for p in physical.parameters],
        snapshot=snapshot,
        batch_size=BATCH_SIZE,
        timeout_ms=engine.timeout_ms,
    )
    reader = engine.read(request)
    table = reader.read_all()  # every batch, or the read's error: never a partial result
    expected = [(c.name, arrow_type(c.encoding)) for c in physical.columns]
    expected += [(name, pa.bool_()) for name, _ in physical.checks]
    expected += [(name, pa.int64()) for name, _ in physical.weights]
    expected += [(name, pa.int64()) for name, _ in physical.as_of]
    found = [(f.name, f.type) for f in table.schema]
    if found != expected:
        raise PlanError(f"the engine returned {found}, the plan describes {expected}")
    for name, metric in physical.checks:  # a declared `reject` refuses the whole result
        if table.filter(pc.fill_null(table.column(name), False)).num_rows:
            raise SemanticError("DIVISION_POLICY_VIOLATION", {"metric": metric})
        table = table.drop_columns([name])
    for name, bridge in physical.weights:  # invalid bridge weights refuse the whole result
        invalid = pc.max(table.column(name)).as_py()  # one scalar, the same on every row
        if invalid:
            raise SemanticError(
                "BRIDGE_WEIGHTS_INVALID", {"bridge": bridge, "groups": str(invalid)}
            )
        table = table.drop_columns([name])
    for name, relationship in physical.as_of:  # a row matched not exactly once refuses all
        unmatched = pc.max(table.column(name)).as_py()  # one scalar, the same on every row
        if unmatched:
            raise SemanticError(
                "TEMPORAL_MATCH_NOT_UNIQUE",
                {"relationship": relationship, "violations": str(unmatched)},
            )
        table = table.drop_columns([name])
    return table


def _execute(
    physical: PhysicalQuery,
    engine: Engine,
    report: CorrectnessReport,
    plan_: SemanticPlan,
    context: Mapping[str, Any],
) -> Answer:
    """One read of `physical` on `engine.snapshot`, and its result and receipt."""
    started = engine.now()
    clock = engine.clock()
    _check_zones(physical, engine)
    table = _read(physical, engine)
    duration = max(0, round((engine.clock() - clock) * 1000))
    limit = plan_.limit
    truncated = limit is not None and table.num_rows > limit
    if truncated and limit is not None:
        table = table.slice(0, limit)
    page: dict[str, object] = {"truncated": truncated}
    if limit is not None:
        page["limit"] = limit
    result = build_result(
        table,
        physical.columns,
        mode="aggregate",
        query_fingerprint=report.query_fingerprint,
        model_fingerprint=report.model_fingerprint,
        page=page,
    )
    compiled: CompiledModel = report.compiled
    snapshot = engine.snapshot
    data = [
        DataRead(
            name=_written(compiled, source.dataset),
            route=source.route,
            retained=True,
            snapshot_id=int(str(snapshot["id"])),
            committed_at=_instant(datetime.fromisoformat(str(snapshot["committed_at"]))),
        )
        for source in plan_.sources
    ]
    record = build_receipt(
        query_canonical=_plain(report.bound.canonical),
        question_document=receipt_question(report),
        query_fingerprint=report.query_fingerprint,
        model=ModelUse(compiled.model_fingerprint, compiled.reader),
        permission_fingerprint=str(context["permission_fingerprint"]),
        data=list({d.name: d for d in data}.values()),
        execution=ExecutionUse(
            engine="duckdb",
            engine_version=engine.engine_version,
            adapter=physical.adapter,
            adapter_version=physical.adapter_version,
            started_at=_instant(started),
            duration_ms=duration,
        ),
        result_canonical=result.canonical(engine.encoder),
        result_key=engine.result_key,
        seal_key=engine.seal_key,
        obligations=[o.for_receipt() for o in report.obligations],
        plan_fingerprint=plan_.fingerprint,
    )
    referenced = result.with_receipt(record.receipt_id, record.reproducibility["class"])
    return Answer(referenced, record, report, plan_, physical)


def _written(compiled: CompiledModel, dataset: str) -> str:
    datasets = compiled.ossie.document.get("datasets", ())
    for node in datasets if isinstance(datasets, tuple | list) else ():
        if sql.normalize(str(node["name"])) == dataset:
            return str(node["name"])
    return dataset


def answer(
    query: str | bytes,
    compiled: CompiledModel,
    policies: PolicyModel,
    context: Mapping[str, Any],
    engine: Engine,
) -> Answer:
    """The answer to `query` for the caller `context` describes, or a `SemanticError`. Policies
    come first, by the same calls every path uses (P2-08 to P2-11); a refused question reads
    nothing."""
    caller = read_caller(context)

    def hidden(member: MemberId) -> bool:
        return policies.protection(member, caller.groups) == "hidden"

    bound: BoundQuery = bind_query(
        compiled, query, instant=engine.instant, limits=engine.limits, hidden=hidden
    )
    report = analyze(bound, compiled, policies, context)
    logical = plan(report)
    physical = compile_plan(logical)
    return _execute(physical, engine, report, logical, context)
