"""The existing-DuckLake engine as the semantic engine uses it (P2-30 PR 3, #399; ADR-0027 §3,
§5, §8): `orvex.semantic.engines.ExternalAdapter` for one enabled connection.

- `pin`: the connection's served snapshot (`existing_lake_snapshots`), fixed once per question
  and named by its receipt with the project, the connection, its id and its commit time.
- `compile`: the semantic engine's DuckDB adapter, unchanged: the lake's dialect is DuckDB SQL,
  never translated, and the statement names datasets' tables as the managed lake's do.
- `read`: the store's read of that statement at the pinned snapshot (`existing_lake_reads`),
  behind its statement guard, under the question's time limit. A read that finds the pinned
  snapshot changed tells the schedule, which treats the lake as new, before the error goes on.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Any

import pyarrow as pa
from orvex.contracts.store.v1 import Parameter, ReadRequest
from orvex.semantic.adapters.duckdb import PhysicalQuery, PolicyValues, compile_plan
from orvex.server.existing_lake_snapshots import SnapshotSchedule
from orvex.store.errors import StoreError
from orvex.store.existing_lake_reads import SNAPSHOT_CHANGED, LakeReads

ENGINE = "ducklake"


class ExistingLakeAdapter:
    """One enabled existing-DuckLake connection."""

    engine = ENGINE

    def __init__(self, reads: LakeReads, schedule: SnapshotSchedule, engine_version: str) -> None:
        self.connection = schedule.connection
        self.engine_version = engine_version
        self._reads = reads
        self._schedule = schedule

    def pin(self) -> Mapping[str, object]:
        return self._schedule.pin()

    def compile(self, plan: Any, values: Mapping[int, PolicyValues]) -> PhysicalQuery:  # noqa: ANN401 - a `SemanticPlan`
        return compile_plan(plan, values)

    def read(
        self,
        statement: str,
        parameters: Sequence[Mapping[str, str | None]],
        version: Mapping[str, object],
        batch_size: int,
        timeout_ms: int | None,
    ) -> pa.RecordBatchReader:
        request = ReadRequest(
            kind="read",
            sql=statement,
            parameters=[Parameter.model_validate(dict(p)) for p in parameters],
            snapshot=int(str(version["id"])),
            batch_size=batch_size,
            timeout_ms=timeout_ms,
        )
        committed = dt.datetime.fromisoformat(str(version["committed_at"]))
        try:
            return self._reads.read(request, committed)
        except StoreError as error:
            if error.details.get("reason") == SNAPSHOT_CHANGED:
                self._schedule.restored()
            raise
