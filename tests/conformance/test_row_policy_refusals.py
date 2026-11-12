"""Under a row policy, a shape the engine does not execute yet is refused before any read (#441
AC6, AC9): `UNSUPPORTED_OPERATION` naming the shape, never an answer without the policy.

Two kinds of shapes are pending:
- **A question's shape** (`QUESTION_SHAPES`): each is asked here on the `utility` fixture by a
  restricted caller, and nothing is read.
- **A model's declaration** (`DECLARED_SHAPES`): a model that declares one is refused for every
  question that reaches it, whoever asks, before planning. `test_correctness` proves those.

Every pending shape is in exactly one of the two, so a shape added later cannot go untested.
The policies that are representable but not executable (a hierarchy, a minimum group size; a
cell condition is executed since #442) refuse their dataset the same way (`POLICY_SHAPES`).
The two normative cases that wait for a later phase (SEC-ROWS-009, P4-02; SEC-LEAK-005, P5-13)
are among those refused.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import yaml
from orvex.semantic.correctness import IMPLEMENTED, SHAPES
from orvex.semantic.errors import SemanticError
from orvex.semantic.execution import answer
from orvex.semantic.policies import read_policies

from conformance.runners import corpus_execution as ce
from conformance.runners import security_fixtures as sf
from tests.conformance.test_execution import Spy
from tests.conformance.test_security_scenarios import cases

Document = dict[str, Any]


def query(**more: Any) -> Document:  # noqa: ANN401
    return {
        "kind": "semantic_query",
        "version": 1,
        "mode": "aggregate",
        "metrics": [{"metric": "total_kwh"}],
        "group_by": [{"member": "readings.zone_id"}],
        **more,
    }


ZONE_FILTER = {
    "scope": "visual",
    "member": "readings.zone_id",
    "predicate": {"op": "in", "values": [{"type": "text", "value": "Z1"}], "include_null": False},
}
QUESTION_SHAPES: dict[str, Document] = {
    "top_n": query(top_n={"by": "total_kwh", "n": "1", "others": False}),
    "context_modifier": query(
        context_modifiers=[{"op": "remove", "scope": "visual", "members": ["readings.zone_id"]}],
        filters=[ZONE_FILTER],
    ),
    "filter_context": query(contexts=[{"name": "other", "filters": [ZONE_FILTER]}]),
    "metric_filter": query(
        metric_filters=[
            {
                "scope": "visual",
                "metric": "total_kwh",
                "dimension": "readings.zone_id",
                "condition": {"op": "gt", "value": {"type": "decimal", "value": "1"}},
            }
        ]
    ),
    "coverage": query(coverage="aligned"),
    "as_known_at": query(as_known_at="2026-10-31T23:59:59Z"),
}
# Declared in the model, refused for every question that reaches the declaration (test_correctness).
DECLARED_SHAPES = frozenset(
    {
        "holistic", "approximate", "semi_additive", "period_end_ties", "mixed_period_end",
        "non_additive", "multi_fact", "fanout", "calendar_bucket", "ordered", "window_filter",
        "bridge_allocation", "metric_context", "level_of_detail", "over_metrics",
        "reaggregable_state", "calculated_field", "history", "metric_extension",
        "segment_filter", "hierarchy_filter", "period_end_filter", "as_of_join",
    }
)  # fmt: skip
RESTRICTED = cases()["SEC-ROWS-001"]["caller"]


def refused(
    document: Document,
    policy_text: str | None = None,
    managed: Any = None,  # noqa: ANN401
    code: str = "UNSUPPORTED_OPERATION",
) -> str:
    found = sf.fixture("utility", ce.VENDORS[0])
    policies = (
        found.policies(RESTRICTED["groups"])
        if policy_text is None
        else read_policies(
            policy_text, found.compiled, groups=sf.GROUPS, managed_tables=managed or found.managed
        )
    )
    spy = Spy()
    with sf.managed_lake(found) as lake, pytest.raises(SemanticError) as caught:
        answer(
            json.dumps(document), found.compiled, policies, dict(RESTRICTED),
            ce.engine_for(lake, read=spy),
        )  # fmt: skip
    assert spy.calls == 0  # nothing read
    assert caught.value.code == code, caught.value
    return str(caught.value.details.get("operation", code))


def test_every_pending_shape_is_tested_once() -> None:
    pending = {s for s, shape in SHAPES.items() if shape.rule not in IMPLEMENTED}
    assert set(QUESTION_SHAPES) | DECLARED_SHAPES == pending
    assert not set(QUESTION_SHAPES) & DECLARED_SHAPES


@pytest.mark.parametrize("shape", sorted(QUESTION_SHAPES))
def test_a_pending_question_shape_is_refused_before_any_read(shape: str) -> None:
    if shape == "as_known_at":  # its own code (S12): no engine keeps the history it needs yet
        assert refused(QUESTION_SHAPES[shape], code="AS_KNOWN_AT_UNSUPPORTED")
        return
    assert refused(QUESTION_SHAPES[shape]) == shape


@pytest.mark.parametrize(
    ("name", "shape"), [("SEC-ROWS-009", "context_modifier"), ("SEC-LEAK-005", "top_n")]
)
def test_a_case_waiting_for_a_later_phase_is_refused_until_then(name: str, shape: str) -> None:
    case = cases()[name]
    assert case["pending"] in {"P4-02", "P5-13"}
    assert refused(case["request"]["body"]) == shape


UTILITY = yaml.safe_load((sf.SECURITY / "fixtures/utility/security.yaml").read_text("utf-8"))
CLOSURE = {"security.org_closure": ["ancestor", "descendant"]}
POLICY_SHAPES: dict[str, Document] = {
    "hierarchy_policy": {
        "row_policies": [
            {
                "name": "org",
                "dataset": "readings",
                "kind": "hierarchy",
                "closure": {
                    "table": "security.org_closure",
                    "ancestor": "ancestor",
                    "descendant": "descendant",
                },
                "node": "org_node",
                "field": "zone_id",
                "orphans": "hidden",
            }
        ]
    },
    "min_group_size": {
        "aggregate_thresholds": [
            {"dataset": "readings", "entity": "readings.consumer_id", "min_group_size": 5}
        ]
    },
}


@pytest.mark.parametrize("shape", sorted(POLICY_SHAPES))
def test_a_representable_policy_not_executed_refuses_its_dataset_before_any_read(
    shape: str,
) -> None:
    document = dict(UTILITY)
    for key, entries in POLICY_SHAPES[shape].items():
        document[key] = [*document.get(key, []), *entries]
    managed = {**sf.fixture("utility", ce.VENDORS[0]).managed, **CLOSURE}
    assert refused(query(), yaml.safe_dump(document), managed) == shape
