"""`semantic-result` v1 with the real `store/v1` `ResultJson` encoder (P2-06b, #293).

The composing module passes `store`'s encoder to `encode_rows`; this test does the same, so
exactness is checked end to end: Arrow, `ResultJson`, the envelope, and back.
"""

from __future__ import annotations

import decimal
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
from hypothesis import given, settings
from hypothesis import strategies as st
from orvex.semantic import SemanticError
from orvex.semantic.result import CellState, SemanticResult, read_result
from orvex.store.json_encoding import arrow_to_json

from tests.support.semantic_results import (
    decimal as decimal_column,
)
from tests.support.semantic_results import (
    detail,
    integer,
    masked,
    spec,
    table,
    text,
    top_n_with_others,
)

ROOT = Path(__file__).resolve().parents[2]
HUGEINT_MIN = "-170141183460469231731687303715884105728"
UBIGINT_MAX = "18446744073709551615"
UHUGEINT_MAX = str(2**128 - 1)
DECIMAL_38_10 = "12345678901234567890.0123456789"


def wire(result: SemanticResult) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(result.wire(arrow_to_json))
    return document


def arrow_of(sql: str) -> pa.Table:
    """`sql`'s result on a private connection: the module-wide default one may be busy with
    another test's unfinished result."""
    with duckdb.connect() as connection:
        relation = connection.sql(sql)
        assert relation is not None
        return relation.to_arrow_table()


def exact_table() -> pa.Table:
    selected = arrow_of(
        f"SELECT {DECIMAL_38_10}::DECIMAL(38,10) AS amount, '{HUGEINT_MIN}'::HUGEINT AS huge, "
        f"'{UBIGINT_MAX}'::UBIGINT AS ubig"
    )
    return selected.append_column("uhuge", pa.array([UHUGEINT_MAX]))


EXACT_COLUMNS = [
    decimal_column("amount", 38, 10),
    integer("huge", "HUGEINT", 128, True),
    integer("ubig", "UBIGINT", 64, False),
    integer("uhuge", "UHUGEINT", 128, False),
]


def test_exact_values_round_trip_without_change() -> None:
    source = exact_table()
    result = detail(source, EXACT_COLUMNS)
    document = wire(result)
    assert document["rows"] == [[DECIMAL_38_10, HUGEINT_MIN, UBIGINT_MAX, UHUGEINT_MAX]]
    read = read_result(json.dumps(document), result.arrow())
    back = [cell.value for cell in read.cells[0]]
    assert decimal.Decimal(str(back[0])) == source.column("amount")[0].as_py()
    assert int(str(back[1])) == source.column("huge")[0].as_py() == -(2**127)
    assert int(str(back[2])) == source.column("ubig")[0].as_py() == 2**64 - 1
    assert int(str(back[3])) == 2**128 - 1
    assert all(isinstance(value, str) for value in back)


@settings(max_examples=200, deadline=None)
@given(data=st.data(), precision=st.integers(1, 38))
def test_decimals_of_every_precision_and_scale_stay_exact(
    data: st.DataObject, precision: int
) -> None:
    scale = data.draw(st.integers(0, precision))
    digits = data.draw(st.integers(-(10**precision) + 1, 10**precision - 1))
    value = decimal.Decimal(f"{digits}e-{scale}")  # exact: no context rounding
    source = arrow_of(
        f"SELECT d FROM (VALUES (1, '{value:f}'::DECIMAL({precision},{scale})), (2, NULL)) t(i, d) "
        "ORDER BY i"
    )
    result = detail(source, [decimal_column("d", precision, scale)])
    cell = wire(result)["rows"][0][0]
    assert isinstance(cell, str)
    assert decimal.Decimal(cell) == value
    assert len(cell.partition(".")[2]) == scale
    canonical = json.loads(result.canonical(arrow_to_json))
    assert canonical["rows"] == [[cell], [None]]


def test_the_four_kinds_of_cell_stay_distinct_in_both_forms() -> None:
    source = table(
        {
            "name": pa.array(["***", "", None, "ana"]),
            "phone": pa.array(["***"] * 4),
            "growth": pa.array([0.5, None, -0.0, None], pa.float64()),
        }
    )
    columns = [
        text("name"),
        masked("phone"),
        spec("growth", "DOUBLE", {"family": "Float"}),
    ]
    result = detail(source, columns, cell_states=[CellState(3, 2, "undefined_at_total")])
    document = wire(result)
    assert document["rows"] == [
        ["***", "***", 0.5],
        ["", "***", None],
        [None, "***", -0.0],
        ["ana", "***", {"state": "undefined_at_total"}],
    ]
    read = read_result(json.dumps(document), result.arrow())
    assert [[cell.kind for cell in row] for row in read.cells] == [
        ["value", "mask", "value"],
        ["value", "mask", "null"],
        ["null", "mask", "value"],
        ["value", "mask", "state"],
    ]
    arrow = result.arrow()
    assert arrow.column("growth").null_count == 2  # a NULL and a state, told apart by cell_states
    assert arrow.schema.metadata[b"semantic.cell_states"] == (
        b'[{"column":2,"row":3,"state":"undefined_at_total"}]'
    )


def test_states_survive_both_forms_and_the_forms_round_trip() -> None:
    result = top_n_with_others()
    document = wire(result)
    assert document["rows"][2] == [{"state": "others"}, "310.00", {"state": "undefined_at_total"}]
    assert document["others_row"] == 2
    read = read_result(json.dumps(document), result.arrow())
    assert [cell.kind for cell in read.cells[2]] == ["state", "value", "state"]
    assert read.envelope == result.envelope()


def test_the_canonical_form_keeps_every_value_exact() -> None:
    source = table(
        {
            "x": pa.array([-0.0, float("nan"), float("inf"), float("-inf"), 0.1], pa.float64()),
            "f": pa.array([0.1, None, None, None, 1.5], pa.float32()),
            "n": pa.array([1, 2, 3, 4, -5], pa.int32()),
        }
    )
    columns = [
        spec("x", "DOUBLE", {"family": "Float"}),
        spec("f", "FLOAT", {"family": "Float"}),
        integer("n", "INTEGER", 32, True),
    ]
    result = detail(source, columns)
    assert wire(result)["rows"][0] == [-0.0, 0.1, 1]
    canonical = result.canonical(arrow_to_json)
    rows = json.loads(canonical)["rows"]
    assert rows == [
        ["-0.0", "0.1", "1"],
        ["NaN", None, "2"],
        ["Infinity", None, "3"],
        ["-Infinity", None, "4"],
        ["0.1", "1.5", "-5"],
    ]
    exact = detail(exact_table(), EXACT_COLUMNS).canonical(arrow_to_json)
    assert json.loads(exact)["rows"] == [[DECIMAL_38_10, HUGEINT_MIN, UBIGINT_MAX, UHUGEINT_MAX]]


CANONICAL_SCRIPT = """
import hashlib, sys
sys.path.insert(0, {root!r})
from orvex.store.json_encoding import arrow_to_json
from tests.conformance.test_semantic_result import detail, exact_table, EXACT_COLUMNS
from tests.support.semantic_results import top_n_with_others
parts = [
    top_n_with_others().canonical(arrow_to_json),
    detail(exact_table(), EXACT_COLUMNS).canonical(arrow_to_json),
]
print(hashlib.sha256(b"|".join(parts)).hexdigest())
"""


def canonical_digest() -> str:
    parts = [
        top_n_with_others().canonical(arrow_to_json),
        detail(exact_table(), EXACT_COLUMNS).canonical(arrow_to_json),
    ]
    return hashlib.sha256(b"|".join(parts)).hexdigest()


GOLDEN = ROOT / "tests/golden/semantic-result-canonical.sha256"


def test_canonical_bytes_are_equal_across_processes_and_hash_seeds() -> None:
    """The same bytes in other processes and hash seeds, and on both CI Python versions: the
    digest is pinned in the repository."""
    here = canonical_digest()
    for seed in ("0", "1", "12345", "random"):
        environment = {**os.environ, "PYTHONHASHSEED": seed}
        run = subprocess.run(
            [sys.executable, "-c", CANONICAL_SCRIPT.format(root=str(ROOT))],
            capture_output=True,
            text=True,
            env=environment,
            cwd=ROOT,
            check=False,
        )
        assert run.returncode == 0, run.stderr[-2000:]
        assert run.stdout.strip() == here
    assert GOLDEN.read_text("ascii").strip() == here


def test_an_empty_page_with_a_masked_column_and_a_notice_reveals_no_builder_detail() -> None:
    data = table({"region": pa.array([], pa.string()), "phone": pa.array([], pa.string())})
    notice = SemanticError("FILTER_INTERSECTION_EMPTY", {"member": "consumers.region"})
    result = detail(data, [text("region"), masked("phone")], notices=[notice])
    document = wire(result)
    assert document["rows"] == []
    assert document["row_count"] == 0
    assert [n["code"] for n in document["notices"]] == ["FILTER_INTERSECTION_EMPTY"]
    for form in (result.wire(arrow_to_json).encode(), result.canonical(arrow_to_json)):
        assert b"consumers.region" not in form
    assert read_result(json.dumps(document), result.arrow()).cells == ()
