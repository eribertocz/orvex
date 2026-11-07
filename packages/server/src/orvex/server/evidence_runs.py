"""Evidence after runs and on demand, kept as `semantic` state in the lake (P2-26b, #355).

After a successful run, the evidence of every dataset the run wrote, and of every relationship
touching one, is checked on the snapshot the run committed (`semantic.evidence.verify`). A
failed run commits nothing, so nothing is checked. A builder can also ask for one dataset's
evidence. In the CLI, local mode, whoever opens the project is its builder, and that grant
lives only here; the HTTP API checks roles through `access` (P2-27).

Records are written to the lake's `_meta` schema, next to the run records, never among the
datasets. They are keyed by (element, name, kind, snapshot, declaration fingerprint). Writing
them commits a snapshot of their own, which changes no dataset. A question on that later
snapshot uses a record of an earlier one only when the store proves that no table its check
read changed since (`unchanged`, `store/v1` `ChangesRequest`).

`append_only` compares a dataset's snapshot with the last one its evidence was checked on
(`_meta.evidence_checked`). The first check of a dataset has nothing to compare against, so
`append_only` stays `declared`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from orvex.contracts.store.v1 import (
    Parameter,
    ParameterType,
    ReadRequest,
    StatementRequest,
)
from orvex.semantic.evidence import EvidenceRecord, Skipped, declarations, verify
from orvex.semantic.ossie import sql
from orvex.server.evidence_records import (
    CHECKED,
    RECORDS,
    engine_evidence,
    has_tables,
    records_at,
    unchanged,
)
from orvex.server.problems import Problem
from orvex.server.semantic_model import SemanticModel
from orvex.store.errors import StoreError
from orvex.store.lake import Lake

BUDGET_MS = 600_000  # per verification of a project: what is left over stays `declared`

_RECORD_COLUMNS = (
    ("element", "VARCHAR"),
    ("name", "VARCHAR"),
    ("kind", "VARCHAR"),
    ("snapshot", "BIGINT"),
    ("declaration", "VARCHAR"),
    ("status", "VARCHAR"),
    ("violations", "VARCHAR"),  # JSON object: count by kind
    ("tables", "VARCHAR"),  # JSON array: every lake table the check read
    ("checked_at", "TIMESTAMPTZ"),
    ("run_id", "VARCHAR"),
)
_CHECKED_COLUMNS = (("dataset", "VARCHAR"), ("snapshot", "BIGINT"))


@dataclass(frozen=True, slots=True)
class Outcome:
    snapshot: int
    records: tuple[EvidenceRecord, ...] = ()
    skipped: tuple[Skipped, ...] = ()
    problems: tuple[Problem, ...] = ()

    def lines(self) -> list[str]:
        """What a builder reads: the counts, then each violation and each check left
        `declared`, with why."""
        verified = sum(1 for r in self.records if r.status == "verified")
        violated = [r for r in self.records if r.status == "violated"]
        found = [
            f"evidence at snapshot {self.snapshot}: {verified} verified, {len(violated)} "
            f"violated, {len(self.skipped)} declared"
        ]
        found += [
            f"EVIDENCE_VIOLATED {r.name.lower()} {r.kind} "
            + ", ".join(f"{k} {v}" for k, v in sorted(r.violations.items()) if v)
            for r in violated
        ]
        why = {"budget": "over the time budget", "unsupported": "not checkable", "first": "no "
               "earlier snapshot to compare against"}  # fmt: skip
        found += [
            f"declared {s.declaration.name.lower()} {s.declaration.kind}: {why[s.reason]}"
            for s in self.skipped
        ]
        return found + [str(p) for p in self.problems]


def _parameter(name: str, kind: str, value: str | None) -> Parameter:
    return Parameter(name=name, type=ParameterType(kind), value=value)


def _statement(text: str, parameters: Sequence[Parameter] = ()) -> StatementRequest:
    return StatementRequest(kind="statement", sql=text, parameters=list(parameters))


def _tables() -> list[StatementRequest]:
    def columns(found: Sequence[tuple[str, str]]) -> str:
        return ", ".join(f"{name} {kind}" for name, kind in found)

    return [
        _statement("CREATE SCHEMA IF NOT EXISTS _meta"),
        _statement(f"CREATE TABLE IF NOT EXISTS {RECORDS} ({columns(_RECORD_COLUMNS)})"),
        _statement(f"CREATE TABLE IF NOT EXISTS {CHECKED} ({columns(_CHECKED_COLUMNS)})"),
    ]


def store_statements(
    records: Sequence[EvidenceRecord], checked: Collection[str], snapshot: int
) -> list[StatementRequest]:
    """The tables when missing, every record, and the datasets checked at `snapshot`."""
    statements = _tables()
    placeholders = ", ".join(f"$p_{name}" for name, _ in _RECORD_COLUMNS)
    for record in records:
        values = {
            "element": record.element,
            "name": record.name,
            "kind": record.kind,
            "snapshot": str(record.snapshot),
            "declaration": record.declaration,
            "status": record.status,
            "violations": json.dumps(dict(sorted(record.violations.items()))),
            "tables": json.dumps(list(record.tables)),
            "checked_at": record.at.astimezone(UTC).isoformat(),
            "run_id": record.run,
        }
        statements.append(
            _statement(
                f"INSERT INTO {RECORDS} VALUES ({placeholders})",
                [_parameter(f"p_{n}", k, values[n]) for n, k in _RECORD_COLUMNS],
            )
        )
    for dataset in sorted(checked):
        statements.append(
            _statement(
                f"INSERT INTO {CHECKED} VALUES ($p_dataset, $p_snapshot)",
                [
                    _parameter("p_dataset", "VARCHAR", dataset),
                    _parameter("p_snapshot", "BIGINT", str(snapshot)),
                ],
            )
        )
    return statements


def baselines(lake: Lake, snapshot: int) -> dict[str, int]:
    """Each dataset's last snapshot before `snapshot` its evidence was checked on."""
    if not has_tables(lake, snapshot):
        return {}
    rows = (
        lake.read(
            ReadRequest(
                kind="read",
                sql=f"SELECT dataset, max(snapshot) AS at FROM {CHECKED} "
                "WHERE snapshot < $p_snapshot GROUP BY dataset",
                parameters=[_parameter("p_snapshot", "BIGINT", str(snapshot))],
                snapshot=snapshot,
                batch_size=1000,
            )
        )
        .read_all()
        .to_pylist()
    )
    return {str(r["dataset"]): int(r["at"]) for r in rows}


def items(document: Mapping[str, object], key: str) -> list[Mapping[str, object]]:
    found = document.get(key, ())
    return [d for d in found if isinstance(d, Mapping)] if isinstance(found, Sequence) else []


def touching(model: SemanticModel, written: Collection[str]) -> tuple[set[str], set[str]]:
    """The datasets whose source table is among the tables `written` (as the model names them,
    case aside), and every name to check: those datasets, and the relationships that touch
    one of them."""
    tables = {t.casefold() for t in written}
    document = model.compiled.ossie.document
    datasets = {
        sql.normalize(str(d["name"]))
        for d in items(document, "datasets")
        if str(d.get("source", d["name"])).casefold() in tables
        or f"main.{d.get('source', d['name'])}".casefold() in tables
    }
    relationships = {
        sql.normalize(str(r["name"]))
        for r in items(document, "relationships")
        if sql.normalize(str(r["from"])) in datasets or sql.normalize(str(r["to"])) in datasets
    }
    return datasets, datasets | relationships


def verify_and_store(
    model: SemanticModel,
    lake: Lake,
    *,
    datasets: Collection[str] | None,
    run: str | None = None,
    budget_ms: int = BUDGET_MS,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Outcome:
    """The evidence of `datasets` (normalized names; none: every one) checked on the model's
    snapshot and written to the lake; `run` names the run that asked, none for a builder. A
    failure to write records leaves every piece `declared` and is reported, never raised."""
    snapshot = model.snapshot
    compiled = model.compiled
    if datasets is None:
        chosen = {d.name for d in declarations(compiled)}
        checked = {d.name for d in declarations(compiled) if d.element == "dataset"}
    else:
        checked = {sql.normalize(d) for d in datasets}
        relationships = {
            sql.normalize(str(r["name"]))
            for r in items(compiled.ossie.document, "relationships")
            if sql.normalize(str(r["from"])) in checked or sql.normalize(str(r["to"])) in checked
        }
        chosen = checked | relationships
    try:
        found = verify(
            compiled,
            lake.read,
            snapshot,
            at=now(),
            run=run,
            budget_ms=budget_ms,
            only=chosen,
            changes=lake.changes,
            baselines=baselines(lake, snapshot),
        )
    except StoreError as error:
        problem = Problem(error.code, ".", None, f"evidence not checked: {error.message}")
        return Outcome(snapshot, problems=(problem,))
    try:
        with lake.transaction() as transaction:
            for statement in store_statements(found.records, checked, snapshot):
                transaction.execute(statement)
    except StoreError as error:
        problem = Problem(error.code, ".", None, f"evidence records not written: {error.message}")
        return Outcome(snapshot, skipped=found.skipped, problems=(problem,))
    return Outcome(snapshot, found.records, found.skipped)


__all__ = [
    "CHECKED",
    "RECORDS",
    "Outcome",
    "baselines",
    "engine_evidence",
    "items",
    "records_at",
    "store_statements",
    "touching",
    "unchanged",
    "verify_and_store",
]
