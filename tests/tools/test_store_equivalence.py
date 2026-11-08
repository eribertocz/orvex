"""#246: the store's differential test (`tools/benchmarks/store_equivalence.py`): its
comparison finds every difference, and only what a run does not decide is made a placeholder."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from tools.benchmarks.store_equivalence import differences, normalized


def test_identical_observations_have_no_difference() -> None:
    observed = {"lake": [{"step": "open", "value": [1, {"a": "b"}]}]}
    assert differences(observed, {"lake": [{"step": "open", "value": [1, {"a": "b"}]}]}) == []


def test_every_kind_of_difference_is_found() -> None:
    baseline = {"a": [{"error": {"code": "X", "message": "m"}}], "b": [1, 2], "c": 1}
    candidate = {"a": [{"error": {"code": "Y", "message": "m"}}], "b": [1], "d": 1}
    assert differences(baseline, candidate) == [
        "/a[0]/error/code: 'X' != 'Y'",
        "/b: 2 items != 1",
        "/c: only in baseline",
        "/d: only in candidate",
    ]


def test_only_run_specific_text_becomes_a_placeholder(tmp_path: Path) -> None:
    work, data = tmp_path / "work", tmp_path / "data"
    now = dt.datetime.now(dt.UTC).isoformat()
    text = (
        f"{work}/x {data}/y 127.0.0.1:5000 /tmp/lake-spill-abc1 {'ab' * 16} {now} "
        "340282366920938463463374607431768211455 2024-11-01T14:50:00+00:00"
    )
    assert normalized(text, work, data, "127.0.0.1:5000").split() == [
        "<work>/x",
        "<data>/y",
        "<endpoint>",
        "/tmp/<tmp>",
        "<id>",
        "<commit-time>",
        "340282366920938463463374607431768211455",  # a value, kept
        "2024-11-01T14:50:00+00:00",  # not a commit of this run
    ]
