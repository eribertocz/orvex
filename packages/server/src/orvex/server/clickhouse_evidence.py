"""The evidence of a ClickHouse connection's datasets, verified on its schedule (P2-31 PR 4,
#225; ADR-0027 §7).

A live engine has no snapshot to hold back, so its evidence is verified on the connection's
`evidence_schedule` (`<n>h` or `<n>d`, at least one hour, default one day): every declaration of
its datasets, and of the relationships between them, checked by the ClickHouse adapter's own
statements (`orvex.semantic.adapters.clickhouse.ClickHouseChecks`) through the store's reads.
The records of the last verification hold until the next one: a question that relies on
evidence found violated is refused (`EVIDENCE_VIOLATED`), and its receipt names the
verification instant of what it relied on (`verified_at`), changes since then not yet checked.
What no statement checks, or what does not finish within the budget, stays `declared`.
"""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pyarrow as pa
from orvex.semantic.adapters.clickhouse import ClickHouseChecks
from orvex.semantic.compiler.compiled import CompiledModel
from orvex.semantic.engines import Routing
from orvex.semantic.evidence import EvidenceRecord, verify
from orvex.semantic.external import LIVE
from orvex.semantic.ossie import sql
from orvex.server.existing_lake_snapshots import period
from orvex.store.clickhouse import ClickHouse

MINIMUM = dt.timedelta(hours=1)
DEFAULT_SCHEDULE = "1d"
BUDGET_MS = 600_000  # per verification: what is left over stays `declared`


def checked(
    compiled: CompiledModel,
    routing: Routing,
    connection: str,
    engine: ClickHouse,
    columns: Mapping[str, Sequence[str]],
    at: dt.datetime,
) -> tuple[EvidenceRecord, ...]:
    """Every declaration of the datasets on `connection`, and of the relationships between
    them, checked on the engine now."""
    datasets = {
        name
        for name, entry in routing.datasets.items()
        if entry is not None and entry.connection == connection
    }
    relationships = {
        sql.normalize(str(node["name"]))
        for node in _nodes(compiled.ossie.document.get("relationships"))
        if {sql.normalize(str(node["from"])), sql.normalize(str(node["to"]))} <= datasets
    }
    checks = ClickHouseChecks(columns)

    def read(request: Any) -> pa.RecordBatchReader:  # noqa: ANN401 - a `store/v1` `ReadRequest`
        counts = [(name, "BIGINT") for name in checks.written[request.sql]]
        return engine.read(request.sql, request.parameters, counts, request.timeout_ms)

    found = verify(
        compiled,
        read,
        LIVE,
        at=at,
        budget_ms=BUDGET_MS,
        only=datasets | relationships,
        checks=checks,
    )
    return found.records


class EvidenceSchedule:
    """The evidence records of one ClickHouse connection, verified when due."""

    def __init__(
        self,
        connection: str,
        schedule: str | None,
        verification: Callable[[dt.datetime], tuple[EvidenceRecord, ...]],
    ) -> None:
        self.connection = connection
        self.every = period(schedule or DEFAULT_SCHEDULE, minimum=MINIMUM)
        self._verification = verification
        self._lock = threading.Lock()
        self._records: tuple[EvidenceRecord, ...] = ()
        self._checked: dt.datetime | None = None

    def due(self, now: dt.datetime) -> bool:
        return self._checked is None or now - self._checked >= self.every

    def check(self, now: dt.datetime) -> tuple[EvidenceRecord, ...]:
        """One verification; its records replace the last ones. A failed one keeps none, so
        nothing stays `verified` on records that could not be renewed."""
        with self._lock:
            self._checked = now
            self._records = ()
            self._records = self._verification(now)
            return self._records

    def records(self) -> tuple[EvidenceRecord, ...]:
        with self._lock:
            return self._records


def _nodes(found: object) -> list[Mapping[str, Any]]:
    if not isinstance(found, Sequence) or isinstance(found, str):
        return []
    return [n for n in found if isinstance(n, Mapping)]
