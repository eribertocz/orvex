"""P2-28 (#367; Owner correction 3): multi-fact alignment and materializations against the
pure-`Decimal` oracle. Both reuse the conformance worlds of P2-17 and P2-25, and hold their
answers to recomputations that share nothing with the engine."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from hypothesis import example, given
from hypothesis import strategies as st

from conformance.runners import corpus_execution as ce
from tests.conformance import test_conformed as conformed
from tests.conformance import test_materialization as materialized
from tests.conformance.test_execution import ask
from tests.properties.world import PROFILE, sorted_rows

CENT = Decimal("0.01")
MONTHS = ("2026-01-01", "2026-02-01")
REGIONS = ("North", "South", "East")
# month, region, cents — one generated fact row (CSV cells, so no NULLs here; NULL groups are
# the one-fact world's business in `test_engine_oracle`).
Fact = tuple[str, str, int]
facts = st.lists(
    st.tuples(st.sampled_from(MONTHS), st.sampled_from(REGIONS), st.integers(-99999, 99999)),
    max_size=8,
)
FACT_READS = {
    "gross_revenue": ("sales",),
    "refunds": ("refunds",),
    "net_revenue": ("sales", "refunds"),
}


def _rows(table: list[Fact]) -> list[list[str]]:
    return [
        [str(number), month, region, "web", f"{Decimal(cents) / 100:.2f}"]
        for number, (month, region, cents) in enumerate(table)
    ]


def _sums(table: list[Fact], dims: tuple[str, ...]) -> dict[tuple[str, ...], Decimal]:
    groups: dict[tuple[str, ...], Decimal] = {}
    for month, region, cents in table:
        key = tuple({"sales.month": month, "sales.region": region}[d] for d in dims)
        groups[key] = groups.get(key, Decimal(0)) + Decimal(cents) / 100
    return groups


def _text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    value = value.quantize(CENT)
    return f"{abs(value) if value == 0 else value:f}"


def multifact_oracle(
    sales: list[Fact], refunds: list[Fact], metrics: list[str], dims: tuple[str, ...]
) -> list[list[str | None]]:
    """Aligned facts (C5): the groups are the union over the facts the metrics read; a fact
    without the group is NULL there, and a metric over metrics propagates NULL."""
    per_fact = {"sales": _sums(sales, dims), "refunds": _sums(refunds, dims)}
    read = {fact for name in metrics for fact in FACT_READS[name]}
    keys = sorted({key for fact in read for key in per_fact[fact]})
    if not dims and not keys:
        keys = [()]  # a grand total exists even over nothing
    rows: list[list[str | None]] = []
    for key in keys:
        values: list[str | None] = list(key)
        gross = per_fact["sales"].get(key)
        refunded = per_fact["refunds"].get(key)
        for name in metrics:
            if name == "gross_revenue":
                values.append(_text(gross))
            elif name == "refunds":
                values.append(_text(refunded))
            elif gross is None or refunded is None:  # net: NULL-propagating, after alignment
                values.append(None)
            else:
                values.append(_text(gross - refunded))
        rows.append(values)
    return rows


@st.composite
def questions(draw: st.DrawFn) -> tuple[list[str], tuple[str, ...]]:
    """Valid questions only (Owner correction 2). The model defines which: `net_revenue` is
    aligned on month x region, so it is asked only at that grain; a grouping by `sales.*`
    reads the sales fact, so `gross_revenue` is always among the metrics then. The engine's
    refusals of the other shapes (`METRIC_ALIGNMENT_AMBIGUOUS`, `ILLEGAL_GRAIN_TRANSITION`)
    are tested in `tests/conformance/test_conformed.py`."""
    grain = ("sales.month", "sales.region")
    dims = draw(st.sampled_from((grain, ("sales.month",), ("sales.region",), ())))
    names = ["gross_revenue", "refunds"] + (["net_revenue"] if dims == grain else [])
    chosen = draw(st.lists(st.sampled_from(names), min_size=1, max_size=3, unique=True))
    if dims and "gross_revenue" not in chosen:
        chosen = ["gross_revenue", *chosen]
    return chosen, dims


@PROFILE
@example(
    [("2026-01-01", "North", 10000), ("2026-02-01", "East", 5000)],
    [("2026-01-01", "North", 2000), ("2026-02-01", "South", -500)],
    (["gross_revenue", "refunds", "net_revenue"], ("sales.month", "sales.region")),
)
@given(facts, facts, questions())
def test_aligned_facts_equal_the_oracle_s_union(
    sales: list[Fact], refunds: list[Fact], question: tuple[list[str], tuple[str, ...]]
) -> None:
    metrics, dims = question
    data = {"sales": _rows(sales), "refunds": _rows(refunds), "targets": []}
    document = conformed.query(*metrics, group_by=dims)
    with conformed.lake_of(data) as lake:
        found = ask(document, conformed.COMPILED, lake)
    assert sorted_rows(ce.rows_of(found)) == sorted_rows(
        multifact_oracle(sales, refunds, metrics, dims)
    )


def materialized_oracle(
    sales: list[materialized.Sale], group_by: tuple[str, ...]
) -> list[list[str | None]]:
    """Revenue, quantity and the recomputed price, by direct `Decimal` evaluation."""
    groups: dict[tuple[str, ...], list[Decimal]] = {}
    for month, category, _, _, cents, qty in sales:
        key = tuple(
            {"sales.month": month, "sales.category": category}[dimension] for dimension in group_by
        )
        entry = groups.setdefault(key, [Decimal(0), Decimal(0)])
        entry[0] += Decimal(cents) / 100
        entry[1] += qty
    rows: list[list[str | None]] = []
    for key, (revenue, quantity) in groups.items():
        price = None if not quantity else (revenue / quantity).quantize(CENT, ROUND_HALF_UP)
        rows.append([*key, _text(revenue), _text(quantity), _text(price)])
    return rows


@PROFILE
@example(
    [("2026-01-01", "A", "x", "a", 5, 2), ("2026-01-01", "A", "y", None, -5, 0)],
    ("sales.month", "sales.category"),
)
@given(
    st.lists(
        st.tuples(
            st.sampled_from(MONTHS),
            st.sampled_from(["A", "B"]),
            st.sampled_from("xy"),
            st.sampled_from(["a", None]),
            st.integers(-99999, 99999),
            st.integers(0, 5),
        ),
        min_size=1,
        max_size=8,
    ),
    st.sampled_from((("sales.month", "sales.category"), ("sales.category",), ())),
)
def test_a_verified_aggregate_table_gives_the_oracle_s_answer(
    sales: list[materialized.Sale], group_by: tuple[str, ...]
) -> None:
    """C8 against the oracle: the substituted answer and the base's both equal the direct
    recomputation, so a table may stand in only because it holds the base's numbers."""
    document = materialized.query("revenue", "quantity", "price", group_by=group_by)
    substituted = materialized.run(document, sales)
    base = materialized.run(document, sales, verified=False)
    assert materialized.receipt_reads(substituted) == ["rollup"]  # it really stood in
    assert "rollup" not in materialized.receipt_reads(base)
    expected = sorted_rows(materialized_oracle(sales, group_by))
    assert sorted_rows(ce.rows_of(substituted)) == expected
    assert sorted_rows(ce.rows_of(base)) == expected
