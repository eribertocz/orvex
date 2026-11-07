"""P2-27b (#364): the query baseline's percentiles, checks and record."""

from __future__ import annotations

import json

from tools.benchmarks import query_baseline as qb


def test_percentiles_are_nearest_rank() -> None:
    values = [float(v) for v in range(1, 101)]
    assert qb.nearest_rank(values, 50) == 50.0
    assert qb.nearest_rank(values, 95) == 95.0
    assert qb.nearest_rank([3.0, 1.0, 2.0], 95) == 3.0
    assert qb.nearest_rank([7.0], 50) == 7.0


def test_a_wrong_or_failed_answer_is_an_error_never_a_timing() -> None:
    spec = {"path": "/api/v1/semantic/query", "reference": "SELECT 1"}
    check = qb.checker("aggregate", spec, {"aggregate": [["a", "1.50"]]})
    right = json.dumps({"result": {"rows": [["a", "1.50"]]}}).encode()
    wrong = json.dumps({"result": {"rows": [["a", "1.5"]]}}).encode()  # a decimal is exact text
    failed = json.dumps({"error": {"code": "PROJECT_BUSY"}}).encode()
    assert check(200, right) == ""
    assert check(200, wrong) == "wrong rows"
    assert check(503, failed) == "503 PROJECT_BUSY"
    explain = qb.checker("explain", {"path": "/api/v1/semantic/explain"}, {})
    assert explain(200, b'{"outcome": "planned"}') == ""
    assert explain(200, b'{"outcome": "refused"}') == "not planned"


def test_the_record_shows_every_round_and_its_errors() -> None:
    timings = qb.Timings("aggregate", 1, [10.0, 20.0], [30.0, 40.0], 2.0, {"wrong rows": 1})
    shown = qb.summary_of(timings)
    assert shown["c1_p50_ms"] == 10.0
    assert shown["c4_p95_ms"] == 40.0
    assert shown["c4_throughput_per_s"] == 1.0
    text = qb.markdown(
        {"rounds": [{"round": 1, "peak_rss_bytes": 104857600, "questions": [shown]}]}
    )
    assert "wrong rows: 1" in text
    assert "| 1 | 100 |" in text
