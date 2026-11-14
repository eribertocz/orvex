"""The result cache on the engines other than the managed lake (#444 AC2, AC5): an existing
DuckLake's verified snapshot is in the key, so its owner's change (a new snapshot, verified)
misses; a live engine (ClickHouse, CI) has no retained version, so every question bypasses the
cache and nothing is stored.

On the `direct-query` fixture, with a caller in `operations` who sees every zone.
"""

from __future__ import annotations

from typing import Any

import pytest
from orvex.semantic.receipt import DataRead
from orvex.semantic.result_cache import ResultCache, use_of

from conformance.runners import corpus_execution as ce
from conformance.runners import security_fixtures as sf
from tests.support.security_engines import ask, served
from tests.support.security_sequences import context_for

FOUND = sf.fixture("direct-query", ce.VENDORS[0])
CALLER = {"kind": "security_context", "version": 1, "user": "u-open", "project": "direct-query",
          "role": "author", "groups": ["operations"], "attributes": {},
          "permission_fingerprint": "sha256:" + "0" * 64}  # fmt: skip
BY_ZONE = {"kind": "semantic-query", "body": {
    "kind": "semantic_query", "version": 1, "mode": "aggregate",
    "metrics": [{"metric": "outage_minutes"}], "group_by": [{"member": "outages.zone_id"}],
    "order": [{"by": "outages.zone_id", "direction": "asc"}]}}  # fmt: skip


def origin(place: Any, request: Any) -> tuple[str, list[Any] | None, int]:  # noqa: ANN401
    """The cache origin, the rows and the data version read, for the caller now."""
    received, found = ask(FOUND, place, request, context_for(FOUND, place, CALLER, {}), whole=True)
    assert received.code is None, received
    document = found.receipt.document()
    snapshot = max(int(d["snapshot_id"]) for d in document["data"] if not d["for_policy"])
    return str(document["execution"]["cache"]), received.rows, snapshot


@pytest.mark.parametrize("catalog", ["sqlite", "postgres"])
def test_an_existing_ducklakes_new_verified_snapshot_misses(catalog: str) -> None:
    with served(FOUND, "existing-ducklake", catalog) as place:
        place.cache = ResultCache()
        first, second = origin(place, BY_ZONE), origin(place, BY_ZONE)
        assert (first[0], second[0]) == ("miss", "hit")
        assert first[1:] == second[1:]
        assert place.delete is not None
        place.delete("outages", {"outage_id": 3})  # by its owner, then verified on schedule
        after = origin(place, BY_ZONE)
        assert after[0] == "miss"
        assert after[2] != first[2]  # the new verified snapshot
        place.cache = None
        assert origin(place, BY_ZONE)[1:] == after[1:]  # as a cache-free engine answers


def test_a_live_engine_always_bypasses_and_stores_nothing() -> None:
    with served(FOUND, "clickhouse", "") as place:
        place.cache = ResultCache()
        assert [origin(place, BY_ZONE)[0] for _ in range(2)] == ["bypass", "bypass"]
        assert len(place.cache) == 0


def test_a_read_without_a_retained_version_has_no_key() -> None:
    live = DataRead(name="outages", route="direct_query", retained=False)
    assert (
        use_of(FOUND.compiled, ["outages"], "sha256:" + "1" * 64, "SELECT 1", (), [live]).key
        is None
    )
