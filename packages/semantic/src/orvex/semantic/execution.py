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

import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
from orvex.contracts.store.v1 import StatisticsRequest, StatisticsResult
from orvex.semantic import zonedata
from orvex.semantic.adapters.duckdb import PhysicalQuery, compile_plan, quote
from orvex.semantic.canonical import fingerprint
from orvex.semantic.compiler.compiled import CompiledModel
from orvex.semantic.compiler.parsing import MemberId
from orvex.semantic.correctness import CorrectnessReport, analyze
from orvex.semantic.correctness.discharge import verify_physical
from orvex.semantic.errors import SemanticError
from orvex.semantic.estimates import estimate
from orvex.semantic.evidence import CONSISTENT, EvidenceRecord, Ledger, relied
from orvex.semantic.ossie import sql
from orvex.semantic.planning import PlanError, SemanticPlan, plan
from orvex.semantic.policies import PolicyModel, read_caller
from orvex.semantic.query.binding import SCOPES, BoundQuery, Limits, bind_query
from orvex.semantic.receipt import (
    DataRead,
    EvidenceUse,
    ExecutionUse,
    Key,
    ModelUse,
    PolicyUse,
    ReceiptRecord,
    build_receipt,
)
from orvex.semantic.result import SemanticResult, ValueEncoder, build_result
from orvex.semantic.result.building import CellState
from orvex.semantic.result.cursors import (
    CursorBinding,
    CursorForm,
    CursorState,
    SnapshotStatus,
    issue_cursor,
    verify_cursor,
)
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
    memory_bytes: int | None = None  # the engine's memory limit, as `server` configured it
    # Detail pages (P2-24): the cursor form, and the snapshot of a cursor's data version while
    # it is still kept (None once expired); both supplied by `server`.
    cursors: CursorForm | None = None
    pinned: Callable[[str], Mapping[str, object] | None] | None = None
    # Evidence (P2-26): the verification records of a snapshot, supplied by `server`; none:
    # every piece of evidence is `declared`, and no aggregate table is read in place of its
    # base, since none is verified consistent (C8).
    evidence: Callable[[Mapping[str, object]], Iterable[EvidenceRecord]] | None = None
    # Read-cost estimates (#352): the snapshot's file statistics (`store/v1`), none when the
    # store gives none; supplied by `server`. None: no estimate, which decides nothing anyway.
    statistics: Callable[[StatisticsRequest], StatisticsResult | None] | None = None
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
    if canonical["mode"] == "detail":  # the fields shown and the page asked (P2-24)
        question["fields"] = [
            {"member": _label(report, m)} | ({"role": roles[role]} if role else {})
            for m, role in canonical["fields"]
        ]
        page = canonical["page"]
        question["page"] = {"size": page["size"]} | (
            {"after": page["after"]} if page.get("after") is not None else {}
        )
    else:
        question["metrics"] = [{"metric": _label(report, m)} for m, _, _ in canonical["metrics"]]
    group_by = []
    for member, role, bucket in canonical.get("group_by", ()):
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


def _budget(error: Exception, engine: Engine) -> SemanticError | None:
    """A read stopped by an execution limit, as the budget it reached (P2-24): the engine's
    time limit (`STORE_TIMEOUT`, the store interrupts the statement) or its memory limit
    (DuckDB's out-of-memory error, which the store reports with DuckDB's text). The whole
    result is refused, never a partial page; any other failure stays what it is."""
    code = getattr(error, "code", None)
    if code == "STORE_TIMEOUT" and engine.timeout_ms is not None:
        limit = str(engine.timeout_ms)
        return SemanticError("QUERY_BUDGET_EXCEEDED", {"budget": "time_ms", "limit": limit})
    details = getattr(error, "details", None)
    text = str(details.get("duckdb", "")) if isinstance(details, Mapping) else ""
    out_of_memory = code == "STORE_QUERY_FAILED" and text.startswith("Out of Memory Error")
    if out_of_memory and engine.memory_bytes is not None:
        limit = str(engine.memory_bytes)
        return SemanticError("QUERY_BUDGET_EXCEEDED", {"budget": "memory_bytes", "limit": limit})
    return None


def _read(physical: PhysicalQuery, engine: Engine, budgeted: bool = False) -> pa.Table:
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
    try:
        reader = engine.read(request)
        table = reader.read_all()  # every batch, or the read's error: never a partial result
    except Exception as error:
        budget = _budget(error, engine) if budgeted else None
        if budget is None:
            raise
        raise budget from error
    expected = [(c.name, arrow_type(c.encoding)) for c in physical.columns]
    expected += [(name, pa.bool_()) for name, _ in physical.checks]
    expected += [(name, pa.int64()) for name, _ in physical.weights]
    expected += [(name, pa.int64()) for name, _ in physical.as_of]
    expected += [(name, arrow_type(encoding)) for name, encoding in physical.hidden]
    expected += [(physical.level, pa.int32())] if physical.level else []
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


def _detail_page(
    table: pa.Table,
    physical: PhysicalQuery,
    plan_: SemanticPlan,
    engine: Engine,
    binding: CursorBinding | None,
) -> tuple[pa.Table, dict[str, object]]:
    """A detail page (P2-24): at most `size` rows; when one more was read, a cursor to the next
    page, sealing the last row's sort-key values and this snapshot. The hidden sort columns
    never reach the result."""
    size = plan_.page
    if size is None or binding is None:
        raise PlanError("a detail page has a size and a cursor binding")
    page: dict[str, object] = {"size": size}
    if table.num_rows > size:
        table = table.slice(0, size)
        if engine.cursors is None:
            raise PlanError("no cursor form to continue a detail page with")
        last = table.select([name for name, _ in physical.sorts]).slice(size - 1, 1)
        text = "".join(
            engine.encoder(pa.RecordBatchReader.from_batches(last.schema, last.to_batches()))
        )
        (position,) = json.loads(text)["rows"]  # one row: the exact values of its sort keys
        state = CursorState(str(engine.snapshot["id"]), tuple(position))
        page["next"] = issue_cursor(engine.cursors, binding, state)
    if physical.hidden:
        table = table.drop_columns([name for name, _ in physical.hidden])
    return table, page


def _status(engine: Engine) -> Callable[[str], SnapshotStatus]:
    """How this engine sees a cursor's data version: kept, expired (an earlier snapshot no
    longer kept), or foreign (anything this route never served)."""

    def status(version: str) -> SnapshotStatus:
        if engine.pinned is not None and engine.pinned(version) is not None:
            return SnapshotStatus.AVAILABLE
        current = engine.snapshot.get("id")
        if version.isdigit() and isinstance(current, int) and int(version) <= current:
            return SnapshotStatus.EXPIRED
        return SnapshotStatus.FOREIGN

    return status


def _row_roles(
    table: pa.Table, physical: PhysicalQuery, plan_: SemanticPlan
) -> tuple[pa.Table, list[dict[str, object]] | None, list[CellState]]:
    """With totals (P2-23): the table without its level column, each row's role, and a
    running total's cells `undefined_at_total` at every total row (S9)."""
    if physical.level is None:
        return table, None, []
    levels = table.column(physical.level).to_pylist()  # one small integer per result row
    table = table.drop_columns([physical.level])
    groups = sum(1 for c in physical.columns if c.role == "dimension")
    roles: list[dict[str, object]] = [
        {
            "role": "total" if level == 0 else "detail" if level == groups else "subtotal",
            "level": level,
        }
        for level in levels
    ]
    windows = {w.name for w in plan_.windows}
    at = [i for i, c in enumerate(physical.columns) if c.name in windows]
    states = [
        CellState(row, column, "undefined_at_total")
        for row, level in enumerate(levels)
        if level != groups or not groups
        for column in at
    ]
    return table, roles, states


def _execute(
    physical: PhysicalQuery,
    engine: Engine,
    report: CorrectnessReport,
    plan_: SemanticPlan,
    context: Mapping[str, Any],
    evidence: Mapping[str, str],
    binding: CursorBinding | None = None,
    uses: Sequence[EvidenceUse] = (),
) -> Answer:
    """One read of `physical` on `engine.snapshot`, and its result and receipt, which names
    the evidence of each obligation (C9)."""
    started = engine.now()
    clock = engine.clock()
    _check_zones(physical, engine)
    placement = plan_.placement
    applied = dict(placement.applied) if placement is not None else {}
    notices = [
        SemanticError("POLICY_ATTRIBUTE_MISSING", {"policy": name})
        for name, how in sorted(applied.items())
        if how == "no_rows"
    ]
    for name in _unregistered(placement, engine):  # a deny-list caller absent: zero rows
        applied[name] = "no_rows"
        notices.append(SemanticError("POLICY_MAPPING_MISSING", {"policy": name}))
    snapshot = engine.snapshot.get("id")
    estimated = (  # an upper bound, recorded only: it never refuses (#352)
        estimate(plan_, physical, report.compiled, engine.statistics, snapshot)
        if isinstance(snapshot, int) and not isinstance(snapshot, bool)
        else None
    )
    table = _read(physical, engine, budgeted=plan_.mode == "detail")
    duration = max(0, round((engine.clock() - clock) * 1000))
    page: dict[str, object]
    if plan_.mode == "detail":
        table, page = _detail_page(table, physical, plan_, engine, binding)
    else:
        limit = plan_.limit
        truncated = limit is not None and table.num_rows > limit
        if truncated and limit is not None:
            table = table.slice(0, limit)
        page = {"truncated": truncated}
        if limit is not None:
            page["limit"] = limit
    table, row_roles, states = _row_roles(table, physical, plan_)
    result = build_result(
        table,
        physical.columns,
        mode=plan_.mode,
        query_fingerprint=report.query_fingerprint,
        model_fingerprint=report.model_fingerprint,
        page=page,
        notices=notices,
        audience="builder" if read_caller(context).builder else "other",
        cell_states=states,
        row_roles=row_roles,
    )
    compiled: CompiledModel = report.compiled
    snapshot = engine.snapshot
    data = [
        DataRead(  # the aggregate table, when it is read in place of its base (C8)
            name=_written(
                compiled,
                plan_.rollup.dataset
                if plan_.rollup is not None and source.dataset == plan_.rollup.base
                else source.dataset,
            ),
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
            preaggregation=plan_.rollup.name if plan_.rollup is not None else None,
            started_at=_instant(started),
            duration_ms=duration,
            # Rows the read may scan, an upper bound from the snapshot's statistics (#352),
            # and the cells a detail page returned (P2-24); actual rows scanned are not
            # measured, so they are left out, never guessed.
            estimate=estimated,
            actual={"cells": table.num_rows * len(physical.columns)}
            if plan_.mode == "detail"
            else None,
        ),
        result_canonical=result.canonical(engine.encoder),
        result_key=engine.result_key,
        seal_key=engine.seal_key,
        obligations=[o.for_receipt(evidence[o.id]) for o in report.obligations],
        evidence=uses,
        policies=[PolicyUse(name, "row", how) for name, how in sorted(applied.items())],
        plan_fingerprint=plan_.fingerprint,
    )
    referenced = result.with_receipt(record.receipt_id, record.reproducibility["class"])
    return Answer(referenced, record, report, plan_, physical)


def _unregistered(placement: Any, engine: Engine) -> list[str]:  # noqa: ANN401
    """The deny-list policies whose registration does not list the caller, by a count on the
    question's own snapshot: the statement already gives their rows as none, this only says
    why, with the notice `POLICY_MAPPING_MISSING`."""
    if placement is None:
        return []
    from orvex.contracts.store.v1 import Parameter, ReadRequest

    found: list[str] = []
    rules = {r.rule.name: r for rs in placement.restrictions.values() for r in rs}
    for name, restriction in sorted(rules.items()):
        registration = restriction.rule.registration
        if registration is None or restriction.no_rows:
            continue
        key = str(registration.get("attribute", "user_id"))
        attribute = placement.attributes[key]  # present and of a fitting type, or no_rows
        table = ".".join(quote(part) for part in str(registration["table"]).split("."))
        column = quote(str(registration["key"]))
        value = {"name": "p0", "type": "VARCHAR", "value": str(attribute[1])}
        request = ReadRequest(
            kind="read",
            sql=(
                f'SELECT count(*) AS "#registered" FROM {table}'
                f" WHERE CAST({column} AS VARCHAR) = $p0"
            ),
            parameters=[Parameter.model_validate(value)],
            snapshot=int(str(engine.snapshot["id"])),
            batch_size=BATCH_SIZE,
            timeout_ms=engine.timeout_ms,
        )
        counted = engine.read(request).read_all()
        if not pc.max(counted.column("#registered")).as_py():  # one scalar: a count
            found.append(name)
    return found


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
    binding: CursorBinding | None = None
    start: tuple[Any, ...] | None = None
    if bound.canonical["mode"] == "detail":
        page = bound.canonical["page"]
        unpaged = {**bound.canonical, "page": {"size": page["size"], "after": None}}
        binding = CursorBinding(  # every page of one question, for this caller, alike
            query_fingerprint=fingerprint("semantic_query", 1, _plain(unpaged)),
            model_fingerprint=compiled.model_fingerprint,
            permission_fingerprint=str(context["permission_fingerprint"]),
            page_size=int(page["size"]),
        )
        if page.get("after") is not None:  # the next page: on its own snapshot, from its row
            if engine.cursors is None:
                raise SemanticError("PAGE_CURSOR_INVALID", {"reason": "invalid"})
            state = verify_cursor(engine.cursors, page["after"], binding, _status(engine))
            pinned = engine.pinned(state.data_version) if engine.pinned else None
            if pinned is None:
                raise SemanticError("PAGE_CURSOR_INVALID", {"reason": "expired"})
            engine = replace(engine, snapshot=pinned)
            # A float as its shortest exact text: bound as text and cast back, never rounded.
            start = tuple(repr(v) if isinstance(v, float) else v for v in state.position)
    snapshot = engine.snapshot.get("id")
    ledger = Ledger(  # the records of the snapshot read, for the declarations as they are
        compiled,
        engine.evidence(engine.snapshot) if engine.evidence is not None else (),
        snapshot if isinstance(snapshot, int) and not isinstance(snapshot, bool) else -1,
    )
    rollup = next(  # the first proven table verified consistent on this snapshot (C8)
        (
            c.name
            for c in report.materials
            if c.eligible and ledger.verified(("dataset", c.dataset, CONSISTENT))
        ),
        None,
    )
    logical = plan(report, start, rollup)
    physical = compile_plan(logical)
    evidence = verify_physical(logical, physical)  # checked again, and in the statement (C9)
    uses = ledger.uses(relied(logical))  # violated evidence: refused before any read (P2-26)
    return _execute(physical, engine, report, logical, context, evidence, binding, uses)
