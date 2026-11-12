"""Transitive hiding (#442 AC5, Q1, precision 3): a metric derived from a hidden or masked
member, directly or through other metrics, is hidden for the caller and indistinguishable from
a metric that never existed: absent from the catalog, and `MEMBER_NOT_FOUND` with an unknown
name's details in a question and in `explain`. A caller who may see the member sees it.

On a `utility` copy whose model adds metrics over metrics: `phone_ratio` over `phone_count`
(reads `consumers.phone`, masked except for `collections`), `phone_ratio_scaled` over
`phone_ratio`, `national_ratio` over `national_id_count` (reads `consumers.national_id`,
hidden except for `collections`), and `kwh_per_reading`, which reads nothing protected.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from orvex.semantic.catalog.building import catalog_for_context
from orvex.semantic.compiler.compiled import compile_model
from orvex.semantic.errors import SemanticError
from orvex.semantic.execution import answer
from orvex.semantic.explanation import explain
from orvex.semantic.ossie.reader import read
from orvex.semantic.policies import read_policies
from orvex.semantic.receipt.viewer import CallerViewer

from conformance.runners import corpus_execution as ce
from conformance.runners import security_fixtures as sf

VENDOR = ce.VENDORS[0]
FOUND = sf.fixture("utility", VENDOR)


def over(name: str, text: str) -> dict[str, Any]:
    declared = {
        "version": 1, "element": "metric", "additivity": "non_additive",
        "aggregate_class": "algebraic", "zero_denominator": "null",
        "rounding": {"scale": 4, "mode": "half_even"},
        "over_metrics": {"expression": text, "align": {"grain": ["zone_id"], "time": "none"}},
    }  # fmt: skip
    return {
        "name": name,
        "expression": {"dialects": [{"dialect": VENDOR, "expression": text}]},
        "custom_extensions": [{"vendor_name": VENDOR, "data": json.dumps(declared)}],
    }


TEXT = (FOUND.folder / "model.ossie.json").read_text("utf-8")
MODEL = json.loads(TEXT.replace("{{vendor}}", VENDOR))
MODEL["metrics"] += [
    over("phone_ratio", "phone_count / consumer_count"),
    over("phone_ratio_scaled", "phone_ratio * 100"),
    over("national_ratio", "national_id_count / consumer_count"),
    over("kwh_per_reading", "total_kwh / reading_count"),
]
COMPILED = compile_model(read(json.dumps(MODEL).encode(), "model.ossie.json", ce.VENDORS),
                         FOUND.catalog, ce.VENDORS)  # fmt: skip
POLICIES = read_policies((FOUND.folder / "security.yaml").read_text("utf-8"), COMPILED,
                         groups=sf.GROUPS, managed_tables=FOUND.managed)  # fmt: skip
HIDDEN = ["phone_count", "phone_ratio", "phone_ratio_scaled", "national_id_count",
          "national_ratio"]  # fmt: skip
NEVER = "zz_never_declared"


def caller(groups: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "kind": "security_context", "version": 1, "user": "u-zone1", "project": "utility",
        "role": "author", "groups": sorted(groups), "attributes": {},
        "permission_fingerprint": "sha256:" + "0" * 64,
    }  # fmt: skip


def question(metric: str) -> str:
    return json.dumps({"kind": "semantic_query", "version": 1, "mode": "aggregate",
                       "metrics": [{"metric": metric}]})  # fmt: skip


def refusal(metric: str, how: str, context: dict[str, Any]) -> tuple[str, str] | None:
    """The code and details the caller gets for `metric`, with its name blanked, or None."""
    with sf.managed_lake(FOUND) as lake:
        try:
            if how == "explain":
                viewer = CallerViewer(COMPILED, POLICIES, context)
                explain(question(metric), COMPILED, POLICIES, context, ce.engine_for(lake), viewer)
            else:
                answer(question(metric), COMPILED, POLICIES, context, ce.engine_for(lake))
        except SemanticError as error:
            details = json.dumps(dict(error.details), sort_keys=True)
            return error.code, details.replace(metric, "<name>")
    return None


def metrics_of(context: dict[str, Any]) -> set[str]:
    document = catalog_for_context(COMPILED, POLICIES, context)
    return {str(m["name"]) for m in document["metrics"]}


def test_derived_metrics_are_absent_from_the_catalog_of_a_caller_who_may_not_see_the_member() -> (
    None
):
    listed = metrics_of(caller())
    assert not set(HIDDEN) & listed
    assert "kwh_per_reading" in listed  # reads nothing protected
    text = json.dumps(catalog_for_context(COMPILED, POLICIES, caller()))
    assert not any(name in text for name in HIDDEN)  # not even as a reference or a description


def test_a_caller_who_may_see_the_member_sees_every_derived_metric() -> None:
    assert {*HIDDEN, "kwh_per_reading"} <= metrics_of(caller(("collections",)))
    for metric in HIDDEN:
        found = refusal(metric, "question", caller(("collections",)))
        assert found is None or found[0] != "MEMBER_NOT_FOUND", (metric, found)


@pytest.mark.parametrize("how", ["question", "explain"])
@pytest.mark.parametrize("metric", HIDDEN)
def test_a_hidden_derived_metric_is_refused_exactly_as_an_unknown_one(
    metric: str, how: str
) -> None:
    unknown = refusal(NEVER, how, caller())
    assert unknown is not None
    assert unknown[0] == "MEMBER_NOT_FOUND"
    assert refusal(metric, how, caller()) == unknown
