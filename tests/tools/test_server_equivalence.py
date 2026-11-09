"""#246: the server's differential test (`tools/benchmarks/server_equivalence.py`): only what a
run does not decide is made a placeholder, and record rows are compared in a fixed order."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from tools.benchmarks.server_equivalence import cell, normalized, sorted_rows


def test_only_run_specific_text_becomes_a_placeholder(tmp_path: Path) -> None:
    work = tmp_path / "work"
    now = dt.datetime.now(dt.UTC)
    local = now.astimezone(dt.timezone(dt.timedelta(hours=-4))).strftime("%Y-%m-%d %H:%M:%S")
    text = (
        f"{work}/x 127.0.0.1:5000 {'ab' * 16} {now.isoformat()} {local} "
        "2024-11-01T14:50:00+00:00 2024-11-01 240720"
    )
    assert normalized(text, work, "127.0.0.1:5000").split() == [
        "<work>/x",
        "<endpoint>",
        "<id>",
        "<time>",  # when a run started, in UTC
        "<time>",  # and in the project's zone (`runs`)
        "2024-11-01T14:50:00+00:00",  # a value of the data, kept
        "2024-11-01",
        "240720",
    ]


def test_times_are_compared_in_iso_form() -> None:
    moment = dt.datetime(2024, 1, 2, 3, 4, 5, tzinfo=dt.UTC)
    assert cell(moment) == "2024-01-02T03:04:05+00:00"
    assert cell(dt.date(2024, 1, 2)) == "2024-01-02"
    assert cell(None) == "None"


def test_rows_are_compared_in_one_order() -> None:
    def observed(rows: list[dict[str, str]]) -> dict[str, object]:
        return {"persisted": {"tables": {"_meta.r": {"rows": rows}, "s.t": {"checksum": "1"}}}}

    first, second = {"model": "'b'"}, {"model": "'a'"}
    assert sorted_rows(observed([first, second])) == sorted_rows(observed([second, first]))
