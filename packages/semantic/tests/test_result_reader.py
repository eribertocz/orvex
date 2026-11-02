"""The reference reader of `semantic-result` v1 (P2-06b, #293).

The fixtures beside this file hold what a JSON Schema cannot refuse, since it cannot relate a
cell to its column by position: each invalid fixture names the rule it breaks.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from orvex.contracts.semantic_result.v1 import SemanticResult as GeneratedResult
from orvex.contracts.semantic_result.v1 import StateCell
from orvex.semantic.canonical import canonicalize
from orvex.semantic.result import InvalidResult, NoValue, read_result
from orvex.semantic.result.building import CELL_STATES_KEY, schema_validator

from tests.support.semantic_results import top_n_with_others

FIXTURES = Path(__file__).with_name("result_fixtures")
ROOT = Path(__file__).resolve().parents[3]
CONTRACT_EXAMPLES = ROOT / "contracts/semantic-result/v1/examples"


def load(path: Path) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(path.read_text("utf-8"))
    return document


def reason(document: str | dict[str, Any], arrow: pa.Table | None = None) -> str:
    with pytest.raises(InvalidResult) as caught:
        read_result(document, arrow)
    return caught.value.reason


VALID = sorted((FIXTURES / "valid").glob("*.json")) + sorted(
    (CONTRACT_EXAMPLES / "valid").glob("*.json")
)
INVALID = sorted((FIXTURES / "invalid").glob("*.json"))


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.stem)
def test_valid_results_are_read(path: Path) -> None:
    document = load(path)
    assert not list(schema_validator().iter_errors(document))
    read = read_result(document)
    assert len(read.cells) == document["row_count"]


@pytest.mark.parametrize("path", INVALID, ids=lambda p: p.stem)
def test_each_positional_rule_is_refused(path: Path) -> None:
    document = load(path)
    # The schema accepts each fixture: only the reader can refuse it.
    assert not list(schema_validator().iter_errors(document)), path.stem
    expected = path.with_suffix(".expected.txt").read_text("utf-8").strip()
    assert reason(document) == expected


@pytest.mark.parametrize("path", sorted((CONTRACT_EXAMPLES / "invalid").glob("*.json")))
def test_the_reader_refuses_the_contracts_invalid_examples(path: Path) -> None:
    assert reason(load(path)) in {"schema", "logical_encoding", "time_column"}


def test_the_four_kinds_of_cell_stay_distinct() -> None:
    read = read_result(load(FIXTURES / "valid/detail-four-kinds.json"))
    rows = [[cell.kind for cell in row] for row in read.cells]
    assert rows == [
        ["value", "mask", "value", "value"],
        ["value", "mask", "null", "value"],
        ["null", "mask", "value", "value"],
    ]
    assert read.cells[0][0].value == "***"  # the mask's text, in a column that is not masked
    assert read.cells[1][0].value == ""
    assert read.cells[0][2].value == "1250.00"
    for cell in (read.cells[1][2], read.cells[0][1], read.cells[2][0]):
        with pytest.raises(NoValue):
            _ = cell.value


def test_an_unknown_state_is_unavailable_never_null_zero_or_a_value() -> None:
    read = read_result(load(FIXTURES / "valid/unknown-state.json"))
    cell = read.cells[1][2]
    assert cell.kind == "unavailable"
    assert cell.state == "future_state"
    with pytest.raises(NoValue):
        _ = cell.value
    assert cell.kind not in {"null", "value"}
    # A calculation over the column's values leaves it out; it is not counted as zero.
    values = [row[2].value for row in read.cells if row[2].kind == "value"]
    assert values == ["1250.00", "7.00"]


def test_known_states_are_states() -> None:
    read = read_result(load(FIXTURES / "valid/aggregate-others-and-total.json"))
    assert [(cell.kind, cell.state) for cell in read.cells[2]] == [
        ("state", "others"),
        ("value", None),
        ("state", "undefined_at_total"),
    ]


def test_the_generated_python_reader_accepts_an_unknown_state_as_a_state() -> None:
    document = load(FIXTURES / "valid/unknown-state.json")
    generated = GeneratedResult.model_validate(document)
    cell = getattr(generated.rows[1][2], "root", generated.rows[1][2])
    assert isinstance(cell, StateCell)
    assert cell.state.root == "future_state"


# --- Arrow and JSON state the same cells ----------------------------------------------------


@pytest.fixture
def pair() -> tuple[dict[str, Any], pa.Table]:
    """The JSON form of `top_n_with_others` (from its fixture) and its Arrow form."""
    return load(FIXTURES / "valid/aggregate-others-and-total.json"), top_n_with_others().arrow()


def test_both_forms_agree(pair: tuple[dict[str, Any], pa.Table]) -> None:
    document, arrow = pair
    assert read_result(document, arrow).cells[2][0].state == "others"


def test_arrow_metadata_that_differs_from_the_envelope_is_refused(
    pair: tuple[dict[str, Any], pa.Table],
) -> None:
    document, arrow = pair
    metadata = dict(arrow.schema.metadata)
    metadata[CELL_STATES_KEY] = canonicalize([{"row": 2, "column": 0, "state": "others"}])
    assert reason(document, arrow.replace_schema_metadata(metadata)) == "arrow_metadata"
    spaced = json.dumps(document["cell_states"]).encode()
    metadata[CELL_STATES_KEY] = spaced  # the same entries, not byte-identical
    assert reason(document, arrow.replace_schema_metadata(metadata)) == "arrow_metadata"


def test_a_value_under_a_state_in_arrow_is_refused(pair: tuple[dict[str, Any], pa.Table]) -> None:
    document, arrow = pair
    growth = pa.chunked_array([pa.array([0.5, -0.25, 1.0])])
    index = arrow.schema.get_field_index("growth")
    changed = arrow.set_column(index, arrow.schema.field("growth"), growth)
    assert reason(document, changed) == "state_not_null"


def test_an_arrow_null_is_a_json_null_and_never_a_value(
    pair: tuple[dict[str, Any], pa.Table],
) -> None:
    document, arrow = pair
    changed = copy.deepcopy(document)
    changed["rows"][0][1] = None
    assert reason(changed, arrow) == "arrow_disagrees"


def test_an_arrow_field_must_carry_its_descriptor(pair: tuple[dict[str, Any], pa.Table]) -> None:
    document, arrow = pair
    field = arrow.schema.field("revenue")
    metadata = dict(field.metadata)
    metadata[b"semantic.unit"] = b"kWh"
    index = arrow.schema.get_field_index("revenue")
    changed = arrow.set_column(index, field.with_metadata(metadata), arrow.column(index))
    assert reason(document, changed) == "arrow_metadata"
    as_float = pa.field("revenue", pa.float64(), metadata=field.metadata)
    floats = pa.chunked_array([pa.array([1250.0, 990.0, 310.0])])
    assert reason(document, arrow.set_column(index, as_float, floats)) == "arrow_type"


def test_the_reader_refuses_json_that_is_not_strict() -> None:
    text = (FIXTURES / "valid/detail-four-kinds.json").read_text("utf-8")
    assert reason(text.replace('"row_count": 3', '"row_count": 3, "row_count": 3')) == "json"
    assert reason(text.replace('"7.00"', "NaN")) == "json"
