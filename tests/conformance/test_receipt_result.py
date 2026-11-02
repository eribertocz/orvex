"""A result and its receipt end to end (P2-07, #303), with the real `store/v1` encoder."""

from __future__ import annotations

import json

import pytest
from orvex.semantic.receipt import verify_replay
from orvex.semantic.result import InvalidResult, read_result
from orvex.store.json_encoding import arrow_to_json

from tests.support.receipts import EVERYTHING, RESULT, SEAL, SampleViewer, receipt
from tests.support.semantic_results import top_n_with_others


def test_a_result_references_its_receipt_and_its_replay_verifies() -> None:
    result = top_n_with_others()
    canonical = result.canonical(arrow_to_json)
    record = receipt(result_canonical=canonical)
    referenced = result.with_receipt(record.receipt_id, record.reproducibility["class"])
    assert referenced.canonical(arrow_to_json) == canonical  # the reference is not hashed
    view = record.view(SampleViewer())
    document = referenced.wire(arrow_to_json)
    view = {**view, "query_fingerprint": json.loads(document)["query_fingerprint"]}
    assert read_result(document, referenced.arrow(), view).envelope["receipt"] == {
        "receipt_id": record.receipt_id,
        "reproducibility": "exact",
    }
    replayed = verify_replay(record, top_n_with_others().canonical(arrow_to_json), RESULT, SEAL)
    assert replayed.view(EVERYTHING)["reproducibility"]["replay"] == "verified"


def test_a_result_that_names_another_receipt_is_refused() -> None:
    result = top_n_with_others()
    record = receipt(result_canonical=result.canonical(arrow_to_json))
    other = receipt(result_canonical=b'{"other":1}')
    document = result.with_receipt(other.receipt_id, "exact").wire(arrow_to_json)
    view = {
        **record.view(SampleViewer()),
        "query_fingerprint": json.loads(document)["query_fingerprint"],
    }
    with pytest.raises(InvalidResult, match="receipt"):
        read_result(document, None, view)
    without = result.wire(arrow_to_json)
    with pytest.raises(InvalidResult, match="receipt"):
        read_result(without, None, view)
    assert read_result(without).envelope.get("receipt") is None  # still valid without one
    own = result.with_receipt(record.receipt_id, "exact").wire(arrow_to_json)
    with pytest.raises(InvalidResult, match="receipt"):
        read_result(own, None, record.view(SampleViewer()))  # another query's receipt
