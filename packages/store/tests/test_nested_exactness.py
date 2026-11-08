"""#410: a UHUGEINT nested in LIST, ARRAY, STRUCT, MAP or UNION has no exact Arrow form (DuckDB
wraps it into decimal128(38,0)), so a lake read that returns one is refused whole, before any
batch. Nested HUGEINT, DECIMAL, BIGINT and UBIGINT stay exact; a top-level UHUGEINT stays text."""

from __future__ import annotations

from decimal import Decimal

import pytest
from orvex.contracts.store.v1 import ReadRequest
from orvex.store.errors import StoreError
from orvex.store.lake import Lake

UHUGE_MAX = "340282366920938463463374607431768211455"
HUGE_MAX = "170141183460469231731687303715884105727"
HUGE_MIN = "-170141183460469231731687303715884105728"
UBIG_MAX = "18446744073709551615"
BIG_MIN = "-9223372036854775808"
DECIMAL_38 = "1234567890123456789012345678.1234567890"

NESTED_UHUGEINT = {
    "list": f"[{UHUGE_MAX}::UHUGEINT]",
    "array": f"[{UHUGE_MAX}::UHUGEINT]::UHUGEINT[1]",
    "struct": f"{{'a': {UHUGE_MAX}::UHUGEINT}}",
    "map-value": f"MAP {{'k': {UHUGE_MAX}::UHUGEINT}}",
    "map-key": f"MAP {{{UHUGE_MAX}::UHUGEINT: 1}}",
    "union": f"union_value(u := {UHUGE_MAX}::UHUGEINT)::UNION(u UHUGEINT, i INTEGER)",
    "union-other-member": "union_value(i := 1)::UNION(u UHUGEINT, i INTEGER)",
    "deep": f"[{{'a': MAP {{'k': [[{UHUGE_MAX}::UHUGEINT]::UHUGEINT[1]]}}}}]",
    "null": "NULL::UHUGEINT[]",
}


def request(lake: Lake, sql: str) -> ReadRequest:
    return ReadRequest(
        kind="read", sql=sql, parameters=[], snapshot=lake.current_snapshot().id, batch_size=16
    )


@pytest.mark.parametrize("expression", NESTED_UHUGEINT.values(), ids=NESTED_UHUGEINT.keys())
def test_a_nested_uhugeint_refuses_the_whole_read_before_any_batch(
    lake: Lake, expression: str
) -> None:
    # The first column is valid: the refusal still comes from `read` itself, so no stream (and
    # no batch of it) is ever returned.
    with pytest.raises(StoreError) as refused:
        lake.read(request(lake, f"SELECT 1 AS ok, {expression} AS v"))
    assert refused.value.code == "STORE_TYPE_UNSUPPORTED"
    assert refused.value.details["column"] == "v"


EXACT_NESTED = {
    "hugeint-list": (
        f"[{HUGE_MAX}::HUGEINT, '{HUGE_MIN}'::HUGEINT]",
        [Decimal(HUGE_MAX), Decimal(HUGE_MIN)],
    ),
    "hugeint-array": (f"[{HUGE_MAX}::HUGEINT]::HUGEINT[1]", [Decimal(HUGE_MAX)]),
    "hugeint-struct": (f"{{'a': '{HUGE_MIN}'::HUGEINT}}", {"a": Decimal(HUGE_MIN)}),
    "hugeint-map": (f"MAP {{'k': {HUGE_MAX}::HUGEINT}}", [("k", Decimal(HUGE_MAX))]),
    "hugeint-union": (
        f"union_value(h := {HUGE_MAX}::HUGEINT)::UNION(h HUGEINT, i INTEGER)",
        Decimal(HUGE_MAX),
    ),
    "decimal-list": (f"[{DECIMAL_38}::DECIMAL(38,10)]", [Decimal(DECIMAL_38)]),
    "decimal-struct": (f"{{'d': {DECIMAL_38}::DECIMAL(38,10)}}", {"d": Decimal(DECIMAL_38)}),
    "bigint-list": (f"['{BIG_MIN}'::BIGINT]", [int(BIG_MIN)]),
    "bigint-map": (f"MAP {{'{BIG_MIN}'::BIGINT: 1}}", [(int(BIG_MIN), 1)]),
    "ubigint-list": (f"[{UBIG_MAX}::UBIGINT]", [int(UBIG_MAX)]),
    "ubigint-struct": (f"{{'u': {UBIG_MAX}::UBIGINT}}", {"u": int(UBIG_MAX)}),
    "ubigint-union": (
        f"union_value(u := {UBIG_MAX}::UBIGINT)::UNION(u UBIGINT, i INTEGER)",
        int(UBIG_MAX),
    ),
}


@pytest.mark.parametrize(("expression", "expected"), EXACT_NESTED.values(), ids=EXACT_NESTED.keys())
def test_nested_exact_types_stay_exact(lake: Lake, expression: str, expected: object) -> None:
    table = lake.read(request(lake, f"SELECT {expression} AS v")).read_all()
    assert table.column("v").to_pylist() == [expected]


def test_a_top_level_uhugeint_is_still_read_as_text(lake: Lake) -> None:
    table = lake.read(request(lake, f"SELECT {UHUGE_MAX}::UHUGEINT AS v")).read_all()
    assert table.column("v").to_pylist() == [UHUGE_MAX]
    assert table.schema.field("v").metadata == {b"duckdb_type": b"UHUGEINT"}
