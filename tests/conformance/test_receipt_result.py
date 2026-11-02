"""A result and its receipt end to end (P2-07, #303), with the real `store/v1` encoder: the
receipt is built for the result's own query, and its view is checked on the server."""

from __future__ import annotations

import json

import pytest
from orvex.semantic.canonical import fingerprint
from orvex.semantic.receipt import verify_receipt, verify_replay
from orvex.semantic.result import InvalidResult, SemanticResult, build_result, read_result
from orvex.store.json_encoding import arrow_to_json

from tests.support.receipts import EVERYTHING, QUERY, RESULT, SEAL, SampleViewer, receipt
from tests.support.semantic_results import MODEL, top_n_with_others

QUERY_FINGERPRINT = fingerprint("semantic_query", 1, QUERY)


def result_for_query() -> SemanticResult:
    """`top_n_with_others`, answered for the receipt fixture's own query."""
    sample = top_n_with_others()
    envelope = sample.envelope()
    from tests.support.semantic_results import decimal, spec, text

    columns = [
        text("region", role="dimension"),
        decimal("revenue", 18, 2, role="metric", member={"name": "revenue"}),
        spec("growth", "DOUBLE", {"family": "Float"}, role="metric", member={"name": "growth"}),
    ]
    return build_result(
        sample.arrow(),
        columns,
        mode="aggregate",
        query_fingerprint=QUERY_FINGERPRINT,
        model_fingerprint=MODEL,
        page=envelope["page"],
        cell_states=sample.cell_states,
        others_row=sample.others_row,
    )


def test_a_result_references_its_receipt_and_its_replay_verifies() -> None:
    result = result_for_query()
    canonical = result.canonical(arrow_to_json)
    record = receipt(result_canonical=canonical)
    referenced = result.with_receipt(record.receipt_id, record.reproducibility["class"])
    assert referenced.canonical(arrow_to_json) == canonical  # the reference is not hashed
    viewer = SampleViewer()
    view = record.view(viewer)
    assert verify_receipt(view, record, SEAL, viewer)
    document = referenced.wire(arrow_to_json)
    assert read_result(document, referenced.arrow(), view).envelope["receipt"] == {
        "receipt_id": record.receipt_id,
        "reproducibility": "exact",
    }
    replayed = verify_replay(record, result_for_query().canonical(arrow_to_json), RESULT, SEAL)
    assert replayed.view(EVERYTHING)["reproducibility"]["replay"] == "verified"


def test_a_result_that_names_another_receipt_is_refused() -> None:
    result = result_for_query()
    record = receipt(result_canonical=result.canonical(arrow_to_json))
    view = record.view(SampleViewer())
    other = receipt(result_canonical=b'{"other":1}')
    with pytest.raises(InvalidResult, match="receipt"):
        read_result(result.with_receipt(other.receipt_id, "exact").wire(arrow_to_json), None, view)
    without = result.wire(arrow_to_json)
    with pytest.raises(InvalidResult, match="receipt"):
        read_result(without, None, view)
    assert read_result(without).envelope.get("receipt") is None  # still valid without one
    foreign = top_n_with_others().with_receipt(record.receipt_id, "exact").wire(arrow_to_json)
    with pytest.raises(InvalidResult, match="receipt"):
        read_result(foreign, None, view)  # another query's result
    assert json.loads(foreign)["query_fingerprint"] != QUERY_FINGERPRINT
