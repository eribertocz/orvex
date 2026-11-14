"""A store's refusal of a lake's data, as the caller is told it (#447, `lake_refusals`)."""

from __future__ import annotations

from orvex.server.lake_refusals import as_semantic
from orvex.store.errors import StoreError


def test_a_lakes_data_refused_keeps_the_connection_and_the_reason_only() -> None:
    error = StoreError(
        "STORE_SOURCE_INVALID",
        "Connection warehouse: /secret/file is outside its location.",
        {"connection": "warehouse", "reason": "file_refused", "duckdb": "/secret/file"},
    )
    refused = as_semantic(error)
    assert refused is not None
    assert (refused.code, dict(refused.details)) == (
        "STORE_SOURCE_INVALID",
        {"connection": "warehouse", "reason": "file_refused"},
    )
    assert "secret" not in refused.to_json("en", "builder")


def test_a_refusal_without_details_still_names_something() -> None:
    refused = as_semantic(StoreError("STORE_SOURCE_INVALID", "No snapshot.", {}))
    assert refused is not None
    assert dict(refused.details) == {"connection": "unnamed", "reason": "checks_failed"}
    by_kind = as_semantic(
        StoreError("STORE_SOURCE_INVALID", "Role.", {"connection": "w", "kind": "role in use"})
    )
    assert by_kind is not None
    assert by_kind.details["reason"] == "role in use"


def test_any_other_store_error_is_not_a_refusal_of_the_data() -> None:
    for code in ("STORE_QUERY_FAILED", "STORE_TIMEOUT", "STORE_LOCATION_INVALID", "STORE_BUSY"):
        assert as_semantic(StoreError(code, "x", {"connection": "w"})) is None
