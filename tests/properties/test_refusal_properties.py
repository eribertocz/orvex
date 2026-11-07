"""P2-28 (#367; Owner correction 2): a corrupted question is refused with a stable code from
the error catalog, before any read. The valid branch lives in `test_engine_oracle`, where a
refusal of a valid question fails the test — so rejecting everything cannot pass either."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pyarrow as pa
import pytest
from hypothesis import given
from hypothesis import strategies as st
from orvex.semantic.errors import SemanticError

from conformance.runners.semantic_corpus import ROOT
from tests.properties.world import (
    PROFILE,
    Document,
    World,
    aggregate_queries,
    ask,
    lake_of,
    worlds,
)

CODES = set(json.loads((ROOT / "contracts/semantic-error/v1/codes.json").read_text("utf-8")))


Corruption = Callable[[Document], Document]


def corruptions() -> st.SearchStrategy[Corruption]:
    """One corruption of a valid question, each refused by a different rule."""

    def unknown_metric(document: Document) -> Document:
        return {**document, "metrics": [{"metric": "no_such_metric"}]}

    def unknown_member(document: Document) -> Document:
        return {**document, "group_by": [{"member": "sales.no_such_field"}]}

    def wrong_shape(document: Document) -> Document:
        return {**document, "metrics": [{"metric": "revenue", "apply": "not-a-list"}]}

    def wrong_mode(document: Document) -> Document:
        return {**document, "mode": "pivot"}

    def missing_kind(document: Document) -> Document:
        return {key: value for key, value in document.items() if key != "kind"}

    def unknown_predicate(document: Document) -> Document:
        bad = {"scope": "visual", "member": "sales.amount", "predicate": {"op": "like"}}
        return {**document, "filters": [bad]}

    def page_above_the_cap(document: Document) -> Document:
        return {
            "kind": "semantic_query",
            "version": 1,
            "mode": "detail",
            "fields": [{"member": "sales.sale_id"}],
            "order": [{"by": "sales.sale_id", "direction": "asc"}],
            "page": {"size": "100000"},
        }

    return st.sampled_from(
        [
            unknown_metric,
            unknown_member,
            wrong_shape,
            wrong_mode,
            missing_kind,
            unknown_predicate,
            page_above_the_cap,
        ]
    )


class Spy:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, request: Any) -> pa.RecordBatchReader:  # noqa: ANN401
        self.calls += 1
        raise AssertionError("a refused question must not read")


@PROFILE
@given(worlds(), aggregate_queries(), corruptions())
def test_a_corrupted_question_is_refused_with_a_stable_code_before_any_read(
    world: World, document: Document, corrupt: Corruption
) -> None:
    spy = Spy()
    with lake_of(world) as lake, pytest.raises(SemanticError) as caught:
        ask(corrupt(document), lake, read=spy)
    assert caught.value.code in CODES  # a stable code of the catalog, never a stack trace
    assert spy.calls == 0
