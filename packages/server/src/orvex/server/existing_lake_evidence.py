"""The evidence of an existing DuckLake's datasets, checked on a new snapshot before it is
served (P2-30 PR 3, #399; ADR-0027 §7).

On the managed lake, violated evidence blocks the questions that rely on it
(`EVIDENCE_VIOLATED`). An existing lake never reaches that state: every declaration of its
datasets, and of the relationships between them, is checked on each new snapshot
(`orvex.semantic.evidence.verify`, reading through the lake's guarded reads), and a snapshot
with any violation is held back by the schedule (`existing_lake_snapshots`), which keeps the
last verified one served. What no statement checks, or what does not finish within the budget,
stays `declared`, as on the managed lake.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pyarrow as pa
from orvex.contracts.store.v1 import Snapshot
from orvex.semantic.compiler.compiled import CompiledModel
from orvex.semantic.engines import Routing
from orvex.semantic.evidence import verify
from orvex.semantic.ossie import sql

BUDGET_MS = 600_000  # per snapshot: what is left over stays `declared`


def violated(
    compiled: CompiledModel,
    routing: Routing,
    connection: str,
    read: Callable[[Any], pa.RecordBatchReader],
    snapshot: Snapshot,
    at: dt.datetime,
) -> str | None:
    """What `snapshot` violates of the evidence of the datasets on `connection`, or None."""
    datasets = {
        name
        for name, entry in routing.datasets.items()
        if entry is not None and entry.connection == connection
    }
    document = compiled.ossie.document
    nodes = {key: _nodes(document.get(key)) for key in ("datasets", "relationships")}
    labels = {
        sql.normalize(str(node["name"])): str(node["name"])
        for found in nodes.values()
        for node in found
    }
    relationships = {
        sql.normalize(str(node["name"]))
        for node in nodes["relationships"]
        if {sql.normalize(str(node["from"])), sql.normalize(str(node["to"]))} <= datasets
    }
    found = verify(
        compiled, read, snapshot.id, at=at, budget_ms=BUDGET_MS, only=datasets | relationships
    )
    failed = sorted(
        f"{r.kind} of {labels.get(r.name, r.name)}" for r in found.records if r.status == "violated"
    )
    return f"evidence violated: {', '.join(failed)}" if failed else None


def _nodes(found: object) -> list[Mapping[str, Any]]:
    """The model's elements of one kind, as mappings."""
    if not isinstance(found, Sequence) or isinstance(found, str):
        return []
    return [n for n in found if isinstance(n, Mapping)]
