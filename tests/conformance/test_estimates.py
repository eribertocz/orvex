"""#352: a detail page's read cost, an upper bound from the snapshot's file statistics, that
decides nothing."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from orvex.contracts.store.v1 import StatementRequest, StatisticsRequest, StatisticsResult
from orvex.semantic.estimates import estimate
from orvex.store.errors import StoreError
from orvex.store.lake import Lake

from conformance.runners import corpus_execution as ce
from tests.conformance import test_detail_rows as detail
from tests.conformance import test_row_policies as rp
from tests.conformance.test_execution import ask
from tests.support.receipts import EVERYTHING

Sale = tuple[int, str, str, int]  # id, tenant, region, cents
INLINED_UP_TO = 10  # the lake keeps an insert of at most this many rows in its catalog
CUSTOMERS = [["1", "BO"], ["2", "PE"]]


@contextmanager
def lake_with(chunks: list[list[Sale]]) -> Iterator[Lake]:
    """One insert per chunk: a data file when larger than the inlining limit, else inlined."""
    with rp.lake_of({"sales": [], "customers": CUSTOMERS}) as lake:
        for chunk in chunks:
            values = ", ".join(
                f"({i}, '{t}', 1, 1, '{r}', {Decimal(c) / 100})" for i, t, r, c in chunk
            )
            with lake.transaction() as transaction:
                transaction.execute(
                    StatementRequest(
                        kind="statement", sql=f"INSERT INTO sales VALUES {values}", parameters=[]
                    )
                )
        yield lake


def statistics_of(lake: Lake) -> Any:  # noqa: ANN401
    """As `server` gives them: the store's statistics, none when it refuses."""

    def statistics(request: StatisticsRequest) -> StatisticsResult | None:
        try:
            return lake.statistics(request)
        except StoreError:
            return None

    return statistics


def estimated(lake: Lake, document: dict[str, Any], **more: Any) -> tuple[Any, Any]:  # noqa: ANN401
    pager = detail.Pager(lake, more.pop("policy", None), more.pop("context", None))
    engine = pager.engine() | {"statistics": more.pop("statistics", statistics_of(lake))}
    found = ask(
        document,
        rp.COMPILED,
        lake,
        policy=pager.policy,
        context=pager.context,
        **engine,
    )
    return found, found.receipt.document()["execution"].get("estimate")


FIELDS = ("sales.sale_id", "sales.region", "customers.country", "sales.amount")
ORDER = [{"by": "sales.sale_id", "direction": "asc"}]


def chunks_of(sales: list[Sale], sizes: list[int]) -> list[list[Sale]]:
    found, at = [], 0
    for size in sizes:
        if at >= len(sales):
            break
        found.append(sales[at : at + size])
        at += size
    if at < len(sales):
        found.append(sales[at:])
    return found


def ranged(sale: Sale, low: int | None, high: int | None) -> bool:
    return (low is None or sale[3] >= low) and (high is None or sale[3] <= high)


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.lists(
        st.tuples(
            st.sampled_from("AB"), st.sampled_from(["North", "South"]), st.integers(-500, 500)
        ),
        min_size=1,
        max_size=60,
    ),
    st.lists(st.integers(1, 25), min_size=1, max_size=5),
    st.one_of(st.none(), st.integers(-500, 500)),
    st.one_of(st.none(), st.integers(-500, 500)),
)
def test_the_estimate_counts_exactly_the_files_a_filter_can_need(
    rows: list[tuple[str, str, int]], sizes: list[int], low: int | None, high: int | None
) -> None:
    """Pruned by exact minimum and maximum only, so it is never below the rows the page needs:
    every inlined row, every customer, and every file whose range meets the filter."""
    sales = [(i + 1, t, r, c) for i, (t, r, c) in enumerate(rows)]
    chunks = chunks_of(sales, sizes)
    bounds = {}
    if low is not None:
        bounds["gte"] = {"type": "decimal", "value": str(Decimal(low) / 100)}
    if high is not None:
        bounds["lte"] = {"type": "decimal", "value": str(Decimal(high) / 100)}
    filters = (
        [{"scope": "visual", "member": "sales.amount", "predicate": {"op": "range", **bounds}}]
        if bounds
        else []
    )
    more = {"filters": filters} if filters else {}
    document = detail.query(size=100, order=ORDER, fields=FIELDS, **more)
    with lake_with(chunks) as lake:
        found, estimate = estimated(lake, document)
    kept = 0
    for chunk in chunks:
        if len(chunk) <= INLINED_UP_TO:
            kept += len(chunk)  # no column statistics: always counted
            continue
        cents = [c for *_, c in chunk]
        meets = (low is None or max(cents) >= low) and (high is None or min(cents) <= high)
        kept += len(chunk) if meets else 0
    assert estimate == {"rows_scanned": kept + len(CUSTOMERS)}
    needed = sum(1 for s in sales if ranged(s, low, high))
    assert len(ce.rows_of(found)) == needed  # the answer is the same
    assert estimate["rows_scanned"] >= needed + len(CUSTOMERS) * (needed > 0)


def file_of(start: int, count: int, tenant: str = "A", cents: int = 100) -> list[Sale]:
    return [(start + i, tenant, "North", cents) for i in range(count)]


def test_the_cursor_start_and_the_policy_prune_the_base_only() -> None:
    a, b, c = file_of(1, 20), file_of(21, 20, tenant="B"), file_of(41, 20)
    with lake_with([a, b, c]) as lake:
        document = detail.query(size=5, order=ORDER, fields=FIELDS)
        _, everything = estimated(lake, document)
        first, page = estimated(
            lake, document, policy=detail.policies(rp.TENANT), context=rp.caller(tenant="A")
        )
        after = first.result.envelope()["page"]["next"]
        second, later = estimated(
            lake,
            document | {"page": {"size": "5", "after": after}},
            policy=detail.policies(rp.TENANT),
            context=rp.caller(tenant="A"),
        )
    assert everything == {"rows_scanned": 62}  # three files and the customers
    assert page == {"rows_scanned": 42}  # tenant B's file cannot hold a row of tenant A
    assert later == {"rows_scanned": 42}  # the start (sale 5) is inside the first file
    assert [r[0] for r in ce.rows_of(second)] == ["6", "7", "8", "9", "10"]


def test_a_start_past_a_file_leaves_it_out() -> None:
    with lake_with([file_of(1, 20), file_of(21, 20)]) as lake:
        document = detail.query(size=25, order=ORDER, fields=FIELDS)
        first, _ = estimated(lake, document)
        after = first.result.envelope()["page"]["next"]
        _, later = estimated(lake, document | {"page": {"size": "25", "after": after}})
    assert later == {"rows_scanned": 22}  # past sale 25: only the second file, and customers


def test_the_estimate_is_hidden_from_viewers_when_row_policies_apply() -> None:
    with lake_with([file_of(1, 20)]) as lake:
        found, estimate = estimated(
            lake,
            detail.query(size=5, order=ORDER, fields=FIELDS),
            policy=detail.policies(rp.TENANT),
            context=rp.caller(tenant="A"),
        )
    assert estimate == {"rows_scanned": 22}  # recorded, for measurement
    assert "estimate" not in found.receipt.view(EVERYTHING)["execution"]  # never shown


def test_statistics_never_refuse_and_never_change_the_answer() -> None:
    """No lower bound: a filter that every file's minimum and maximum match runs, and the
    page is the same with statistics, without them, or with incomplete ones."""
    one_value = [file_of(1, 20, cents=500), file_of(21, 20, cents=500)]
    filters = [
        {
            "scope": "visual",
            "member": "sales.amount",
            "predicate": {"op": "range", "gte": {"type": "decimal", "value": "5.00"}},
        }
    ]
    document = detail.query(size=50, order=ORDER, fields=FIELDS, filters=filters)

    def incomplete(request: StatisticsRequest) -> StatisticsResult:
        found: StatisticsResult = statistics_of(lake)(request)
        tables = [t.model_copy(update={"complete": False}) for t in found.tables]
        changed: StatisticsResult = found.model_copy(update={"tables": tables})
        return changed

    with lake_with(one_value) as lake:
        answers = {
            name: estimated(lake, document, statistics=given)
            for name, given in (
                ("statistics", statistics_of(lake)),
                ("none", None),
                ("refused", lambda request: None),
                ("incomplete", incomplete),
            )
        }
    assert answers["statistics"][1] == {"rows_scanned": 42}
    assert [answers[k][1] for k in ("none", "refused", "incomplete")] == [None, None, None]
    pages = {k: ce.rows_of(answers[k][0]) for k in answers}
    assert len(pages["statistics"]) == 40
    assert all(rows == pages["statistics"] for rows in pages.values())


def test_aggregate_questions_carry_no_estimate() -> None:
    with lake_with([file_of(1, 20)]) as lake:
        found = ask(
            {
                "kind": "semantic_query",
                "version": 1,
                "mode": "aggregate",
                "metrics": [{"metric": "revenue"}],
            },
            rp.COMPILED,
            lake,
            statistics=statistics_of(lake),
        )
    assert "estimate" not in found.receipt.view(EVERYTHING)["execution"]


@pytest.mark.parametrize(
    "table",
    [" sales", "a. b", "x" * 300, "a.b.c", ""],
)
def test_a_table_name_the_request_cannot_carry_gives_no_estimate(table: str) -> None:
    """Statistics never refuse: a name `store/v1` cannot carry is no estimate, and the store is
    never asked."""
    with lake_with([file_of(1, 20)]) as lake:
        document = detail.query(size=5, order=ORDER, fields=FIELDS)
        found, _ = estimated(lake, document)
        asked: list[Any] = []
        physical = replace(found.physical, reads=((table, "SALES"),))
        result = estimate(
            found.plan, physical, rp.COMPILED, asked.append, lake.current_snapshot().id
        )
    assert result is None
    assert asked == []
