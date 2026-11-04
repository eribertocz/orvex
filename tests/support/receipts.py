"""Synthetic receipts for the P2-07 tests: no real data, test keys only."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from orvex.semantic.canonical import fingerprint
from orvex.semantic.receipt import (
    DataRead,
    EvidenceUse,
    ExecutionUse,
    FreshnessUse,
    Key,
    ModelUse,
    Obligation,
    PolicyUse,
    ReceiptRecord,
    build_receipt,
)

CANARY = "c4n4ry"
SEAL = Key("seal-1", b"s" * 32)
RESULT = Key("result-1", b"r" * 32)
RESULT_BYTES = b'{"rows":[["north","1250.00"]]}'

QUERY: dict[str, Any] = {
    "kind": "semantic_query",
    "version": 1,
    "mode": "aggregate",
    "metrics": [{"metric": "revenue"}, {"metric": "secret_count"}],
    "group_by": [{"member": "customers.region"}],
    "filters": [
        {
            "scope": "page",
            "member": "customers.region",
            "predicate": {"op": "in", "values": [{"type": "text", "value": f"zone-{CANARY}"}]},
        },
        {
            "scope": "visual",
            "member": "customers.national_id",
            "predicate": {"op": "is_not_null"},
        },
    ],
    "order": [{"by": "revenue", "direction": "desc"}],
}


@dataclass(frozen=True)
class SampleViewer:
    builder: bool = False
    members: frozenset[str] = frozenset({"revenue", "customers.region"})
    values: frozenset[str] = frozenset()
    datasets: frozenset[str] = frozenset({"payments", "customers"})
    policies: frozenset[str] = frozenset()

    def sees_member(self, member: str) -> bool:
        return member in self.members

    def sees_value(self, member: str, value: Mapping[str, object]) -> bool:
        return str(value.get("value")) in self.values

    def sees_dataset(self, dataset: str) -> bool:
        return dataset in self.datasets

    def knows_policy(self, name: str) -> bool:
        return name in self.policies


EVERYTHING = SampleViewer(
    builder=True,
    members=frozenset({"revenue", "secret_count", "customers.region", "customers.national_id"}),
    values=frozenset({f"zone-{CANARY}"}),
    policies=frozenset({"own_zone_only", "deny_blocked", "national_id_hidden"}),
)


def reads(**change: Any) -> list[DataRead]:  # noqa: ANN401
    managed = DataRead("payments", "managed", retained=True, snapshot_id=41)
    policy = DataRead(
        "own_zone_only", "managed", retained=True, for_policy=True, snapshot_id=40,
        table=f"security.user_zone_{CANARY}",
    )  # fmt: skip
    return [replace(managed, **change), policy]


EXECUTION = ExecutionUse(
    engine="duckdb",
    engine_version="2.0.0",
    adapter="duckdb",
    adapter_version="1",
    started_at="2026-11-02T12:00:00.000000Z",
    duration_ms=12,
    estimate={"rows_scanned": 987_654_321},
    actual={"rows_scanned": 123_456_789},
)


def receipt(**change: Any) -> ReceiptRecord:  # noqa: ANN401
    arguments: dict[str, Any] = {
        "query_canonical": QUERY,
        "query_fingerprint": fingerprint("semantic_query", 1, QUERY),
        "model": ModelUse("sha256:" + "b" * 64, "ossie-c721ffc"),
        "permission_fingerprint": "sha256:" + "c" * 64,
        "data": reads(),
        "execution": EXECUTION,
        "result_canonical": RESULT_BYTES,
        "result_key": RESULT,
        "seal_key": SEAL,
        "policies": [
            PolicyUse("own_zone_only", "row", "restricted"),
            PolicyUse("national_id_hidden", "column", "restricted"),
        ],
        "evidence": [EvidenceUse("customers", "unique_key", "verified", verified_by_run="run-7")],
        "obligations": [
            Obligation("C1", "grain_preserved", members=("revenue",), datasets=("payments",))
        ],
        "freshness": [
            FreshnessUse("payments", "2026-11-02T11:00:00.000000Z"),
            FreshnessUse("audit_log", "2026-11-02T10:00:00.000000Z"),
        ],
    }
    arguments.update(change)
    return build_receipt(**arguments)
