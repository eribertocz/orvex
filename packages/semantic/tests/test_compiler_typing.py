"""P2-04c (#289; #280 is the source of truth): the type system, exact decimal rules and the
source catalog. Every case runs on the model written as YAML and as JSON."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st
from orvex.semantic import SemanticError
from orvex.semantic.compiler.catalog import LogicalType, SourceCatalog, SourceColumn, logical_of
from orvex.semantic.compiler.parsing import MemberId, parse_model
from orvex.semantic.compiler.typecheck import TypedModel, TypedNode, type_model
from orvex.semantic.compiler.zones import TZDATA_VERSION, is_zone
from orvex.semantic.ossie import sql
from orvex.semantic.ossie.reader import read

ROOT = Path(__file__).resolve().parents[3]
_PRODUCT = tomllib.loads((ROOT / "identity.toml").read_text("utf-8"))["product"]
CURRENT: str = _PRODUCT["ossie_vendor_name"]
VENDORS = (CURRENT, *_PRODUCT["previous_vendor_names"])
FORMS = ("yaml", "json")

Model = dict[str, Any]

CATALOG = {
    "sales": {
        "sale_id": SourceColumn("BIGINT", False),
        "customer_id": SourceColumn("BIGINT", False),
        "quantity": SourceColumn("BIGINT", False),
        "unit_price": SourceColumn("DECIMAL(18,2)", False),
        "amount": SourceColumn("DECIMAL(38,2)"),
        "wide": SourceColumn("DECIMAL(38,20)"),
        "share": SourceColumn("DECIMAL(5,4)"),
        "score": SourceColumn("DOUBLE"),
        "region": SourceColumn("VARCHAR"),
        "sold_on": SourceColumn("DATE", False),
        "sold_at": SourceColumn("TIMESTAMP WITH TIME ZONE"),
        "local_at": SourceColumn("TIMESTAMP"),
        "flag": SourceColumn("BOOLEAN"),
        "tags": SourceColumn("VARCHAR[]"),
    },
    "stores": {"store_id": SourceColumn("BIGINT", False), "open": SourceColumn("BOOLEAN")},
}


def extension(payload: dict[str, Any]) -> Model:
    return {"vendor_name": CURRENT, "data": json.dumps({"version": 1, **payload})}


def field(name: str, expression: str | None = None, **payload: object) -> Model:
    node: Model = {"name": name, "expression": {"sql": expression or name}}
    if payload:
        node["custom_extensions"] = [extension({"element": "field", **payload})]
    return node


def metric(name: str, expression: str, **payload: object) -> Model:
    node: Model = {"name": name, "expression": {"sql": expression}}
    if payload:
        node["custom_extensions"] = [extension({"element": "metric", **payload})]
    return node


def base(*metrics: Model, fields: list[Model] | None = None, model: Model | None = None) -> Model:
    names = [
        "sale_id",
        "quantity",
        "unit_price",
        "amount",
        "wide",
        "score",
        "region",
        "sold_on",
        "sold_at",
        "local_at",
        "flag",
        "tags",
    ]
    document: Model = {
        "version": "0.2.0.dev0",
        "name": "shop",
        "datasets": [
            {
                "name": "sales",
                "source": "sales",
                "primary_key": ["sale_id"],
                "fields": [
                    *(field(n) for n in names),
                    field("customer_id", semantic_type="identifier"),
                    field("share", semantic_type="ratio"),
                    *(fields or []),
                ],
            },
            {
                "name": "stores",
                "source": "stores",
                "primary_key": ["store_id"],
                "fields": [field("store_id"), field("open")],
            },
        ],
        "metrics": list(metrics),
    }
    if model is not None:
        document["custom_extensions"] = [extension({"element": "model", **model})]
    return document


def content(document: Model, form: str) -> bytes:
    if form == "yaml":
        return yaml.safe_dump(document, sort_keys=False).encode()
    return json.dumps(document).encode()


def typed_both(
    document: Model, catalog: SourceCatalog = CATALOG, engines: dict[str, str] | None = None
) -> list[TypedModel]:
    found = []
    for form in FORMS:
        loaded = read(content(document, form), f"semantic/shop.ossie.{form}", VENDORS, engines)
        found.append(type_model(loaded, parse_model(loaded, VENDORS), catalog))
    return found


def failure_both(document: Model, catalog: SourceCatalog = CATALOG) -> list[SemanticError]:
    errors = []
    for form in FORMS:
        loaded = read(content(document, form), f"semantic/shop.ossie.{form}", VENDORS)
        parsed = parse_model(loaded, VENDORS)
        with pytest.raises(SemanticError) as caught:
            type_model(loaded, parsed, catalog)
        errors.append(caught.value)
    return errors


def problems(error: SemanticError) -> list[tuple[str, str]]:
    found = cast(list[dict[str, str]], error.details["problems"])
    return [(problem["rule"], problem["pointer"]) for problem in found]


def m(name: str) -> MemberId:
    return MemberId("metric", None, name.upper())


def f(name: str) -> MemberId:
    return MemberId("field", "SALES", name.upper())


TEXT = "/metrics/0/expression/sql"


def expect_type_error(expression: str, **payload: object) -> None:
    for error in failure_both(base(metric("x", expression, **payload))):
        assert error.code == "OSSIE_INVALID"
        assert problems(error) == [("type", TEXT)]


def walk(tree: TypedNode) -> list[TypedNode]:
    found = [tree]
    for child in tree.children:
        found += walk(child)
    return found


# --- the case matrix (#280) ----------------------------------------------------------------------


def test_case_002_decimal_product_sums_to_decimal_38_2_exact() -> None:
    text = "SUM(CAST(sales.quantity AS DECIMAL(20, 0)) * sales.unit_price)"
    for typed in typed_both(base(metric("revenue", text))):
        revenue = typed.members[m("revenue")]
        assert revenue.type.logical == LogicalType("Decimal", 38, 2)
        assert revenue.type.exactness == "exact"
        assert revenue.type.kind == "metric"
        assert revenue.type.aggregate_class == "distributive"
        assert revenue.tree is not None
        product = revenue.tree.children[0]
        assert product.type.logical == LogicalType("Decimal", 38, 2)  # p1+p2 = 38, s1+s2 = 2
        assert revenue.tree.type.overflow_check  # SUM carries the obligation


@pytest.mark.parametrize(
    "expression",
    [
        "SUM(sales.customer_id)",  # an identifier admits no SUM
        "AVG(sales.customer_id)",
        "SUM(sales.customer_id + 1)",  # nor arithmetic
        "SUM(sales.share)",  # a ratio admits no SUM
        "SUM(CAST(sales.customer_id AS DECIMAL(20, 0)))",  # a cast keeps the meaning
    ],
)
def test_semantic_type_restrictions(expression: str) -> None:
    expect_type_error(expression)


def test_a_ratio_still_averages() -> None:
    for typed in typed_both(
        base(metric("x", "AVG(sales.share)", rounding={"scale": 4, "mode": "half_even"}))
    ):
        assert typed.members[m("x")].type.logical == LogicalType("Decimal", 38, 4)


@pytest.mark.parametrize(
    "expression",
    [
        "SUM(sales.amount + CAST(sales.quantity AS DOUBLE))",  # exact with approximate
        "SUM(sales.amount + sales.score)",
        "SUM(CAST(sales.quantity AS DECIMAL))",  # precision and scale are required
        "SUM(CAST(sales.quantity AS DECIMAL(10)))",
        "SUM(CAST(sales.quantity AS DECIMAL(39, 0)))",
        "SUM(CAST(sales.quantity AS DECIMAL(5, 6)))",
    ],
)
def test_casts_never_hide_an_implicit_conversion(expression: str) -> None:
    expect_type_error(expression)


def test_decimal_38_2_plus_decimal_38_2_caps_with_an_overflow_obligation() -> None:
    for typed in typed_both(base(metric("x", "SUM(sales.amount + sales.amount)"))):
        tree = typed.members[m("x")].tree
        assert tree is not None
        addition = tree.children[0]
        assert addition.type.logical == LogicalType("Decimal", 38, 2)
        assert addition.type.overflow_check


def test_decimal_38_20_times_decimal_38_20_is_rejected_for_its_scale() -> None:
    expect_type_error("SUM(sales.wide * sales.wide)")


@pytest.mark.parametrize(
    "expression",
    ["SUM(SUM(sales.amount))", "COUNT(MAX(sales.quantity))", "SUM(sales.amount) + sales.quantity"],
)
def test_no_nested_aggregates_and_no_row_value_beside_an_aggregate(expression: str) -> None:
    expect_type_error(expression)


def test_an_unknown_zone_is_an_extension_error() -> None:
    document = base(
        fields=[field("local_day", "DATE_TRUNC('DAY', sold_at)", time_zone={"name": "Mars/Base"})]
    )
    for error in failure_both(document):
        assert problems(error) == [("extension", "/datasets/0/fields/14/custom_extensions/0/data")]
    document = base(model={"default_time_zone": "Mars/Base"})
    for error in failure_both(document):
        assert problems(error) == [("extension", "/custom_extensions/0/data")]


def test_case_019_bucketing_carries_the_declared_zone() -> None:
    document = base(
        fields=[
            field(
                "business_day",
                "DATE_TRUNC('DAY', sold_at)",
                time_zone={"name": "America/La_Paz", "database_version": "2025b"},
                business_time=True,
            )
        ]
    )
    for typed in typed_both(document):
        day = typed.members[f("business_day")]
        assert day.type.logical == LogicalType("DateTimeTz", zone="America/La_Paz")
        assert day.tree is not None
        assert day.tree.type.buckets_instant
        assert day.tree.type.bucket_zone == "America/La_Paz"
        assert typed.tzdata == TZDATA_VERSION


# --- other rules ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expression",
    [
        "COUNT(sales.sale_id) FILTER (WHERE sales.amount)",  # not a boolean
        "COUNT(sales.sale_id) FILTER (WHERE stores.open)",  # another dataset
        "COUNT(sales.sale_id) FILTER (WHERE SUM(sales.amount) > 1)",
        "SUM(CASE WHEN sales.sold_at > sales.local_at THEN 1 END)",  # DateTimeTz with DateTime
        "SUM(CASE WHEN sales.sold_on > sales.local_at THEN 1 END)",  # no implicit widening
        "SUM(CASE WHEN sales.region > 3 THEN 1 END)",
        # an instant needs an offset
        "SUM(CASE WHEN sales.sold_at > '2026-01-01T00:00:00' THEN 1 END)",
        "SUM(CASE WHEN sales.sold_on > '2026-13-01' THEN 1 END)",
        "SUM(CASE WHEN sales.flag THEN sales.amount ELSE sales.region END)",
        "SUM(sales.region)",
        "SUM(sales.tags)",  # opaque: never computed with
        "COUNT(sales.sale_id) + 'a'",
        "MAX(LOWER(sales.quantity))",
        "SUM(sales.amount) / SUM(sales.score)",
        "SUM(sales.quantity) * SQRT(SUM(sales.amount)) + SUM(sales.amount)",
        "MAX(ROUND(sales.amount, sales.quantity))",
        "SUM(DATEADD(DAY, 1, sales.amount))",
        "MAX(DATE_TRUNC('FORTNIGHT', sales.sold_on))",
        "MAX(HOUR(sales.sold_on))",
        "1",
        "sales.amount",
    ],
)
def test_type_errors(expression: str) -> None:
    expect_type_error(expression)


@pytest.mark.parametrize(
    "expression",
    [
        "COUNT(sales.sale_id) FILTER (WHERE sales.flag AND sales.region = 'north')",
        "SUM(CASE WHEN sales.sold_at > '2026-01-01T00:00:00-04:00' THEN sales.amount END)",
        "SUM(CASE WHEN sales.sold_on >= '2026-01-01' THEN sales.amount END)",
        "SUM(sales.score * 2) + 1.5",  # a literal takes its partner's exactness
        "MAX(DATE_TRUNC(MONTH, sales.sold_on))",
        "SUM(COALESCE(sales.amount, 0))",
        "COUNT(DISTINCT sales.customer_id)",
    ],
)
def test_well_typed(expression: str) -> None:
    for typed in typed_both(base(metric("x", expression))):
        assert m("x") in typed.members


def test_a_missing_source_column_is_a_reference_error() -> None:
    document = base(fields=[field("missing")])
    for error in failure_both(document):
        assert problems(error) == [("reference", "/datasets/0/fields/14/expression/sql")]
    for error in failure_both(base(), {"stores": CATALOG["stores"]}):
        assert ("reference", "/datasets/0/fields/0/expression/sql") in problems(error)


def test_a_field_is_row_level() -> None:
    for error in failure_both(base(fields=[field("total", "SUM(amount)")])):
        assert problems(error) == [("type", "/datasets/0/fields/14/expression/sql")]


def test_a_declared_datatype_equals_the_inferred_one() -> None:
    document = base(fields=[{**field("price2", "unit_price"), "datatype": "Integer"}])
    for error in failure_both(document):
        assert problems(error) == [("type", "/datasets/0/fields/14/datatype")]
    document = base(metric("x", "SUM(sales.amount)") | {"datatype": "Float"})
    for error in failure_both(document):
        assert problems(error) == [("type", "/metrics/0/datatype")]
    document = base(
        fields=[field("price3", "unit_price", numeric={"precision": "18", "scale": "4"})]
    )
    for error in failure_both(document):
        assert problems(error) == [("type", "/datasets/0/fields/14/custom_extensions/0/data")]
    document = base(
        fields=[
            {
                **field("price4", "unit_price", numeric={"precision": "18", "scale": "2"}),
                "datatype": "Decimal",
            }
        ]
    )
    for typed in typed_both(document):
        assert typed.members[f("price4")].type.logical == LogicalType("Decimal", 18, 2)


def test_division_and_average_take_the_metrics_rounding() -> None:
    rounding = {"scale": 4, "mode": "half_even"}
    document = base(
        metric("ratio", "SUM(sales.amount) / SUM(sales.quantity)", rounding=rounding),
        metric("mean", "AVG(sales.unit_price)"),
    )
    for typed in typed_both(document):
        assert typed.members[m("ratio")].type.logical == LogicalType("Decimal", 38, 4)
        assert typed.members[m("ratio")].type.nullable
        assert typed.members[m("mean")].type.logical == LogicalType("Decimal", 38, None)


def test_nullability_volatility_and_counts() -> None:
    document = base(
        metric("n", "COUNT(sales.amount)"),
        metric("recent", "COUNT(sales.sale_id) FILTER (WHERE sales.sold_on >= CURRENT_DATE)"),
        metric("units", "SUM(sales.quantity)"),
    )
    for typed in typed_both(document):
        n = typed.members[m("n")].type
        assert (n.nullable, n.logical, n.exactness) == (
            False,
            LogicalType("Integer", width=64, signed=True),
            "exact",
        )
        assert typed.members[m("recent")].type.volatility == "query_instant"
        assert typed.members[m("n")].type.volatility == "immutable"
        units = typed.members[m("units")].type
        assert units.logical == LogicalType("Integer", width=128, signed=True)
        assert units.nullable


def test_an_engine_dialect_metric_is_typed_from_its_declarations_and_holistic() -> None:
    opaque = {
        "name": "p90",
        "datatype": "Decimal",
        "expression": {
            "dialects": [{"dialect": "DUCKDB", "expression": "quantile_cont(amount, 0.9)"}]
        },
    }
    document = base()
    document["datasets"][0]["metrics"] = [opaque]
    for typed in typed_both(document, engines={"sales": "DUCKDB"}):
        p90 = typed.members[MemberId("metric", "SALES", "P90")]
        assert p90.tree is None
        assert p90.type.logical.datatype == "Decimal"
        assert p90.type.aggregate_class == "holistic"
        assert p90.type.exactness == "exact"


def test_store_types_map_to_the_logical_vocabulary() -> None:
    assert logical_of("HUGEINT") == LogicalType("Integer", width=128, signed=True)
    assert logical_of("UBIGINT") == LogicalType("Integer", width=64, signed=False)
    assert logical_of("decimal(18, 2)") == LogicalType("Decimal", 18, 2)
    assert logical_of("DECIMAL(40,2)") == LogicalType("Opaque")
    assert logical_of("TIMESTAMP WITH TIME ZONE") == LogicalType("DateTimeTz")
    assert logical_of("STRUCT(a INTEGER)") == LogicalType("Opaque")
    assert is_zone("America/La_Paz")
    assert is_zone("UTC")
    assert not is_zone("Mars/Base")
    assert not is_zone("america/la_paz")


# --- property: no approximation without an explicit one ------------------------------------------

_EXPLICIT = sql.AGGREGATES.keys() & {
    "STDDEV",
    "STDDEV_POP",
    "STDDEV_SAMP",
    "VARIANCE",
    "VAR_POP",
    "VAR_SAMP",
    "APPROX_COUNT_DISTINCT",
    "APPROX_PERCENTILE",
}


def _explicit(tree: TypedNode) -> bool:
    """An explicit approximation at this node or below."""
    for node in walk(tree):
        current = node.node
        if isinstance(current, sql.Cast) and current.type_name in ("DOUBLE", "FLOAT"):
            return True
        if isinstance(current, sql.Call) and (
            current.name in _EXPLICIT or current.name in ("SQRT", "LN", "EXP", "POWER", "PI", "SIN")
        ):
            return True
        if isinstance(current, sql.Number) and "e" in current.text.lower():
            return True
        if isinstance(current, sql.Ref) and node.type.logical.datatype == "Float":
            return True
    return False


_ROW = st.recursive(
    st.sampled_from(
        [
            "sales.amount",
            "sales.quantity",
            "sales.unit_price",
            "sales.score",
            "1",
            "2.50",
            "1e2",
            "NULL",
        ]
    ),
    lambda inner: st.one_of(
        st.tuples(inner, st.sampled_from(["+", "-", "*", "/"]), inner).map(
            lambda t: f"({t[0]} {t[1]} {t[2]})"
        ),
        inner.map(lambda t: f"CAST({t} AS DOUBLE)"),
        inner.map(lambda t: f"CAST({t} AS DECIMAL(18, 2))"),
        inner.map(lambda t: f"SQRT({t})"),
        inner.map(lambda t: f"ABS({t})"),
        st.tuples(inner, inner).map(lambda t: f"COALESCE({t[0]}, {t[1]})"),
    ),
    max_leaves=6,
)
_METRIC = st.tuples(st.sampled_from(["SUM", "AVG", "MAX", "STDDEV", "COUNT"]), _ROW).map(
    lambda t: f"{t[0]}({t[1]})"
)


@settings(max_examples=150, deadline=None)
@given(_METRIC)
def test_no_approximate_node_without_an_explicit_approximation(expression: str) -> None:
    form = "json"
    loaded = read(content(base(metric("x", expression)), form), "semantic/shop.ossie.json", VENDORS)
    try:
        typed = type_model(loaded, parse_model(loaded, VENDORS), CATALOG)
    except SemanticError as error:
        if error.code != "OSSIE_INVALID":
            raise
        return
    tree = typed.members[m("x")].tree
    assert tree is not None
    for node in walk(tree):
        if node.type.exactness == "approximate":
            assert _explicit(node), expression
