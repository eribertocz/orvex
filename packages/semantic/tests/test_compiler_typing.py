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
        st.tuples(inner, inner).map(lambda t: f"CASE WHEN sales.flag THEN {t[0]} ELSE {t[1]} END"),
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


# --- the Reviewer's first pass: each finding covered for its class ------------------------------


@pytest.mark.parametrize(
    "expression",
    [
        "SUM(sales.amount + 1e3)",
        "SUM(COALESCE(sales.amount, 0E0))",
        "SUM(CASE WHEN sales.flag THEN sales.amount ELSE 0e0 END)",
        "SUM(sales.amount) + 1.5e-2",
    ],
)
def test_a_number_literal_is_exact_exponent_included(expression: str) -> None:
    for typed in typed_both(base(metric("x", expression))):
        x = typed.members[m("x")].type
        assert x.logical.datatype == "Decimal"
        assert x.exactness == "exact"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1e3", LogicalType("Integer", width=64, signed=True)),
        ("2.50", LogicalType("Decimal", 3, 2)),
        ("1.5e-2", LogicalType("Decimal", 3, 3)),
        ("0.05", LogicalType("Decimal", 2, 2)),
        ("99999999999999999999", LogicalType("Decimal", 20, 0)),
    ],
)
def test_literal_shapes(text: str, expected: LogicalType) -> None:
    for typed in typed_both(base(metric("x", f"SUM(sales.amount) + {text}"))):
        tree = typed.members[m("x")].tree
        assert tree is not None
        assert tree.children[1].type.logical == expected


def overflow_at(document: Model, member: MemberId, path: tuple[int, ...]) -> list[bool]:
    found = []
    for typed in typed_both(document):
        node = typed.members[member].tree
        assert node is not None
        for at in path:
            node = node.children[at]
        found.append(node.type.overflow_check)
    return found


ROUNDING = {"scale": 4, "mode": "half_even"}


@pytest.mark.parametrize(
    ("expression", "rounding", "expected"),
    [
        ("AVG(sales.amount)", ROUNDING, True),  # 36 integer digits + 4 > 38
        ("AVG(sales.unit_price)", ROUNDING, False),  # 16 + 4
        ("AVG(sales.unit_price)", None, True),  # undecided scale: may exceed
        ("SUM(sales.amount) / 3", ROUNDING, True),
        ("SUM(sales.unit_price) / SUM(sales.quantity)", ROUNDING, True),  # 36 + 0 + 4
        ("MAX(sales.unit_price) / 2", ROUNDING, False),  # 16 + 0 + 4
    ],
)
def test_quotients_and_means_carry_the_overflow_obligation(
    expression: str, rounding: object, expected: bool
) -> None:
    payload = {"rounding": rounding} if rounding is not None else {}
    found = overflow_at(base(metric("x", expression, **payload)), m("x"), ())
    assert found == [expected, expected]


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("CAST(amount AS DECIMAL(5, 2))", True),
        ("CAST(unit_price AS DECIMAL(18, 2))", False),
        ("CAST(unit_price AS DECIMAL(17, 1))", True),  # rounding 9.99 reaches 10.0
        ("CAST(unit_price AS DECIMAL(18, 1))", False),
        ("CAST(amount AS BIGINT)", True),
        ("CAST(quantity AS INTEGER)", True),
        ("CAST(quantity AS BIGINT)", False),
        ("CAST(quantity AS DECIMAL(19, 0))", False),
        ("CAST(quantity AS DECIMAL(18, 0))", True),
        ("CAST(region AS BIGINT)", True),
        ("CAST(score AS DECIMAL(10, 2))", True),
        ("CAST(quantity AS VARCHAR)", False),
    ],
)
def test_a_narrowing_cast_carries_the_overflow_obligation(expression: str, expected: bool) -> None:
    document = base(fields=[field("narrowed", expression)])
    assert overflow_at(document, f("narrowed"), ()) == [expected, expected]


@pytest.mark.parametrize(
    "expression",
    [
        "SUM(CASE WHEN sales.flag THEN sales.customer_id ELSE sales.quantity END)",
        "SUM(GREATEST(sales.customer_id, sales.quantity))",
        "SUM(COALESCE(sales.customer_id, sales.quantity))",
        "AVG(CASE WHEN sales.flag THEN sales.customer_id ELSE sales.quantity END)",
        "SUM(IF(sales.flag, sales.share, sales.unit_price))",
        "SUM(NVL2(sales.flag, sales.share, sales.unit_price))",
    ],
)
def test_a_semantic_restriction_survives_alternatives(expression: str) -> None:
    expect_type_error(expression)


@pytest.mark.parametrize(
    "expression",
    [
        "SUM(DATE_TRUNC(sales.amount))",
        "SUM(DATE_PART(sales.quantity))",
        "SUM(DATEDIFF(sales.amount))",
        "SUM(DATEADD(sales.quantity, 1, sales.sold_on))",
        "MAX(DATE_TRUNC())",
        "SUM(ABS(sales.amount, 1))",
        "SUM(SQRT(sales.amount, 2))",
        "MAX(YEAR(sales.sold_on, 1))",
    ],
)
def test_every_scalar_has_an_explicit_signature(expression: str) -> None:
    expect_type_error(expression)


def test_the_bucketing_zone_does_not_depend_on_where_it_is_written() -> None:
    """The bucketing field's own zone, then the bucketed value's, then the model default."""
    document = base(
        metric("days", "COUNT(DISTINCT DATE_TRUNC('DAY', sales.zoned))"),
        metric("utc_days", "COUNT(DISTINCT DATE_TRUNC('DAY', sales.sold_at))"),
        fields=[
            field("zoned", "sold_at", time_zone={"name": "America/La_Paz"}),
            field("zoned_day", "DATE_TRUNC('DAY', sold_at)", time_zone={"name": "Asia/Tokyo"}),
            field("plain_day", "DATE_TRUNC('DAY', sold_at)"),
        ],
        model={"default_time_zone": "UTC"},
    )
    for typed in typed_both(document):

        def zone(member: MemberId, *path: int, typed: TypedModel = typed) -> str | None:
            node = typed.members[member].tree
            assert node is not None
            for at in path:
                node = node.children[at]
            return node.type.bucket_zone

        assert typed.members[f("zoned")].type.logical.zone == "America/La_Paz"
        assert zone(m("days"), 0) == "America/La_Paz"  # the value's zone over the default
        assert zone(m("utc_days"), 0) == "UTC"
        assert zone(f("zoned_day")) == "Asia/Tokyo"  # the bucketing field's own zone
        assert zone(f("plain_day")) == "UTC"


def test_an_undeclared_opaque_type_is_of_unknown_exactness() -> None:
    opaque = {
        "name": "p90",
        "expression": {"dialects": [{"dialect": "DUCKDB", "expression": "quantile(amount, 0.9)"}]},
    }
    document = base()
    document["datasets"][0]["metrics"] = [opaque]
    document["metrics"] = [
        {
            "name": "doubled",
            "expression": {"dialects": [{"dialect": CURRENT, "expression": "sales.p90 * 2"}]},
            "custom_extensions": [
                extension(
                    {
                        "element": "metric",
                        "over_metrics": {
                            "expression": "sales.p90 * 2",
                            "align": {"grain": ["month"], "time": "month"},
                        },
                    }
                )
            ],
        }
    ]
    for typed in typed_both(document, engines={"sales": "DUCKDB"}):
        assert typed.members[MemberId("metric", "SALES", "P90")].type.exactness == "unknown"
        assert typed.members[m("doubled")].type.exactness == "unknown"


def test_a_calculated_expression_buckets_in_its_fields_own_zone() -> None:
    calculated = {"expression": "DATE_TRUNC('DAY', zoned)", "max_cardinality": "100"}
    document = base(
        fields=[
            field("zoned", "sold_at", time_zone={"name": "America/La_Paz"}),
            field("day", "sold_at", time_zone={"name": "Asia/Tokyo"}, calculated=calculated),
        ],
        model={"default_time_zone": "UTC"},
    )
    for typed in typed_both(document):
        (label, tree), *_ = typed.members[f("day")].payload
        assert label == "calculated"
        assert tree.type.bucket_zone == "Asia/Tokyo"


# --- P2-04d (#290): units and currencies -------------------------------------------------------


def measured(*metrics: Model, model: Model | None = None) -> Model:
    """The base model with fields that declare units and currencies."""
    return base(
        *metrics,
        fields=[
            field("amount_usd", "amount", semantic_type="amount", currency="USD"),
            field("amount_bob", "amount", semantic_type="amount", currency="BOB"),
            field("rate", "unit_price", semantic_type="quantity", unit="BOB/USD"),
            field("energy", "unit_price", semantic_type="quantity", unit="kW.h"),
            field("power", "unit_price", semantic_type="quantity", unit="kW"),
            field("hours", "quantity", semantic_type="quantity", unit="h"),
            field("distance", "unit_price", semantic_type="quantity", unit="km"),
            field("metres", "unit_price", semantic_type="quantity", unit="m"),
        ],
        model=model,
    )


def unit_of(document: Model, member: MemberId) -> list[str | None]:
    found = []
    for typed in typed_both(document):
        unit = typed.members[member].type.unit
        found.append(None if unit is None else unit.text)
    return found


def incompatible(document: Model) -> list[list[str]]:
    found = []
    for error in failure_both(document):
        assert error.code == "INCOMPATIBLE_UNITS"
        found.append(list(cast(list[str], error.details["units"])))
    return found


def test_case_017_dollars_plus_bolivianos_is_incompatible() -> None:
    document = measured(
        metric("revenue_usd", "SUM(sales.amount_usd)"),
        metric("revenue_bob", "SUM(sales.amount_bob)"),
        derived_metric("combined", "revenue_usd + revenue_bob"),
    )
    assert incompatible(document) == [["USD", "BOB"], ["USD", "BOB"]]
    document = measured(metric("x", "SUM(sales.amount_usd + sales.amount_bob)"))
    assert incompatible(document) == [["USD", "BOB"], ["USD", "BOB"]]


def derived_metric(name: str, text: str) -> Model:
    payload = {
        "version": 1,
        "element": "metric",
        "over_metrics": {"expression": text, "align": {"grain": ["month"], "time": "month"}},
    }
    return {
        "name": name,
        "expression": {"dialects": [{"dialect": CURRENT, "expression": text}]},
        "custom_extensions": [{"vendor_name": CURRENT, "data": json.dumps(payload)}],
    }


def test_dollars_times_a_rate_in_bolivianos_per_dollar_is_bolivianos() -> None:
    document = measured(
        metric("converted", "SUM(sales.amount_usd * sales.rate)"),
        metric("declared", "SUM(sales.amount_usd * sales.rate)", currency="BOB"),
    )
    assert unit_of(document, m("converted")) == ["BOB", "BOB"]
    assert unit_of(document, m("declared")) == ["BOB", "BOB"]
    wrong = measured(metric("x", "SUM(sales.amount_usd * sales.rate)", currency="USD"))
    assert incompatible(wrong) == [["USD", "BOB"], ["USD", "BOB"]]


def test_an_unknown_unit_or_currency_is_an_extension_error() -> None:
    pointer = "/datasets/0/fields/14/custom_extensions/0/data"
    for payload in ({"unit": "xyz"}, {"unit": "kh"}, {"currency": "ZZZ"}, {"currency": "XXX"}):
        kind = "amount" if "currency" in payload else "quantity"
        document = base(fields=[field("x", "amount", semantic_type=kind, **payload)])
        for error in failure_both(document):
            assert problems(error) == [("extension", pointer)]
    document = base(metric("x", "SUM(sales.amount)", unit="kg", currency="USD"))
    for error in failure_both(document):
        assert problems(error) == [("extension", "/metrics/0/custom_extensions/0/data")]


def test_kw_times_h_is_kw_h_and_nothing_is_converted() -> None:
    document = measured(metric("x", "SUM(sales.power * sales.hours) + SUM(sales.energy)"))
    assert unit_of(document, m("x")) == ["kW.h", "kW.h"]
    assert incompatible(measured(metric("x", "SUM(sales.distance + sales.metres)"))) == [
        ["km", "m"],
        ["km", "m"],
    ]


@pytest.mark.parametrize(
    ("expression", "units"),
    [
        ("SUM(sales.amount_usd + sales.amount)", ["USD", "undeclared"]),  # undeclared never adds
        ("SUM(sales.amount_usd) + COUNT(sales.sale_id)", ["USD", "1"]),
        ("COUNT(sales.sale_id) FILTER (WHERE sales.amount_usd > sales.amount_bob)", ["USD", "BOB"]),
        (
            "SUM(CASE WHEN sales.flag THEN sales.amount_usd ELSE sales.amount_bob END)",
            ["USD", "BOB"],
        ),
        ("SUM(COALESCE(sales.amount_usd, sales.amount_bob))", ["USD", "BOB"]),
        ("SUM(GREATEST(sales.energy, sales.power))", ["kW.h", "kW"]),
        ("MAX(SQRT(sales.energy))", ["kW.h", "1"]),
        ("MAX(LN(sales.energy))", ["kW.h", "1"]),
    ],
)
def test_mixed_units_are_incompatible(expression: str, units: list[str]) -> None:
    assert incompatible(measured(metric("x", expression))) == [units, units]


@pytest.mark.parametrize(
    ("expression", "unit"),
    [
        ("SUM(sales.amount_usd) + 1", "USD"),  # a bare literal takes its partner's unit
        ("SUM(sales.amount_usd * 2)", "USD"),
        ("COUNT(sales.amount_usd)", "1"),
        ("AVG(sales.energy)", "kW.h"),
        ("MIN(sales.energy)", "kW.h"),
        ("MAX(sales.energy)", "kW.h"),
        ("MEDIAN(sales.energy)", "kW.h"),
        ("VARIANCE(sales.amount_usd)", "USD2"),
        ("SUM(sales.amount_usd) / SUM(sales.hours)", "USD/h"),
        ("SUM(sales.power * sales.hours) / SUM(sales.energy)", "1"),
        ("SUM(sales.amount)", None),
        ("SUM(sales.amount_usd * sales.amount)", None),  # undeclared stays undeclared
    ],
)
def test_aggregates_and_products_carry_units(expression: str, unit: str | None) -> None:
    assert unit_of(measured(metric("x", expression)), m("x")) == [unit, unit]


def test_a_count_is_an_integer_with_unit_one_and_never_null() -> None:
    for typed in typed_both(measured(metric("x", "COUNT(sales.amount_usd)"))):
        x = typed.members[m("x")].type
        assert (x.logical.datatype, x.nullable) == ("Integer", False)
        assert x.unit is not None
        assert x.unit.dimensions == ()


_SPELLINGS = [
    ["kW.h", "h.kW", "W.h.10*3"],
    ["kW"],
    ["L", "dm3", "l"],
    ["m3"],
    ["USD"],
    ["BOB", "USD.BOB/USD"],
    ["km", "m.10*3"],
    ["m"],
]


@settings(max_examples=60, deadline=None)
@given(st.integers(0, 7), st.integers(0, 7), st.data())
def test_addition_succeeds_iff_normalized_units_are_equal(
    a: int, b: int, data: st.DataObject
) -> None:
    left = data.draw(st.sampled_from(_SPELLINGS[a]))
    right = data.draw(st.sampled_from(_SPELLINGS[b]))
    document = base(
        metric("x", "SUM(sales.left_value + sales.right_value)"),
        fields=[
            field("left_value", "unit_price", semantic_type="quantity", unit=left),
            field("right_value", "unit_price", semantic_type="quantity", unit=right),
        ],
    )
    loaded = read(content(document, "json"), "semantic/shop.ossie.json", VENDORS)
    parsed = parse_model(loaded, VENDORS)
    if a == b:
        typed = type_model(loaded, parsed, CATALOG)
        assert typed.members[m("x")].type.unit is not None
    else:
        with pytest.raises(SemanticError) as caught:
            type_model(loaded, parsed, CATALOG)
        assert caught.value.code == "INCOMPATIBLE_UNITS"
