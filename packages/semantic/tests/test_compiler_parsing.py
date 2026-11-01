"""P2-04b (#288; #280 is the source of truth): parsing, references, P2-03's advisories, cycles and
lineage. Every case runs on the model written as YAML and as JSON."""

from __future__ import annotations

import contextlib
import json
import tomllib
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st
from orvex.semantic import SemanticError
from orvex.semantic.compiler.parsing import MemberId, ParsedModel, parse_model
from orvex.semantic.ossie import sql
from orvex.semantic.ossie.reader import read

ROOT = Path(__file__).resolve().parents[3]
_PRODUCT = tomllib.loads((ROOT / "identity.toml").read_text("utf-8"))["product"]
CURRENT: str = _PRODUCT["ossie_vendor_name"]
VENDORS = (CURRENT, *_PRODUCT["previous_vendor_names"])
FORMS = ("yaml", "json")

Model = dict[str, Any]


def field(name: str, expression: str | None = None, **more: object) -> Model:
    return {"name": name, "expression": {"sql": expression or name}, **more}


def base() -> Model:
    return {
        "version": "0.2.0.dev0",
        "name": "shop",
        "datasets": [
            {
                "name": "sales",
                "source": "sales",
                "primary_key": ["sale_id"],
                "fields": [
                    field("sale_id"),
                    field("amount"),
                    field("quantity"),
                    field("price"),
                    field("date"),
                ],
                "metrics": [{"name": "sale_count", "expression": {"sql": "COUNT(sale_id)"}}],
            }
        ],
        "metrics": [
            {"name": "revenue", "expression": {"sql": "SUM(sales.amount)"}},
            {"name": "units", "expression": {"sql": "SUM(sales.quantity)"}},
        ],
    }


def extension(payload: object) -> Model:
    return {"vendor_name": CURRENT, "data": json.dumps(payload)}


def derived(name: str, text: str) -> Model:
    """A metric over metrics, declared explicitly (`over_metrics` with the product dialect)."""
    payload = {
        "version": 1,
        "element": "metric",
        "over_metrics": {"expression": text, "align": {"grain": ["month"], "time": "month"}},
    }
    return {
        "name": name,
        "expression": {"dialects": [{"dialect": CURRENT, "expression": text}]},
        "custom_extensions": [extension(payload)],
    }


def compile_both(model: Model, engines: dict[str, str] | None = None) -> list[ParsedModel]:
    parsed = []
    for form in FORMS:
        content = (
            yaml.safe_dump(model, sort_keys=False).encode()
            if form == "yaml"
            else json.dumps(model).encode()
        )
        loaded = read(content, f"semantic/shop.ossie.{form}", VENDORS, engines)
        parsed.append(parse_model(loaded, VENDORS))
    return parsed


def failure_both(model: Model) -> list[SemanticError]:
    errors = []
    for form in FORMS:
        content = (
            yaml.safe_dump(model, sort_keys=False).encode()
            if form == "yaml"
            else json.dumps(model).encode()
        )
        with pytest.raises(SemanticError) as caught:
            parse_model(read(content, f"semantic/shop.ossie.{form}", VENDORS), VENDORS)
        errors.append(caught.value)
    return errors


def problems(error: SemanticError) -> list[tuple[str, str]]:
    found = cast(list[dict[str, str]], error.details["problems"])
    return [(problem["rule"], problem["pointer"]) for problem in found]


REVENUE = MemberId("metric", None, "REVENUE")
UNITS = MemberId("metric", None, "UNITS")


def f(name: str) -> MemberId:
    return MemberId("field", "SALES", name.upper())


# --- references and lineage -------------------------------------------------------------------


def test_references_resolve_and_lineage_reaches_source_fields() -> None:
    for parsed in compile_both(base()):
        assert parsed.members[REVENUE].depends_on == (f("amount"),)
        assert parsed.members[REVENUE].lineage == (f("amount"),)
        assert parsed.members[f("amount")].lineage == (f("amount"),)
        assert parsed.excluded == {}


def test_a_metric_over_a_calculated_field_has_the_fields_sources_in_its_lineage() -> None:
    model = base()
    calculated = {
        "version": 1,
        "element": "field",
        "calculated": {"expression": "quantity * price", "max_cardinality": "1000"},
    }
    model["datasets"][0]["fields"].append(
        field("line_total", "quantity * price", custom_extensions=[extension(calculated)])
    )
    model["metrics"].append({"name": "total", "expression": {"sql": "SUM(sales.line_total)"}})
    for parsed in compile_both(model):
        total = parsed.members[MemberId("metric", None, "TOTAL")]
        assert total.depends_on == (f("line_total"),)
        assert total.lineage == (f("price"), f("quantity"))


def test_filter_predicates_are_part_of_the_lineage() -> None:
    model = base()
    model["metrics"][0]["expression"]["sql"] = "SUM(sales.amount) FILTER (WHERE sales.quantity > 1)"
    for parsed in compile_both(model):
        assert parsed.members[REVENUE].lineage == (f("amount"), f("quantity"))


def test_a_date_part_word_reads_as_a_field_where_an_operand_is_expected() -> None:
    model = base()
    model["metrics"].append({"name": "first_day", "expression": {"sql": "MIN(sales.date)"}})
    for parsed in compile_both(model):
        assert parsed.members[MemberId("metric", None, "FIRST_DAY")].depends_on == (f("date"),)


def test_a_derived_metric_reads_the_metrics_it_names() -> None:
    model = base()
    model["metrics"].append(derived("avg_price", "revenue / units"))
    for parsed in compile_both(model):
        member = parsed.members[MemberId("metric", None, "AVG_PRICE")]
        assert member.language == "product"
        assert member.depends_on == (REVENUE, UNITS)
        assert member.lineage == (f("amount"), f("quantity"))


# --- P2-03's advisories, one test per kind ----------------------------------------------------


def test_unreadable_expression_is_not_compiled_and_neither_is_what_depends_on_it() -> None:
    model = base()
    model["metrics"][0]["expression"]["sql"] = "SUM(sales.amount) + INTERVAL '1' DAY"
    model["metrics"].append(derived("per_unit", "revenue / units"))
    for parsed in compile_both(model):
        assert [a.kind for a in parsed.advisories] == ["unreadable_expression"]
        assert parsed.excluded == {
            REVENUE: "interval",
            MemberId("metric", None, "PER_UNIT"): "interval",
        }
        with pytest.raises(SemanticError) as caught:
            parsed.require(MemberId("metric", None, "PER_UNIT"))
        assert (caught.value.code, dict(caught.value.details)) == (
            "UNSUPPORTED_OPERATION",
            {"operation": "interval"},
        )
        assert parsed.require(UNITS).id == UNITS


@pytest.mark.parametrize(
    ("dialects", "advisory"),
    [
        ([{"dialect": "NOT_A_DIALECT", "expression": "SUM(amount)"}], "no_executable_expression"),
        (
            [
                {"dialect": "DUCKDB", "expression": "SUM(amount)"},
                {"dialect": "DUCKDB", "expression": "SUM(amount) * 1"},
            ],
            "duplicate_dialect",
        ),
        ([{"dialect": CURRENT, "expression": "SUM(amount)"}], "undeclared_product_dialect"),
    ],
)
def test_a_member_with_no_selectable_expression_is_not_compiled(
    dialects: list[Model], advisory: str
) -> None:
    model = base()
    model["datasets"][0]["metrics"][0]["expression"] = {"dialects": dialects}
    model["metrics"].append(derived("doubled", "sales.sale_count * 2"))
    for parsed in compile_both(model, {"sales": "DUCKDB"}):
        assert advisory in {a.kind for a in parsed.advisories}
        sale_count = MemberId("metric", "SALES", "SALE_COUNT")
        assert parsed.excluded == {
            sale_count: "expression_dialect",
            MemberId("metric", None, "DOUBLED"): "expression_dialect",
        }
        with pytest.raises(SemanticError) as caught:
            parsed.require(sale_count)
        assert dict(caught.value.details) == {"operation": "expression_dialect"}


def test_to_columns_cover_no_key_is_carried() -> None:
    model = base()
    model["datasets"].append(
        {
            "name": "customers",
            "source": "customers",
            "primary_key": ["customer_id", "valid_from"],
            "fields": [field("customer_id"), field("valid_from")],
        }
    )
    model["datasets"][0]["fields"].append(field("customer_id"))
    model["relationships"] = [
        {
            "name": "sale_customer",
            "from": "sales",
            "to": "customers",
            "from_columns": ["customer_id"],
            "to_columns": ["customer_id"],
        }
    ]
    for parsed in compile_both(model):
        assert [(a.pointer, a.kind) for a in parsed.advisories] == [
            ("/relationships/0/to_columns", "to_columns_cover_no_key")
        ]


# --- the matrix rows ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "operation"),
    [
        ("SUM(sales.amount) OVER (ORDER BY sales.date)", "window_function"),
        ("MIN(TO_CHAR(sales.date, 'YYYY'))", "experimental_function"),
        ("FOO(sales.amount)", "unknown_function"),
        ("SUM(sales.amount) ^ 2", "operator"),
        ("STRING_AGG(DISTINCT sales.amount, ',')", "outside_subset"),
        ("SUM(sales.amount) IGNORE NULLS", "outside_subset"),
    ],
)
def test_valid_sql_the_product_does_not_implement_is_unsupported(text: str, operation: str) -> None:
    model = base()
    model["metrics"][0]["expression"]["sql"] = text
    for parsed in compile_both(model):
        with pytest.raises(SemanticError) as caught:
            parsed.require(REVENUE)
        assert (caught.value.code, dict(caught.value.details)) == (
            "UNSUPPORTED_OPERATION",
            {"operation": operation},
        )


@pytest.mark.parametrize("text", ["SUM(", "SUM(sales.amount))", "SUM(sales.amount); 1"])
def test_text_outside_the_grammar_is_an_expression_error(text: str) -> None:
    model = base()
    model["metrics"][0]["expression"]["sql"] = text
    for error in failure_both(model):
        assert problems(error) == [("expression", "/metrics/0/expression/sql")]


def test_a_cycle_through_metrics_is_refused() -> None:
    model = base()
    model["metrics"] += [derived("a", "b + 1"), derived("b", "a - 1")]
    for error in failure_both(model):
        assert (error.code, dict(error.details)) == ("METRIC_CYCLE", {"cycle": ["a", "b", "a"]})


def test_a_cycle_through_a_calculated_field_is_refused() -> None:
    model = base()
    looped = {
        "version": 1,
        "element": "field",
        "calculated": {"expression": "loop + 1", "max_cardinality": "10"},
    }
    model["datasets"][0]["fields"].append(
        field("loop", "amount", custom_extensions=[extension(looped)])
    )
    for error in failure_both(model):
        assert (error.code, dict(error.details)) == (
            "METRIC_CYCLE",
            {"cycle": ["sales.loop", "sales.loop"]},
        )


def test_a_field_text_follows_the_same_split_as_a_metric_text() -> None:
    """The official validator checks field SQL too: not one SQL expression → `expression`;
    SQL outside the subset → the advisory, the field excluded, and so is what reads it."""
    model = base()
    model["datasets"][0]["fields"][1]["expression"]["sql"] = "(amount"
    for error in failure_both(model):
        assert problems(error) == [("expression", "/datasets/0/fields/1/expression/sql")]
    model["datasets"][0]["fields"][1]["expression"]["sql"] = "amount +"
    for parsed in compile_both(model):
        assert [a.kind for a in parsed.advisories] == ["unreadable_expression"]
        assert parsed.excluded == {f("amount"): "outside_subset", REVENUE: "outside_subset"}


# --- product and opaque expressions -----------------------------------------------------------


def test_product_expressions_in_payloads_resolve_or_are_refused() -> None:
    model = base()
    model["custom_extensions"] = [
        extension(
            {
                "version": 1,
                "element": "model",
                "segments": [
                    {
                        "name": "big",
                        "dimension": "sales.sale_id",
                        "metric": "revenue",
                        "condition": "nope > 10",
                        "mode": "static",
                    }
                ],
            }
        )
    ]
    for error in failure_both(model):
        assert problems(error) == [("reference", "/custom_extensions/0/data")]
    model["custom_extensions"][0]["data"] = model["custom_extensions"][0]["data"].replace(
        "nope > 10", "revenue >"
    )
    for error in failure_both(model):
        assert problems(error) == [("expression", "/custom_extensions/0/data")]


def test_an_opaque_metric_reads_every_field_and_may_not_name_a_metric() -> None:
    model = base()
    model["datasets"][0]["metrics"][0]["expression"] = {
        "dialects": [{"dialect": "DUCKDB", "expression": "count(*)"}]
    }
    for parsed in compile_both(model, {"sales": "DUCKDB"}):
        member = parsed.members[MemberId("metric", "SALES", "SALE_COUNT")]
        assert member.language == "opaque"
        assert member.lineage == tuple(
            sorted(f(n) for n in ("sale_id", "amount", "quantity", "price", "date"))
        )
    model["datasets"][0]["metrics"][0]["expression"] = {
        "dialects": [{"dialect": "DUCKDB", "expression": "revenue * 2"}]
    }
    model["datasets"][0]["metrics"].append(
        {"name": "revenue", "expression": {"sql": "SUM(amount)"}}
    )
    for form in FORMS:
        content = (
            yaml.safe_dump(model, sort_keys=False).encode()
            if form == "yaml"
            else json.dumps(model).encode()
        )
        loaded = read(content, f"semantic/shop.ossie.{form}", VENDORS, {"sales": "DUCKDB"})
        with pytest.raises(SemanticError) as caught:
            parse_model(loaded, VENDORS)
        assert problems(caught.value) == [
            ("reference", "/datasets/0/metrics/0/expression/dialects/0/expression")
        ]


# --- properties -------------------------------------------------------------------------------

_SQL_ALPHABET = st.sampled_from(
    [
        *"abc_ 019.,()'\"*+-/<>=%|\n",
        "SUM",
        "COUNT(",
        "DISTINCT ",
        "CASE ",
        "WHEN ",
        "THEN ",
        "END",
        "OVER (",
        "FILTER (WHERE ",
        "AND ",
        "NOT ",
        "IN (",
        "CAST(",
        " AS ",
        "DECIMAL(",
        "INTERVAL ",
        "SELECT ",
        "/*",
        "*/",
        "--",
        "date",
        "::",
    ]
)


@settings(max_examples=400, deadline=None)
@given(st.lists(_SQL_ALPHABET, max_size=40).map("".join))
def test_any_text_gives_a_tree_or_a_typed_refusal(text: str) -> None:
    try:
        tree = sql.parse(text)
    except sql.SqlSyntaxError, sql.SqlUnsupported:
        return
    assert sql.walk(tree)
    sql.unsupported(tree)
    sql.aggregate_class(tree)


@settings(max_examples=80, deadline=None)
@given(
    st.lists(_SQL_ALPHABET, max_size=30).map("".join),
    st.sampled_from(["metric", "dataset_metric", "field"]),
)
def test_any_expression_gives_a_model_or_a_semantic_error(text: str, where: str) -> None:
    model = base()
    if where == "metric":
        model["metrics"][0]["expression"]["sql"] = text or "x"
    elif where == "dataset_metric":
        model["datasets"][0]["metrics"][0]["expression"]["sql"] = text or "x"
    else:
        model["datasets"][0]["fields"][1]["expression"]["sql"] = text or "x"
    content = json.dumps(model).encode()
    with contextlib.suppress(SemanticError):
        parse_model(read(content, "semantic/shop.ossie.json", VENDORS), VENDORS)


def test_deep_nesting_is_a_typed_refusal() -> None:
    with pytest.raises(sql.SqlSyntaxError):
        sql.parse("(" * 5000 + "1" + ")" * 5000)
