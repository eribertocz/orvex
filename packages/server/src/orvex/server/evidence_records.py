"""Evidence records read back from the lake (P2-26b, #355; split out in P2-27a, #363).

Reading the records a question relies on, and the store's proof that an earlier record still
holds, is all a question needs. Checking evidence and writing records is `evidence_runs`, which
holds the CLI's builder grant (#358); the HTTP API imports only this module, so that grant
never reaches it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from orvex.contracts.store.v1 import ChangesRequest, ReadRequest
from orvex.semantic.evidence import EvidenceRecord
from orvex.store.errors import StoreError
from orvex.store.lake import Lake

RECORDS = "_meta.evidence_records"
CHECKED = "_meta.evidence_checked"


def has_tables(lake: Lake, snapshot: int) -> bool:
    from orvex.contracts.store.v1 import TablesRequest

    found = lake.table_columns(TablesRequest(kind="tables", snapshot=snapshot))
    names = {f"{t.schema_}.{t.table}".casefold() for t in found.tables}
    return {RECORDS.casefold(), CHECKED.casefold()} <= names


def records_at(lake: Lake, snapshot: int) -> list[EvidenceRecord]:
    """The newest record of each declaration written up to `snapshot`, as of `snapshot`."""
    if not has_tables(lake, snapshot):
        return []
    rows = (
        lake.read(
            ReadRequest(
                kind="read",
                sql=(
                    "SELECT element, name, kind, snapshot, declaration, status, violations, "
                    "tables, strftime(checked_at AT TIME ZONE 'UTC', '%Y-%m-%dT%H:%M:%S.%f') "
                    f"AS checked_at, run_id FROM {RECORDS} "
                    "QUALIFY row_number() OVER (PARTITION BY element, name, kind, declaration "
                    "ORDER BY snapshot DESC, checked_at DESC) = 1"
                ),
                parameters=[],
                snapshot=snapshot,
                batch_size=1000,
            )
        )
        .read_all()
        .to_pylist()
    )
    return [
        EvidenceRecord(
            str(r["element"]),
            str(r["name"]),
            str(r["kind"]),
            int(r["snapshot"]),
            str(r["declaration"]),
            "verified" if r["status"] == "verified" else "violated",
            {str(k): int(v) for k, v in json.loads(str(r["violations"])).items()},
            datetime.fromisoformat(str(r["checked_at"])).replace(tzinfo=UTC),
            None if r["run_id"] is None else str(r["run_id"]),
            tuple(str(t) for t in json.loads(str(r["tables"]))),
        )
        for r in rows
    ]


def unchanged(lake: Lake) -> Callable[[EvidenceRecord, int], bool]:
    """The store's proof that no table a record's check read changed after the record's
    snapshot up to `snapshot`; a table it cannot answer for is changed (fail closed)."""

    def proof(record: EvidenceRecord, snapshot: int) -> bool:
        if not record.tables or record.snapshot > snapshot:
            return False
        try:
            found = lake.changes(
                ChangesRequest.model_validate(
                    {
                        "kind": "changes",
                        "tables": list(record.tables),
                        "since": record.snapshot,
                        "snapshot": snapshot,
                    }
                )
            )
        except StoreError, ValueError:
            return False
        return not any(t.changed for t in found.tables)

    return proof


def engine_evidence(
    lake: Lake,
) -> tuple[
    Callable[[Mapping[str, object]], list[EvidenceRecord]], Callable[[EvidenceRecord, int], bool]
]:
    """What `server` gives the semantic engine (`Engine.evidence`, `Engine.unchanged`) for a
    question on a snapshot: the records written up to it, and the proof that an earlier
    record still holds there."""

    def evidence(snapshot: Mapping[str, object]) -> list[EvidenceRecord]:
        at = snapshot.get("id")
        return records_at(lake, at) if isinstance(at, int) else []

    return evidence, unchanged(lake)
