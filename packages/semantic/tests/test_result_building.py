"""`build_result` (P2-06b, #293): descriptors, the Arrow form, and every fail-closed refusal."""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from orvex.semantic import SemanticError
from orvex.semantic.result import (
    KNOWN_STATES,
    CellState,
    ColumnSpec,
    InvalidResult,
    SemanticResult,
    build_result,
)
from orvex.semantic.result.descriptors import INTEGERS, OTHERS, pair_allowed

from tests.support.semantic_results import (
    MODEL,
    QUERY,
    aggregate,
    decimal,
    detail,
    integer,
    masked,
    spec,
    table,
    text,
    top_n_with_others,
)

ROOT = Path(__file__).resolve().parents[3]
README = ROOT / "contracts/semantic-result/v1/README.md"


def reason(call: Callable[[], object]) -> str:
    with pytest.raises(InvalidResult) as caught:
        call()
    return caught.value.reason


def one(
    column: ColumnSpec,
    values: pa.Array,
    **extra: Any,  # noqa: ANN401
) -> Callable[[], SemanticResult]:
    return lambda: detail(table({column.name: values}), [column], **extra)


# --- descriptors ---------------------------------------------------------------------------


def test_a_decimal_column_states_its_logical_type_and_encoding() -> None:
    result = detail(
        table({"amount": pa.array([1250], pa.decimal128(18, 2))}), [decimal("amount", 18, 2)]
    )
    (column,) = result.envelope()["columns"]
    assert column["logical"] == {"family": "Decimal", "precision": 18, "scale": 2}
    assert column["encoding"] == "DECIMAL(18,2)"
    assert column["exactness"] == "exact"


def test_the_package_reads_the_contracts_own_schema() -> None:
    packaged = ROOT / "packages/semantic/src/orvex/semantic/result/semantic-result-v1.json"
    assert packaged.read_bytes() == (ROOT / "contracts/semantic-result/v1/schema.json").read_bytes()


def readme_pairs() -> list[tuple[str, list[str]]]:
    """The README's mapping table: each `logical` row and its encodings."""
    section = README.read_text("utf-8").split("These are the only allowed pairs:")[1]
    pairs = []
    for line in section.split("\n\n")[1].splitlines()[2:]:
        logical, encodings = (cell.strip() for cell in line.strip("|").split("|"))
        pairs.append((logical, re.findall(r"`([A-Z][A-Z ]*(?:\(p,s\))?)`", encodings)))
    return pairs


def family_of(logical_text: str) -> str:
    match = re.match(r"`([A-Za-z]+)", logical_text)
    assert match is not None, logical_text
    return match[1]


def test_every_logical_family_maps_to_its_encodings_through_the_readme_table() -> None:
    table_pairs = readme_pairs()
    assert {family_of(logical) for logical, _ in table_pairs} == {
        "String", "Boolean", "Integer", "Decimal", "Float",
        "Date", "DateTime", "DateTimeTz", "Opaque",
    }  # fmt: skip
    for logical_text, encodings in table_pairs:
        family = family_of(logical_text)
        for encoding in encodings:
            if family == "Integer":
                width, signed, _ = INTEGERS[encoding]
                logical: dict[str, Any] = {"family": "Integer", "width": width, "signed": signed}
                assert ("unsigned" not in logical_text) == signed
            elif family == "Decimal":
                encoding, logical = (
                    "DECIMAL(9,3)",
                    {"family": "Decimal", "precision": 9, "scale": 3},
                )
            else:
                logical = {"family": family}
            assert pair_allowed(logical, encoding), (logical, encoding)
            others = [e for e in [*INTEGERS, *OTHERS, "DECIMAL(9,3)"] if e not in encodings]
            assert not any(pair_allowed(logical, other) for other in others if other != encoding)


def test_no_descriptor_member_but_encoding_names_a_store_type() -> None:
    store_types = {*INTEGERS, *OTHERS, "DECIMAL"}
    result = top_n_with_others()
    for column in result.envelope()["columns"]:
        for key, value in column.items():
            if key != "encoding":
                assert not any(kind in json.dumps(value) for kind in store_types), (key, value)
        assert "logical" in column


def test_role_follows_the_mode() -> None:
    column = text("region", role="dimension")
    assert reason(one(column, pa.array(["north"]))) == "role"
    data = table({"region": pa.array(["north"])})
    assert reason(lambda: aggregate(data, [text("region", role="field")])) == "role"


def test_a_float_is_approximate() -> None:
    column = spec("ratio", "DOUBLE", {"family": "Float"}, exactness="exact")
    assert reason(one(column, pa.array([0.5]))) == "float_exact"


def test_a_column_states_its_logical_type_and_exactness() -> None:
    assert reason(one(spec("x", "VARCHAR", None), pa.array(["a"]))) == "logical_missing"
    column = spec("x", "VARCHAR", {"family": "String"}, exactness=None)
    assert reason(one(column, pa.array(["a"]))) == "exactness_missing"


# --- fail closed (§2) ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("column", "values", "expected"),
    [
        (decimal("amount", 18, 2), pa.array([12.5], pa.float64()), "exact_as_float"),
        (integer("n", "BIGINT", 64, True), pa.array([1.0], pa.float64()), "exact_as_float"),
        (decimal("amount", 18, 2), pa.array([1250], pa.decimal128(18, 3)), "arrow_type"),
        (integer("n", "BIGINT", 64, True), pa.array([1], pa.int32()), "arrow_type"),
        (integer("n", "UHUGEINT", 128, False), pa.array([1], pa.uint64()), "arrow_type"),
        (spec("d", "DECIMAL(18,2)", {"family": "Decimal", "precision": 18, "scale": 3}),
         pa.array([1]), "logical_encoding"),
        (spec("t", "VARCHAR", {"family": "Time"}), pa.array(["10:00"]), "time_column"),
        (spec("n", "BIGINT", {"family": "Integer", "width": 64, "signed": False}),
         pa.array([1]), "logical_encoding"),
        (text("name", nullable=False), pa.array(["a", None]), "null_in_non_nullable"),
    ],
)  # fmt: skip
def test_types_that_disagree_are_refused(
    column: ColumnSpec, values: pa.Array, expected: str
) -> None:
    assert reason(one(column, values)) == expected


def test_the_columns_must_be_the_tables() -> None:
    data = table({"a": pa.array(["x"]), "b": pa.array(["y"])})
    assert reason(lambda: detail(data, [text("a")])) == "columns"
    assert reason(lambda: detail(data, [text("b"), text("a")])) == "columns"
    twice = pa.Table.from_arrays([pa.array(["x"]), pa.array(["y"])], names=["a", "a"])
    assert reason(lambda: detail(twice, [text("a"), text("a")])) == "duplicate_name"


def test_a_declared_transport_type_must_be_the_encoding() -> None:
    field = pa.field("amount", pa.decimal128(18, 2), metadata={"duckdb_type": "DECIMAL(18,3)"})
    data = pa.Table.from_arrays([pa.array([1], pa.decimal128(18, 2))], schema=pa.schema([field]))
    assert reason(lambda: detail(data, [decimal("amount", 18, 2)])) == "arrow_metadata"


@pytest.mark.parametrize(
    "values",
    [
        pa.array(["***", "0412 555 000"]),  # a real value
        pa.array(["***", None]),  # a real NULL: the plan selects the mask for it too
        pa.array([1, 2], pa.int64()),  # a typed column
    ],
)
def test_a_masked_column_holds_only_its_mask(values: pa.Array) -> None:
    assert reason(one(masked("phone"), values)) == "masked_values"


def test_a_masked_column_appears_only_in_detail_mode() -> None:
    data = table({"phone": pa.array(["***"])})
    column = masked("phone", role="dimension")
    assert reason(lambda: aggregate(data, [column])) == "masked_in_aggregate"


def test_a_notice_is_never_an_error() -> None:
    data = table({"name": pa.array(["a"])})
    error = SemanticError("MEMBER_NOT_FOUND", {"member": "sales.secret"})
    assert reason(lambda: detail(data, [text("name")], notices=[error])) == "notice_severity"


def test_the_envelope_is_checked_against_the_schema() -> None:
    data = table({"name": pa.array(["a"])})
    assert reason(lambda: detail(data, [text("name")], page={"truncated": False})) == "schema"
    wrong = lambda: build_result(  # noqa: E731
        data, [text("name")], mode="detail", query_fingerprint="sha256:" + "A" * 64,
        model_fingerprint=MODEL, page={"size": 10},
    )  # fmt: skip
    assert reason(wrong) == "schema"


# --- cell states (§1.4) --------------------------------------------------------------------


def states_case(
    states: list[CellState],
    **extra: Any,  # noqa: ANN401
) -> Callable[[], SemanticResult]:
    data = table(
        {
            "region": pa.array(["north", None]),
            "phone": pa.array(["***", "***"]),
            "revenue": pa.array([10, None], pa.decimal128(18, 2)),
        }
    )
    columns = [text("region"), masked("phone"), decimal("revenue", 18, 2)]
    return lambda: detail(data, columns, cell_states=states, **extra)


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ([CellState(1, 2, "undefined_at_total"), CellState(1, 2, "undefined_at_total")],
         "state_duplicate"),
        ([CellState(1, 2, "undefined_at_total"), CellState(1, 2, "others")], "state_duplicate"),
        ([CellState(1, 2, "undefined_at_total"), CellState(1, 0, "undefined_at_total")],
         "state_order"),
        ([CellState(2, 0, "undefined_at_total")], "state_position"),
        ([CellState(0, 3, "undefined_at_total")], "state_position"),
        ([CellState(-1, 0, "undefined_at_total")], "state_position"),
        ([CellState(1, 1, "undefined_at_total")], "state_on_masked"),
        ([CellState(0, 2, "undefined_at_total")], "state_not_null"),
        ([CellState(1, 2, "future_state")], "state_unknown"),
        ([CellState(1, 0, "others")], "others_without_row"),
    ],
)  # fmt: skip
def test_each_violation_of_the_state_rule_is_refused(
    states: list[CellState], expected: str
) -> None:
    assert reason(states_case(states)) == expected


def test_others_sit_exactly_on_the_dimension_cells_of_others_row() -> None:
    data = table(
        {
            "region": pa.array(["north", None]),
            "channel": pa.array(["web", None]),
            "revenue": pa.array([10, 5], pa.decimal128(18, 2)),
        }
    )
    columns = [
        text("region", role="dimension"),
        text("channel", role="dimension"),
        decimal("revenue", 18, 2, role="metric"),
    ]

    def build(states: list[CellState], others_row: int | None) -> Callable[[], SemanticResult]:
        return lambda: aggregate(data, columns, states=states, others_row=others_row)

    both = [CellState(1, 0, "others"), CellState(1, 1, "others")]
    assert build(both, 1)().others_row == 1
    assert reason(build(both[:1], 1)) == "others_placement"  # a dimension cell lacks it
    assert reason(build(both, 0)) == "others_placement"  # off others_row
    assert reason(build([], 1)) == "others_placement"  # others_row without entries
    off_dimension = [*both, CellState(1, 2, "others")]
    assert reason(build(off_dimension, 1)) == "others_placement"
    assert reason(build(both, 2)) == "others_row_position"


def test_the_library_never_emits_an_unlisted_state() -> None:
    assert {"undefined_at_total", "others"} == KNOWN_STATES
    for state in ["future_state", "Others", "null", ""]:
        assert reason(states_case([CellState(1, 2, state)])) == "state_unknown"


# --- the Arrow form ------------------------------------------------------------------------


def product_names() -> set[str]:
    identity = tomllib.loads((ROOT / "identity.toml").read_text("utf-8"))["product"]
    return {str(value).lower() for value in identity.values() if isinstance(value, str)}


def test_the_arrow_form_carries_the_descriptor_and_the_states_under_neutral_keys() -> None:
    result = top_n_with_others()
    schema = result.arrow().schema
    assert schema.metadata == {
        b"semantic.cell_states": b'[{"column":0,"row":2,"state":"others"},'
        b'{"column":2,"row":2,"state":"undefined_at_total"}]',
        b"semantic.others_row": b"2",
    }
    revenue = schema.field("revenue")
    assert revenue.type == pa.decimal128(18, 2)
    assert revenue.metadata[b"duckdb_type"] == b"DECIMAL(18,2)"
    assert revenue.metadata[b"semantic.logical"] == b'{"family":"Decimal","precision":18,"scale":2}'
    assert revenue.metadata[b"semantic.role"] == b"metric"
    keys = {key.decode() for field in schema for key in field.metadata} | {
        key.decode() for key in schema.metadata
    }
    assert all(key == "duckdb_type" or key.startswith("semantic.") for key in keys)
    assert not any(name in key.lower() for key in keys for name in product_names())


# --- masking (security §3.3) ----------------------------------------------------------------


def test_a_masked_member_discloses_nothing_of_itself() -> None:
    """Canaries in the member's logical type, source type, unit, semantic type and nullity, and
    in the incoming Arrow metadata, never reach the descriptor, the Arrow form or the envelope."""
    canaries = ["Canary/Zone", "DECIMAL(37,9)", "canary_unit", "geo_region", "CANARYSRC"]
    phone = ColumnSpec(
        name="phone",
        member={"dataset": "consumers", "name": "phone"},
        role="field",
        encoding="DECIMAL(37,9)",
        nullable=True,
        logical={"family": "DateTimeTz", "zone": "Canary/Zone"},
        semantic_type="geo_region",
        unit="canary_unit",
        exactness="approximate",
        mask="***",
    )
    field = pa.field("phone", pa.string(), metadata={"source": "CANARYSRC"})
    data = pa.Table.from_arrays(
        [pa.array(["***", "***"])], schema=pa.schema([field], metadata={b"x": b"CANARYSRC"})
    )
    result = detail(data, [phone])
    (column,) = result.envelope()["columns"]
    assert column == {
        "name": "phone",
        "member": {"dataset": "consumers", "name": "phone"},
        "role": "field",
        "encoding": "VARCHAR",
        "nullable": False,
        "masked": True,
        "mask": "***",
    }
    arrow = result.arrow()
    places = [
        json.dumps(result.envelope()),
        str(arrow.schema.metadata),
        str(arrow.schema.field("phone").metadata),
        str(arrow.schema),
    ]
    for canary in [*canaries, "exactness", "approximate"]:
        assert not any(canary in place for place in places), canary
    assert arrow.schema.field("phone").nullable is False


# --- notices --------------------------------------------------------------------------------


def test_an_empty_filter_intersection_gives_zero_rows_and_its_notice() -> None:
    data = table({"region": pa.array([], pa.string()), "phone": pa.array([], pa.string())})
    columns = [text("region"), masked("phone")]  # an empty page with a masked column too
    notice = SemanticError("FILTER_INTERSECTION_EMPTY", {"member": "consumers.region"})
    builder = detail(data, columns, notices=[notice], audience="builder", locale="en")
    other = detail(data, columns, notices=[notice], locale="en")
    assert builder.envelope()["row_count"] == 0
    assert builder.envelope()["notices"] == [
        {
            "kind": "semantic_error",
            "code": "FILTER_INTERSECTION_EMPTY",
            "severity": "notice",
            "category": "query",
            "audience": "builder",
            "message": "The filters on consumers.region exclude each other, so no rows match.",
            "locale": "en",
            "details": {"member": "consumers.region"},
        }
    ]
    (seen,) = other.envelope()["notices"]
    assert seen["audience"] == "other"
    assert seen["details"] == {}
    assert "consumers.region" not in json.dumps(other.envelope())
    assert "consumers.region" not in str(other.arrow().schema)


def test_only_a_masked_field_is_non_nullable_in_arrow() -> None:
    """A state is Arrow null even in a column whose values are never NULL, so the Arrow field
    stays nullable; `semantic.nullable` keeps what the descriptor says."""
    data = table({"region": pa.array(["north", None]), "units": pa.array([3, None])})
    columns = [text("region", role="dimension"), integer("units", "BIGINT", 64, True,
               role="metric", nullable=False)]  # fmt: skip
    states = [CellState(1, 0, "others"), CellState(1, 1, "undefined_at_total")]
    schema = aggregate(data, columns, states=states, others_row=1).arrow().schema
    assert schema.field("units").nullable is True
    assert schema.field("units").metadata[b"semantic.nullable"] == b"false"
    phone = detail(table({"phone": pa.array(["***"])}), [masked("phone")]).arrow().schema
    assert phone.field("phone").nullable is False


def test_the_detail_last_page_has_no_cursor_and_a_page_may_carry_one() -> None:
    data = table({"region": pa.array(["north"])})
    assert detail(data, [text("region")]).envelope()["page"] == {"size": 100}
    page = {"size": 1, "next": "AbC-_9"}
    assert detail(data, [text("region")], page=page).envelope()["page"] == page


def test_the_result_is_immutable() -> None:
    result = top_n_with_others()
    result.envelope()["columns"].clear()
    assert len(result.envelope()["columns"]) == 3
    with pytest.raises(AttributeError):
        result.extra = 1  # type: ignore[attr-defined]
    assert result.envelope()["query_fingerprint"] == QUERY
