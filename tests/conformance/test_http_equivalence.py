"""P2-27a (#363): an executed answer with cell states, asked through the HTTP API, is the same
in JSON and in Arrow (Owner precision 3) and the same as the library's."""

from __future__ import annotations

import json

from orvex.semantic.policies import read_policies

from conformance.runners import corpus_execution as ce
from conformance.runners import corpus_http
from tests.conformance import test_asof_windows as windows


def test_running_totals_at_total_levels_are_states_in_both_forms() -> None:
    document = windows.query(
        "cumulative_by_region", group_by=("daily.day", "daily.region"), totals="rollup"
    )
    policies = read_policies(b"version: 1\n", windows.DAILY, groups=frozenset(), managed_tables={})
    with windows.lake_of(windows.DAYS, windows.DAILY_CATALOG) as lake:
        through_http = corpus_http.http(
            json.dumps(document), windows.DAILY, policies, ce.viewer_context(), lake
        )
        through_library = ce.library(
            json.dumps(document), windows.DAILY, policies, ce.viewer_context(), lake
        )
    states = through_http.envelope["cell_states"]
    assert states  # the case has states, so the comparison covers them
    assert {s["state"] for s in states} == {"undefined_at_total"}
    assert through_http.rows == through_library.rows
    assert states == through_library.envelope["cell_states"]
