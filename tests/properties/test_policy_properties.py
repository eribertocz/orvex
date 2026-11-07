"""P2-28 (#367): row and column policies on generated questions never widen what a caller
sees (Owner corrections 3 and 6)."""

from __future__ import annotations

from collections import Counter

from hypothesis import example, given
from orvex.semantic.errors import SemanticError

from conformance.runners import corpus_execution as ce
from tests.properties import oracle
from tests.properties.test_engine_oracle import EDGE, EVERYTHING, PAGED
from tests.properties.world import (
    HIDDEN_POLICY,
    PROFILE,
    ROW_POLICY,
    Caller,
    Document,
    World,
    aggregate_queries,
    ask,
    callers,
    detail_queries,
    lake_of,
    policies,
    sorted_rows,
    worlds,
)

RESTRICTED = policies(ROW_POLICY)
HIDDEN = policies(HIDDEN_POLICY)
WEB = Caller("web", ce.viewer_context(attributes={"channel": {"type": "text", "value": "web"}}))


def pages(query: Document, lake: object, **more: object) -> list[list[str | None]]:
    rows: list[list[str | None]] = []
    document = query
    while True:
        found = ask(document, lake, **more)  # type: ignore[arg-type]
        rows += ce.rows_of(found)
        cursor = found.result.envelope()["page"].get("next")
        if cursor is None:
            return rows
        document = {**query, "page": {**query["page"], "after": cursor}}


@PROFILE
@example(EDGE, EVERYTHING, WEB)
@given(worlds(), aggregate_queries(), callers())
def test_an_aggregate_under_a_row_policy_derives_only_from_visible_rows(
    world: World, query: Document, caller: Caller
) -> None:
    """The answer equals the oracle applied to the filtered rows, so every aggregated value —
    the whole aggregate, not only row lists — derives from visible rows alone."""
    with lake_of(world) as lake:
        found = ask(query, lake, policy=RESTRICTED, context=caller.context)
    expected = oracle.expected(world.customers, world.sales, query, channel=caller.channel)
    assert sorted_rows(ce.rows_of(found)) == sorted_rows(expected)
    with_ratio = any(entry["metric"] == "unit_price" for entry in query["metrics"])
    if not with_ratio:
        duck = oracle.duckdb_expected(world.customers, world.sales, query, caller.channel)
        assert sorted_rows(ce.rows_of(found)) == sorted_rows(duck)


@PROFILE
@example(EDGE, PAGED, WEB)
@given(worlds(), detail_queries(), callers())
def test_rows_with_a_policy_are_a_subset_of_the_rows_without(
    world: World, query: Document, caller: Caller
) -> None:
    with lake_of(world) as lake:
        restricted = pages(query, lake, policy=RESTRICTED, context=caller.context)
        unrestricted = pages(query, lake)
    expected = oracle.expected(world.customers, world.sales, query, channel=caller.channel)
    assert restricted == sorted(expected, key=lambda row: int(str(row[0])))
    gaps = Counter(map(tuple, unrestricted))
    gaps.subtract(map(tuple, restricted))
    assert not -gaps  # never a row the unrestricted answer does not have


@PROFILE
@given(worlds(), aggregate_queries())
def test_a_hidden_member_is_not_found_and_never_binds(world: World, query: Document) -> None:
    import pytest

    naming = {
        **query,
        "group_by": [*query.get("group_by", []), {"member": "customers.segment"}],
    }
    with lake_of(world) as lake:
        with pytest.raises(SemanticError) as caught:
            ask(naming, lake, policy=HIDDEN)
        assert caught.value.code == "MEMBER_NOT_FOUND"
        if not any(
            entry.get("member") == "customers.segment"
            for entry in [*query.get("group_by", []), *query.get("filters", [])]
        ):
            found = ask(query, lake, policy=HIDDEN)  # the rest of the model still answers
            expected = oracle.expected(world.customers, world.sales, query)
            assert sorted_rows(ce.rows_of(found)) == sorted_rows(expected)


def test_a_caller_without_the_policy_s_attribute_sees_zero_rows() -> None:
    """Fail closed: no attribute, no rows, and the notice says so."""
    query = {**PAGED, "page": {"size": "5"}}
    with lake_of(EDGE) as lake:
        found = ask(query, lake, policy=RESTRICTED, context=ce.viewer_context())
    assert ce.rows_of(found) == []
    notices = found.result.envelope().get("notices", [])
    assert any(notice["code"] == "POLICY_ATTRIBUTE_MISSING" for notice in notices)


MASKED = policies(
    {
        "version": 1,
        "column_policies": [{"member": "sales.channel", "default": "masked", "mask": "***"}],
    }
)


@PROFILE
@example(EDGE, PAGED)
@given(worlds(), detail_queries())
def test_a_masked_field_never_shows_a_value(world: World, query: Document) -> None:
    """Correction 3: every cell of a masked field is its mask, never a value or NULL, on every
    page; the other columns still equal the oracle."""
    asked = {
        **query,
        "fields": [{"member": "sales.sale_id"}, {"member": "sales.channel"}],
        "filters": [f for f in query.get("filters", []) if f["member"] != "sales.channel"],
    }
    if not asked["filters"]:
        del asked["filters"]
    with lake_of(world) as lake:
        rows = pages(asked, lake, policy=MASKED)
    assert all(row[1] == "***" for row in rows)
    expected = oracle.expected(world.customers, world.sales, asked)
    ids = sorted(int(str(row[0])) for row in expected)
    assert [int(str(row[0])) for row in rows] == ids
