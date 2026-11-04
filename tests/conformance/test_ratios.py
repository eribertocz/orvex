"""P2-13 (#325): ratios recomputed from their components, divided exactly, rounded once."""

from __future__ import annotations

import json
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP, ROUND_UP, Decimal, localcontext
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from orvex.semantic.compiler.catalog import SourceColumn
from orvex.semantic.compiler.compiled import CompiledModel, compile_model
from orvex.semantic.errors import SemanticError
from orvex.semantic.ossie.reader import read
from orvex.store.errors import StoreError

from conformance.runners import corpus_execution as ce
from tests.conformance.test_execution import ask, corpus_case, lake_for, tiny_lake
from tests.support.policies import VENDORS, extension

MODES = {
    "half_away_from_zero": ROUND_HALF_UP,  # Python's HALF_UP rounds half away from zero
    "half_even": ROUND_HALF_EVEN,
    "down": ROUND_DOWN,
    "up": ROUND_UP,
}
Rows = list[dict[str, str | None]]


def ratio_model(
    *,
    amount: str = "DECIMAL(38,2)",
    qty: str = "DECIMAL(18,2)",
    scale: int = 2,
    mode: str = "half_away_from_zero",
    zero: str = "null",
    numerator: str = "SUM(t.amount)",
    denominator: str = "SUM(t.qty)",
) -> tuple[CompiledModel, dict[str, Any]]:
    document = {
        "version": "0.2.0.dev0",
        "name": "ratios",
        "datasets": [
            {
                "name": "t",
                "source": "t",
                "primary_key": ["id"],
                "fields": [
                    {"name": n, "expression": {"sql": n}} for n in ("id", "g", "amount", "qty")
                ],
            },
            {
                "name": "o",
                "source": "o",
                "primary_key": ["id"],
                "fields": [{"name": "id", "expression": {"sql": "id"}}],
            },
        ],
        "metrics": [
            {
                "name": "ratio",
                "expression": {"sql": f"{numerator} / {denominator}"},
                "custom_extensions": [
                    extension(
                        {
                            "element": "metric",
                            "additivity": "recomputable",
                            "components": {"numerator": numerator, "denominator": denominator},
                            "zero_denominator": zero,
                            "rounding": {"scale": scale, "mode": mode},
                        }
                    )
                ],
            }
        ],
        "custom_extensions": [extension({"element": "model", "default_time_zone": "UTC"})],
    }
    catalog = {
        "t": {
            "id": SourceColumn("BIGINT", False),
            "g": SourceColumn("VARCHAR"),
            "amount": SourceColumn(amount),
            "qty": SourceColumn(qty),
        },
        "o": {"id": SourceColumn("BIGINT", False)},
    }
    loaded = read(json.dumps(document).encode(), "semantic/ratios.ossie.json", VENDORS)
    return compile_model(loaded, catalog, VENDORS), {"t": catalog["t"]}


BY_GROUP = {
    "kind": "semantic_query",
    "version": 1,
    "mode": "aggregate",
    "metrics": [{"metric": "ratio"}],
    "group_by": [{"member": "t.g"}],
}
OVERALL = {
    "kind": "semantic_query",
    "version": 1,
    "mode": "aggregate",
    "metrics": [{"metric": "ratio"}],
}


def rows(*values: tuple[str, str | None, str | None]) -> Rows:
    return [{"id": str(i), "g": g, "amount": a, "qty": q} for i, (g, a, q) in enumerate(values, 1)]


def answer_of(compiled: CompiledModel, catalog: dict[str, Any], data: Rows, query: Any) -> Any:  # noqa: ANN401
    with tiny_lake(catalog, data) as lake:
        return ask(query, compiled, lake)


# --- CASE-006 -------------------------------------------------------------------------------


def test_case_006_recomputes_the_ratio_from_its_components() -> None:
    compiled, _, _ = corpus_case("CASE-006")
    with lake_for("CASE-006") as lake:
        found = ask(ce.QUERIES["CASE-006"], compiled, lake)
    assert ce.rows_of(found) == [["500.00", "4", "125.00"]]
    obligations = {o["obligation"] for o in found.receipt.document()["plan"]["obligations"]}
    assert "C2.recompute:average_order_value" in obligations
    assert " / " not in found.physical.sql  # never a decimal division, which DuckDB makes DOUBLE


def test_a_ratio_is_never_an_average_of_ratios() -> None:
    compiled, catalog = ratio_model()
    data = rows(("a", "100.00", "1.00"), ("a", "1.00", "1.00"), ("b", "9.00", "3.00"))
    found = answer_of(compiled, catalog, data, BY_GROUP)
    # a: (100 + 1) / (1 + 1) = 50.50, never the mean of 100 and 1 per row computed otherwise.
    assert ce.rows_of(found) == [["a", "50.50"], ["b", "3.00"]]
    assert ce.rows_of(answer_of(compiled, catalog, data, OVERALL)) == [["22.00"]]  # 110 / 5


# --- exact division --------------------------------------------------------------------------

CASES = [
    ("5", "2", 0),
    ("-5", "2", 0),
    ("5", "-2", 0),
    ("7", "2", 0),
    ("-7", "2", 0),
    ("1", "3", 10),
    ("2", "3", 2),
    ("-2", "3", 2),
    ("0.05", "1", 1),
    ("-0.05", "1", 1),
    ("12345678901234567890123456789012.34", "7", 2),
]


def expected(numerator: str, denominator: str, scale: int, mode: str) -> str:
    quantum = Decimal(1).scaleb(-scale)
    with localcontext() as context:
        context.prec = 120  # exact for every case here
        found = (Decimal(numerator) / Decimal(denominator)).quantize(quantum, MODES[mode])
    return str(found.copy_abs() if found.is_zero() else found)  # a decimal has no -0


@pytest.mark.parametrize("mode", sorted(MODES))
@pytest.mark.parametrize(("numerator", "denominator", "scale"), CASES)
def test_the_division_is_exact_in_every_mode(
    numerator: str, denominator: str, scale: int, mode: str
) -> None:
    compiled, catalog = ratio_model(scale=scale, mode=mode)
    found = answer_of(compiled, catalog, rows(("a", numerator, denominator)), OVERALL)
    assert ce.rows_of(found) == [[expected(numerator, denominator, scale, mode)]]
    (column,) = found.result.envelope()["columns"]
    assert column["encoding"] == f"DECIMAL(38,{scale})"


def test_negative_midpoints_round_away_from_zero_or_to_even() -> None:
    for mode, value in (("half_away_from_zero", "-3"), ("half_even", "-2")):
        compiled, catalog = ratio_model(scale=0, mode=mode)
        found = answer_of(compiled, catalog, rows(("a", "-5", "2")), OVERALL)
        assert ce.rows_of(found) == [[value]], mode


def test_a_38_digit_numerator_is_answered_exactly() -> None:
    """Every scaled intermediate must fit 128 bits: a 38-digit `DECIMAL(38,2)` sum divided by a
    count fits, since the common power of ten cancels."""
    compiled, catalog = ratio_model(denominator="COUNT(*)", scale=2)
    found = answer_of(
        compiled, catalog, rows(("a", "111111111111111111111111111111111111.55", None)), OVERALL
    )
    assert ce.rows_of(found) == [["111111111111111111111111111111111111.55"]]


def test_counts_skip_nulls_and_count_rows() -> None:
    """CASE-014's rules over the plan and the SQL: `COUNT(x)` skips nulls, `COUNT(*)` counts
    rows, and their ratio divides those two exact counts."""
    compiled, catalog = ratio_model(
        numerator="COUNT(t.amount)", denominator="COUNT(*)", scale=4, mode="half_even"
    )
    data = rows(("a", None, "1.00"), ("a", "1.00", "1.00"), ("a", "2.00", "1.00"))
    found = answer_of(compiled, catalog, data, OVERALL)
    assert "COUNT(*)" in found.physical.sql
    assert ce.rows_of(found) == [["0.6667"]]  # 2 / 3


def test_an_overflow_fails_with_no_result() -> None:
    compiled, catalog = ratio_model(scale=30, amount="DECIMAL(38,8)")
    data = rows(("a", "999999999999999999999999999999.99999999", "1"))
    with pytest.raises(StoreError) as caught:
        answer_of(compiled, catalog, data, OVERALL)
    assert caught.value.code == "STORE_QUERY_FAILED"


# --- zero denominators and nulls --------------------------------------------------------------


def test_a_zero_denominator_follows_its_declared_policy() -> None:
    data = rows(("a", "10.00", "0.00"), ("b", "10.00", "4.00"))
    compiled, catalog = ratio_model(zero="null")
    assert ce.rows_of(answer_of(compiled, catalog, data, BY_GROUP)) == [
        ["a", None],
        ["b", "2.50"],
    ]
    compiled, catalog = ratio_model(zero="reject")
    with pytest.raises(SemanticError) as caught:
        answer_of(compiled, catalog, data, BY_GROUP)
    assert (caught.value.code, dict(caught.value.details)) == (
        "DIVISION_POLICY_VIOLATION",
        {"metric": "ratio"},
    )


def test_nulls_are_never_zero() -> None:
    compiled, catalog = ratio_model()
    data = rows(
        ("partial", "10.00", "1.00"),
        ("partial", None, "1.00"),  # SUM skips it: 10 / 2
        ("no_numerator", None, "1.00"),  # all-null numerator: NULL
        ("no_denominator", "5.00", None),  # all-null denominator: NULL
        ("zero", "0.00", "3.00"),  # a zero numerator is zero, not NULL
    )
    assert ce.rows_of(answer_of(compiled, catalog, data, BY_GROUP)) == [
        ["no_denominator", None],
        ["no_numerator", None],
        ["partial", "5.00"],
        ["zero", "0.00"],
    ]


# --- still refused --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("numerator", "denominator", "operation"),
    [
        ("SUM(t.amount)", "COUNT(o.id)", "multi_fact"),
        ("SUM(t.amount)", "MAX(t.qty)", "recomputable"),
    ],
)
def test_ratios_across_facts_or_of_non_additive_parts_are_refused(
    numerator: str, denominator: str, operation: str
) -> None:
    compiled, catalog = ratio_model(numerator=numerator, denominator=denominator)
    with pytest.raises(SemanticError) as caught:
        answer_of(compiled, catalog, rows(("a", "1.00", "1.00")), OVERALL)
    assert (caught.value.code, dict(caught.value.details)) == (
        "UNSUPPORTED_OPERATION",
        {"operation": operation},
    )


def test_a_distinct_count_component_never_compiles_undeclared() -> None:
    with pytest.raises(SemanticError) as caught:
        ratio_model(numerator="COUNT(DISTINCT t.amount)")
    assert caught.value.code == "MISSING_EXTENSION_DECLARATION"


# --- property -------------------------------------------------------------------------------

DIGITS = st.integers(min_value=-(10**20), max_value=10**20)


@settings(max_examples=25, deadline=None)
@given(DIGITS, DIGITS.filter(lambda d: d != 0), st.sampled_from(sorted(MODES)), st.integers(0, 6))
def test_the_division_equals_decimal_quantize(
    numerator: int, denominator: int, mode: str, scale: int
) -> None:
    n, d = str(Decimal(numerator).scaleb(-2)), str(Decimal(denominator).scaleb(-2))
    compiled, catalog = ratio_model(scale=scale, mode=mode, qty="DECIMAL(38,2)")
    found = answer_of(compiled, catalog, rows(("a", n, d)), OVERALL)
    assert ce.rows_of(found) == [[expected(n, d, scale, mode)]]
