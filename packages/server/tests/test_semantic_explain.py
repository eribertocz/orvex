"""P2-27b (#364): `POST /api/v1/semantic/explain` over the project the server opened, on the
same snapshot, model and caller as `query`, with no data read."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from orvex.server.app import LocalSemantic

from tests.support import semantic_http as h

EXPLAIN = "/api/v1/semantic/explain"


def explain(http: TestClient, semantic: LocalSemantic, query: dict[str, Any]) -> Any:  # noqa: ANN401
    return http.post(EXPLAIN, json=query, headers=h.bearer(semantic))


@pytest.fixture
def served(tmp_path: Path) -> tuple[LocalSemantic, TestClient]:
    h.project(tmp_path)
    h.run(tmp_path)
    semantic = h.local(tmp_path)
    return semantic, h.client(semantic)


def test_an_explanation_has_the_answer_s_identity(served: tuple[LocalSemantic, TestClient]) -> None:
    semantic, http = served
    for query in (h.aggregate(), h.detail(2)):
        explained = explain(http, semantic, query)
        assert explained.status_code == 200, explained.text
        document = explained.json()
        answered = h.ask(http, semantic, query).json()
        assert document["outcome"] == "planned"
        result, receipt = answered["result"], answered["receipt"]
        assert document["query_fingerprint"] == result["query_fingerprint"]
        assert document["model_fingerprint"] == result["model_fingerprint"]
        assert document["question"] == receipt["question"]
        assert document["plan"] == receipt["plan"]
        assert document["evidence"] == receipt["evidence"]
        assert document["snapshot"]["id"] == receipt["data"][0]["snapshot"]["id"]


def test_a_next_page_is_explained_on_its_cursor_s_snapshot(
    served: tuple[LocalSemantic, TestClient], tmp_path: Path
) -> None:
    semantic, http = served
    first = h.ask(http, semantic, h.detail(2)).json()
    pinned = first["receipt"]["data"][0]["snapshot"]["id"]
    after = first["result"]["page"]["next"]
    h.readings(tmp_path, h.FIRST + ", (5, 'C4', 'residential', 9.0::DECIMAL(18,4))")
    h.run(tmp_path)
    document = explain(http, semantic, h.detail(2, after)).json()
    assert document["snapshot"]["id"] == pinned
    assert document["outcome"] == "planned"
    second = h.ask(http, semantic, h.detail(2, after)).json()["result"]
    assert document["query_fingerprint"] == second["query_fingerprint"]
    assert document["model_fingerprint"] == second["model_fingerprint"]


def test_the_question_s_own_errors_stay_http_errors(
    served: tuple[LocalSemantic, TestClient],
) -> None:
    semantic, http = served
    unknown = explain(http, semantic, {**h.aggregate(), "metrics": [{"metric": "nothing"}]})
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "MEMBER_NOT_FOUND")
    forged = explain(http, semantic, h.detail(2, "x" * 40))
    assert forged.status_code == 400
    assert forged.json()["error"]["semantic"]["details"] == {"reason": "invalid"}
    assert http.post(EXPLAIN, json=h.aggregate()).status_code == 401  # no credential


def test_hidden_members_are_not_found_never_explained(tmp_path: Path) -> None:
    h.project(tmp_path, h.POLICIES)
    h.run(tmp_path)
    semantic = h.local(tmp_path)
    response = explain(
        h.client(semantic),
        semantic,
        {**h.aggregate(), "group_by": [{"member": "readings.consumer_id"}]},
    )
    assert (response.status_code, response.json()["error"]["code"]) == (404, "MEMBER_NOT_FOUND")


def test_violated_evidence_is_explained_as_the_refusal_it_would_be(tmp_path: Path) -> None:
    duplicated = h.FIRST + ", (1, 'C9', 'residential', 1.0::DECIMAL(18,4))"
    h.project(tmp_path, rows=duplicated, evidence=("unique",))
    h.run(tmp_path)
    semantic = h.local(tmp_path)
    document = explain(h.client(semantic), semantic, h.detail(10)).json()
    assert (document["outcome"], document["stage"]) == ("refused", "planned")
    assert document["refusal"]["code"] == "EVIDENCE_VIOLATED"
    assert [e["status"] for e in document["evidence"]] == ["violated"]


def test_a_builder_sees_the_estimate_and_the_materialization(
    served: tuple[LocalSemantic, TestClient],
) -> None:
    semantic, http = served
    document = explain(http, semantic, h.detail(10)).json()
    assert document["materialization"] == {"reason": "no_eligible_table"}
    assert set(document["estimate"]) <= {"rows_scanned", "groups", "cells"}
    assert int(document["estimate"]["rows_scanned"]) >= 4
    text = json.dumps(document)
    for canary in ("SELECT", " FROM ", "marts.readings", "12.3456"):
        assert canary not in text  # no SQL, no table path, no data value


def test_an_explanation_never_reads(
    served: tuple[LocalSemantic, TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    from orvex.store.lake import Lake

    semantic, http = served
    original = Lake.read

    def read(self: Lake, request: Any) -> Any:  # noqa: ANN401
        assert "_meta.evidence_" in request.sql, "an explanation read data"
        return original(self, request)

    monkeypatch.setattr(Lake, "read", read)
    for query in (h.aggregate(), h.detail(2)):
        assert explain(http, semantic, query).status_code == 200
