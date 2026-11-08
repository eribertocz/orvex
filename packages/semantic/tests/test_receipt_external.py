"""Receipts of reads on an existing DuckLake whose snapshots are no longer kept (P2-30 PR 3,
#399; ADR-0027 §5)."""

from __future__ import annotations

from orvex.semantic.receipt import ModelUse, ReceiptRecord, verify_receipt
from orvex.semantic.receipt.external import expired

from tests.support.receipts import EVERYTHING, SEAL, reads, receipt

IDENTITY = ("p1", "erp", 41, "2026-11-01T00:00:00.000000Z")


def external() -> ReceiptRecord:
    return receipt(
        data=reads(
            route="existing_ducklake",
            project="p1",
            connection="erp",
            committed_at=IDENTITY[3],
        )
    )


def test_a_record_whose_external_snapshot_is_kept_is_returned_as_it_is() -> None:
    record = external()
    asked: list[tuple[str, str, int, str]] = []

    def kept(identity: tuple[str, str, int, str]) -> bool:
        asked.append(identity)
        return True

    assert expired(record, kept, SEAL) is record
    assert asked == [IDENTITY]  # the identity in full: project, connection, id, commit time


def test_a_record_whose_external_snapshot_is_gone_becomes_explainable_and_sealed() -> None:
    record = external()
    later = expired(record, lambda _: False, SEAL)
    assert later.reproducibility == {
        "class": "explainable",
        "reasons": ["snapshot_expired"],
        "replay": "not_checked",
    }
    data = {(d["route"], d["for_policy"]): d["retained"] for d in later.document()["data"]}
    assert data == {("existing_ducklake", False): False, ("managed", True): True}
    assert verify_receipt(later.view(EVERYTHING), later, SEAL, EVERYTHING)
    assert not verify_receipt(later.view(EVERYTHING), record, SEAL, EVERYTHING)
    before, after = record.document(), later.document()
    for part in ("query", "model", "security", "execution", "execution_fingerprint", "result"):
        assert after[part] == before[part]


def test_managed_reads_and_other_reasons_are_left_as_they_are() -> None:
    managed = receipt()
    assert expired(managed, lambda _: False, SEAL) is managed
    experimental = receipt(
        data=reads(
            route="existing_ducklake", project="p1", connection="erp", committed_at=IDENTITY[3]
        ),
        model=ModelUse("sha256:" + "b" * 64, "ossie-c721ffc", experimental=True),
    )
    later = expired(experimental, lambda _: False, SEAL)
    assert later.reproducibility["reasons"] == ["snapshot_expired", "experimental_reader"]
    assert expired(later, lambda _: False, SEAL) is later  # already not retained
