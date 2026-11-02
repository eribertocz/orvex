"""Receipts (P2-07, #303): identity and seal, execution identity, derived reproducibility,
replay, views by allow-list, and nothing protected in any view."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from orvex.semantic.canonical import canonicalize, fingerprint
from orvex.semantic.receipt import (
    NO_ONE,
    InvalidReceipt,
    Key,
    ModelUse,
    PolicyUse,
    ReceiptRecord,
    verify_receipt,
    verify_replay,
)
from orvex.semantic.receipt.building import schema_validator

from tests.support.receipts import (
    CANARY,
    EVERYTHING,
    EXECUTION,
    QUERY,
    RESULT,
    RESULT_BYTES,
    SEAL,
    SampleViewer,
    reads,
    receipt,
)

ROOT = Path(__file__).resolve().parents[3]


def refused(**change: Any) -> str:  # noqa: ANN401
    with pytest.raises(InvalidReceipt) as caught:
        receipt(**change)
    return caught.value.reason


# --- identity and authentication -----------------------------------------------------------


def test_the_receipt_id_identifies_the_record() -> None:
    record = receipt()
    assert record.receipt_id == fingerprint("receipt", 1, record.document())
    assert receipt().receipt_id == record.receipt_id
    assert receipt(execution=replace(EXECUTION, duration_ms=13)).receipt_id != record.receipt_id


def test_a_view_is_genuine_only_with_its_seal() -> None:
    record = receipt()
    viewer = SampleViewer()
    view = record.view(viewer)
    assert verify_receipt(view, record, SEAL, viewer)
    forged = {**view, "reproducibility": {"class": "exact", "replay": "verified"}}
    assert not verify_receipt(forged, record, SEAL, viewer)
    other_key = Key("seal-1", b"x" * 32)
    assert not verify_receipt(view, record, other_key, viewer)
    other = receipt(execution=replace(EXECUTION, duration_ms=99))
    assert not verify_receipt({**view, "seal": other.view(viewer)["seal"]}, record, SEAL, viewer)
    stripped = {k: v for k, v in view.items() if k != "seal"}
    assert not verify_receipt(stripped, record, SEAL, viewer)


def test_a_recomputed_receipt_id_does_not_make_an_edited_view_genuine() -> None:
    record = receipt(data=reads(retained=False))
    viewer = SampleViewer()
    edited = record.view(viewer)
    edited["reproducibility"] = {"class": "exact", "replay": "not_checked"}
    edited["receipt_id"] = fingerprint("receipt", 1, edited)
    assert not verify_receipt(edited, record, SEAL, viewer)


# --- execution identity ---------------------------------------------------------------------


def test_the_execution_identity_changes_with_anything_that_can_change_a_number() -> None:
    base = receipt().document()["execution_fingerprint"]
    changes: list[dict[str, Any]] = [
        {"model": ModelUse("sha256:" + "d" * 64, "ossie-c721ffc")},
        {"permission_fingerprint": "sha256:" + "e" * 64},
        {"data": reads(snapshot_id=42)},
        {"execution": replace(EXECUTION, engine_version="2.0.1")},
        {"execution": replace(EXECUTION, adapter_version="2")},
    ]
    for change in changes:
        assert receipt(**change).document()["execution_fingerprint"] != base, change
    other_query = {**QUERY, "mode": "aggregate", "metrics": [{"metric": "revenue"}]}
    assert (
        receipt(
            query_canonical=other_query,
            query_fingerprint=fingerprint("semantic_query", 1, other_query),
        ).document()["execution_fingerprint"]
        != base
    )
    same_cases: list[dict[str, Any]] = [
        {"execution": replace(EXECUTION, cache="hit")},
        {"execution": replace(EXECUTION, duration_ms=999)},
        {"result_canonical": b'{"other":true}'},
        {"data": list(reversed(reads()))},
    ]
    for same in same_cases:
        assert receipt(**same).document()["execution_fingerprint"] == base, same


# --- reproducibility --------------------------------------------------------------------------


def test_reproducibility_is_derived_and_retained_is_not_guaranteed() -> None:
    assert receipt().reproducibility == {"class": "exact", "replay": "not_checked"}
    cases: dict[str, dict[str, Any]] = {
        "snapshot_expired": {"data": reads(retained=False)},
        "live_read": {
            "data": reads(
                route="live",
                snapshot_id=None,
                retained=False,
                read_at="2026-11-02T12:00:00.000000Z",
            )
        },
        "experimental_reader": {
            "model": ModelUse("sha256:" + "b" * 64, "ossie-next", experimental=True)
        },
        "engine_unavailable": {"execution": replace(EXECUTION, engine_available=False)},
    }
    for expected, change in cases.items():
        found = receipt(**change).reproducibility
        assert found["class"] == "explainable", expected
        assert expected in found["reasons"]
        assert found["replay"] == "not_checked"
    with pytest.raises(TypeError):
        receipt(reproducibility="exact")


def test_only_a_replay_verifies_and_a_single_changed_cell_fails() -> None:
    record = receipt()
    verified = verify_replay(record, RESULT_BYTES, RESULT, SEAL)
    assert verified.reproducibility == {"class": "exact", "replay": "verified"}
    assert verified.receipt_id != record.receipt_id
    one_cell = RESULT_BYTES.replace(b"1250.00", b"1250.01")
    assert verify_replay(record, one_cell, RESULT, SEAL).reproducibility["replay"] == "failed"
    signed_zero = b'{"rows":[["-0.0"]]}'
    zero = receipt(result_canonical=b'{"rows":[["0.0"]]}')
    assert verify_replay(zero, signed_zero, RESULT, SEAL).reproducibility["replay"] == "failed"
    assert (
        verify_replay(record, RESULT_BYTES, Key("result-2", b"r" * 32), SEAL).reproducibility[
            "replay"
        ]
        == "failed"
    )
    with pytest.raises(InvalidReceipt, match="replay"):
        verify_replay(receipt(data=reads(retained=False)), RESULT_BYTES, RESULT, SEAL)


# --- views ------------------------------------------------------------------------------------


def test_the_summary_view_holds_only_its_members() -> None:
    view = receipt().view(SampleViewer())
    assert set(view) == {
        "kind", "version", "view", "receipt_id", "seal", "query_fingerprint", "question",
        "security", "freshness", "reproducibility", "cache",
    }  # fmt: skip
    assert view["view"] == "summary"
    assert not list(schema_validator().iter_errors(view))


def test_the_question_is_restricted_to_what_the_viewer_may_know() -> None:
    view = receipt().view(SampleViewer())
    assert view["question"] == {
        "mode": "aggregate",
        "metrics": [{"metric": "revenue"}],
        "group_by": [{"member": "customers.region"}],
        "filters": [{"scope": "page", "member": "customers.region", "withheld": True}],
        "order": [{"by": "revenue", "direction": "desc"}],
        "withheld": True,
    }
    seen = receipt().view(replace(SampleViewer(), values=frozenset({f"zone-{CANARY}"})))
    assert seen["question"]["filters"][0]["predicate"]["values"][0]["value"] == f"zone-{CANARY}"


def test_a_builder_without_visibility_does_not_see_a_member_or_a_policy() -> None:
    builder = SampleViewer(builder=True)
    view = receipt().view(builder)
    assert view["view"] == "builder"
    text = json.dumps(view)
    assert "national_id" not in text
    assert "secret_count" not in text
    assert view["security"] == {"restricted": True, "policies": []}
    assert [read["name"] for read in view["data"]] == ["payments"]
    assert not list(schema_validator().iter_errors(view))


def test_policies_and_exemptions_appear_only_to_a_viewer_who_may_know_them() -> None:
    record = receipt(
        policies=[
            PolicyUse("own_zone_only", "row", "exempt"),
            PolicyUse("deny_blocked", "row", "no_rows"),
        ]
    )
    assert record.view(SampleViewer())["security"] == {"restricted": True, "policies": []}
    knows = SampleViewer(policies=frozenset({"own_zone_only"}))
    assert record.view(knows)["security"]["policies"] == [
        {"name": "own_zone_only", "kind": "row", "applied": "exempt"}
    ]


@pytest.mark.parametrize("viewer", [None, object(), "builder", {"builder": True}])
def test_a_malformed_viewer_gets_the_summary_view_with_nothing_visible(viewer: object) -> None:
    view = receipt().view(viewer)
    assert view["view"] == "summary"
    assert view["question"] == {"mode": "aggregate", "withheld": True}
    assert view["freshness"] == []
    assert view == receipt().view(NO_ONE)


def test_a_viewer_that_fails_sees_nothing() -> None:
    class Broken(SampleViewer):
        def sees_member(self, member: str) -> bool:
            raise RuntimeError(member)

    view = receipt().view(Broken(builder=True))
    assert "metrics" not in view["question"]


def test_a_new_internal_field_reaches_no_view_unless_allowed() -> None:
    record = receipt()
    document = record.document()
    document["execution"]["sql"] = "SELECT internal_canary"
    document["internal_note"] = "internal_canary"
    widened = ReceiptRecord(document, SEAL)
    for viewer in (SampleViewer(), EVERYTHING):
        assert "internal_canary" not in json.dumps(widened.view(viewer))


# --- nothing protected in any view ----------------------------------------------------------


def test_no_view_holds_protected_information() -> None:
    record = receipt()
    for viewer in (NO_ONE, SampleViewer(), SampleViewer(builder=True), EVERYTHING):
        text = json.dumps(record.view(viewer))
        for canary in (f"user_zone_{CANARY}", "987654321", "123456789"):
            assert canary not in text, (viewer, canary)
        if viewer is not EVERYTHING:
            assert f"zone-{CANARY}" not in text
    unrestricted = receipt(policies=[]).view(EVERYTHING)
    assert unrestricted["execution"]["estimate"] == {"rows_scanned": 987_654_321}


def test_the_summary_view_names_no_snapshot_or_engine() -> None:
    text = json.dumps(receipt().view(SampleViewer()))
    for internal in ('"snapshot"', '"engine"', "duckdb", '"plan"', "run-7", "41"):
        assert internal not in text, internal


def test_freshness_names_only_datasets_the_viewer_sees() -> None:
    assert [f["dataset"] for f in receipt().view(SampleViewer())["freshness"]] == ["payments"]


# --- fail closed --------------------------------------------------------------------------


def test_each_refusal_of_build_receipt() -> None:
    assert refused(query_fingerprint="sha256:" + "0" * 64) == "fingerprint"
    assert refused(permission_fingerprint="sha256:" + "A" * 64) == "fingerprint"
    assert refused(data=[]) == "data"
    assert refused(data=reads(route="ftp")) == "data_route"
    assert refused(data=reads(snapshot_id=None)) == "data_route"
    assert (
        refused(data=reads(route="live", snapshot_id=None, retained=True, read_at="x"))
        == "data_route"
    )
    assert refused(data=reads(route="existing_ducklake")) == "data_route"
    assert refused(policies=[PolicyUse("zone_id IN (SELECT 1)", "row", "restricted")]) == "policy"
    assert refused(policies=[PolicyUse("own_zone_only", "row", "widened")]) == "policy"
    assert refused(execution=replace(EXECUTION, started_at="yesterday")) == "instant"
    assert refused(execution=replace(EXECUTION, estimate={"table_size": 1})) == "execution"
    assert refused(result_canonical=b"") == "result"
    with pytest.raises(InvalidReceipt, match="key"):
        Key("", b"k" * 32)
    with pytest.raises(InvalidReceipt, match="key"):
        Key("k", b"short")


def test_an_existing_ducklake_snapshot_is_named_in_full() -> None:
    external = reads(
        route="existing_ducklake",
        project="p1",
        connection="erp",
        committed_at="2026-11-01T00:00:00.000000Z",
    )
    view = receipt(data=external).view(EVERYTHING)
    assert view["data"][0]["snapshot"] == {
        "id": 41,
        "committed_at": "2026-11-01T00:00:00.000000Z",
        "project": "p1",
        "connection": "erp",
    }


# --- determinism ----------------------------------------------------------------------------

SCRIPT = """
import sys
sys.path.insert(0, {root!r})
from tests.support.receipts import receipt, EVERYTHING
from orvex.semantic.canonical import canonicalize
import hashlib
r = receipt()
print(hashlib.sha256(r.canonical() + canonicalize(r.view(EVERYTHING))).hexdigest())
"""
GOLDEN = ROOT / "tests/golden/receipt-canonical.sha256"


def digest() -> str:
    import hashlib

    record = receipt()
    return hashlib.sha256(record.canonical() + canonicalize(record.view(EVERYTHING))).hexdigest()


def test_canonical_bytes_are_equal_across_processes_and_hash_seeds() -> None:
    here = digest()
    for seed in ("0", "1", "4242"):
        run = subprocess.run(
            [sys.executable, "-c", SCRIPT.format(root=str(ROOT))],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
            cwd=ROOT,
            check=False,
        )
        assert run.returncode == 0, run.stderr[-2000:]
        assert run.stdout.strip() == here
    assert GOLDEN.read_text("ascii").strip() == here


def test_the_package_reads_the_contracts_own_schema() -> None:
    packaged = ROOT / "packages/semantic/src/orvex/semantic/receipt/receipt-v1.json"
    assert packaged.read_bytes() == (ROOT / "contracts/receipt/v1/schema.json").read_bytes()
