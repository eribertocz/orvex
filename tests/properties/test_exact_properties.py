"""Exact arithmetic properties (#426): every generated question over exact columns is either
answered with the oracle's exact value, never through a float, or refused with a stable code
before any read; the same question gives the same result and reproducible receipt every time.

Deterministic and small in CI (`world.PROFILE`), wide in the nightly run."""

from __future__ import annotations

import random
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml
from hypothesis import example, given
from hypothesis import strategies as st
from orvex.semantic import SemanticError
from orvex.semantic.execution import answer
from orvex.semantic.policies import read_policies
from orvex.store.errors import StoreError

from conformance.runners import corpus_execution as ce
from tests.properties import exact

# The explicit examples pin each shape the class must cover, whatever the generator draws.
THIRD = ("bin", "/", ("col", "i"), ("col", "j"))
ROWS = [{"x": "1.000000", "y": "3.000000", "i": "1", "j": "3"}]
ZERO = [{"x": "1.000000", "y": None, "i": "5", "j": "0"}]

# Refusals a generated question may get before any read, each a stable catalog code.
BEFORE_READ = {"MISSING_EXTENSION_DECLARATION", "UNSUPPORTED_OPERATION", "OSSIE_INVALID"}
# Fields of a receipt that legitimately differ between two runs of one question.
VARYING = {"execution", "execution_fingerprint"}


class Reads:
    """Counts the store calls of one question, so a refusal can be proved to read nothing."""

    def __init__(self, read: Any) -> None:  # noqa: ANN401
        self.read, self.calls = read, 0

    def __call__(self, *arguments: Any, **options: Any) -> Any:  # noqa: ANN401
        self.calls += 1
        return self.read(*arguments, **options)


def ask(node: exact.Node, generated: list[dict[str, str | None]]) -> tuple[Any, int]:
    """(The answer or the `SemanticError`, the number of store calls)."""
    try:
        compiled = exact.model(node)
    except SemanticError as error:
        return error, 0
    policy = read_policies(
        yaml.safe_dump({"version": 1}), compiled, groups=frozenset(), managed_tables={}
    )
    with tempfile.TemporaryDirectory() as folder, exact.lake(Path(folder), generated) as lake:
        reads = Reads(lake.read)
        try:
            found = answer(
                exact.QUESTION,
                compiled,
                policy,
                ce.viewer_context(),
                ce.engine_for(lake, read=reads),
            )
        except (SemanticError, StoreError) as error:
            return error, reads.calls
    return found, reads.calls


def stable(found: Any) -> tuple[Any, Any]:  # noqa: ANN401
    """The semantic result and the reproducible receipt fields, compared exactly."""
    envelope = found.result.envelope()
    result = {k: v for k, v in envelope.items() if k != "receipt"}
    receipt = {k: v for k, v in found.receipt.document().items() if k not in VARYING}
    return result, receipt


@exact.EXACT
@given(node=exact.trees(), generated=exact.rows())
@example(node=THIRD, generated=ROWS)  # an exact quotient: refused, never a float
@example(
    node=("case", ">", THIRD, ("lit", "0.333333333333333333"), ("lit", "1"), ("lit", "0")),
    generated=ROWS,
)  # a quotient compared: the reproduction of #426
@example(node=("bin", "%", ("col", "i"), ("col", "j")), generated=ZERO)  # a zero divisor
@example(
    node=(
        "bin",
        "*",
        ("bin", "*", ("col", "x"), ("col", "x")),
        ("bin", "*", ("col", "x"), ("col", "x")),
    ),
    generated=[{"x": "999999999999.999999", "y": None, "i": None, "j": None}],
)  # past 38 digits
@example(node=("nullif", ("col", "x"), ("col", "y")), generated=ROWS)
@example(node=("coalesce", ("neg", ("abs", ("col", "y"))), "0.5"), generated=ZERO)
def test_an_exact_question_equals_the_oracle_or_is_refused_before_any_read(
    node: exact.Node, generated: list[dict[str, str | None]]
) -> None:
    found, calls = ask(node, generated)
    if isinstance(found, SemanticError) and found.code in BEFORE_READ:
        assert calls == 0, f"{found.code} after a read"
        return
    rows = exact.typed(generated)
    readings = exact.readings(lambda: exact.total(node, rows))
    if isinstance(found, (SemanticError, StoreError)):
        # Only a value the data makes undefined may stop a question after its read: an engine
        # error, stable, never an internal one and never an answer.
        assert isinstance(found, StoreError), found.code
        assert any(isinstance(r, exact.Undefined) for r in readings), readings
        return
    assert not exact.divides(node), "an exact `/` was answered"
    column = found.result.envelope()["columns"][0]
    assert column["exactness"] == "exact"
    assert column["encoding"] not in ("FLOAT", "DOUBLE")
    assert " / " not in found.physical.sql  # no float operation on exact inputs
    value = ce.rows_of(found)[0][0]
    answered = None if value is None else Decimal(value)
    assert answered in [r for r in readings if not isinstance(r, ArithmeticError)], readings


def twice(node: exact.Node, generated: list[dict[str, str | None]]) -> tuple[Any, Any]:
    """The same question asked twice on one lake: (first, second) answers or errors."""
    try:
        compiled = exact.model(node)
    except SemanticError as error:
        return error, error
    policy = read_policies(
        yaml.safe_dump({"version": 1}), compiled, groups=frozenset(), managed_tables={}
    )
    found: list[Any] = []
    with tempfile.TemporaryDirectory() as folder, exact.lake(Path(folder), generated) as lake:
        for _ in range(2):
            try:
                found.append(
                    answer(
                        exact.QUESTION, compiled, policy, ce.viewer_context(), ce.engine_for(lake)
                    )
                )
            except (SemanticError, StoreError) as error:
                found.append(error)
    return found[0], found[1]


@exact.EXACT
@given(node=exact.trees(), generated=exact.rows(), seed=st.integers(0, 2**32 - 1))
def test_the_same_exact_question_gives_the_same_result_and_reproducible_receipt(
    node: exact.Node, generated: list[dict[str, str | None]], seed: int
) -> None:
    first, second = twice(node, generated)
    if not hasattr(first, "result") or not hasattr(second, "result"):
        assert type(first) is type(second)
        assert getattr(first, "code", None) == getattr(second, "code", None)
        return
    assert stable(first) == stable(second)  # one lake: every reproducible field, snapshot too
    shuffled = list(generated)
    random.Random(seed).shuffle(shuffled)
    third, _ = ask(node, shuffled)
    assert hasattr(third, "result")
    first_result, first_receipt = stable(first)
    third_result, third_receipt = stable(third)
    assert first_result == third_result
    # Another lake holds the shuffled rows: its snapshot is its own (`data`); everything else
    # the receipt can reproduce is equal.
    first_receipt.pop("data"), third_receipt.pop("data")
    assert first_receipt == third_receipt


def test_the_oracle_keeps_every_digit_a_lower_precision_would_lose() -> None:
    wide = ("bin", "*", ("col", "x"), ("col", "y"))
    big = "999999999999.999999"
    rows = exact.typed([{"x": big, "y": big, "i": None, "j": None}])
    assert exact.total(wide, rows) == Decimal("999999999999999998000000.000000000001")
    narrow = exact.ORACLE.copy()
    narrow.prec = 28  # the default context: it would round the product
    assert narrow.multiply(Decimal(big), Decimal(big)) != exact.total(wide, rows)


def test_the_comparison_keeps_reproducible_receipt_fields_and_ignores_timing() -> None:
    found, _ = ask(("col", "x"), [{"x": "1.000000", "y": None, "i": None, "j": None}])
    _, receipt = stable(found)
    document = found.receipt.document()
    assert set(receipt) == set(document) - VARYING
    assert {"query", "model", "plan", "security", "reproducibility", "data"} <= set(receipt)
    changed = dict(document, plan={**document["plan"], "fingerprint": "sha256:" + "f" * 64})
    assert {k: v for k, v in changed.items() if k not in VARYING} != receipt
    timed = dict(document, execution={**document["execution"], "duration_ms": 999999})
    assert {k: v for k, v in timed.items() if k not in VARYING} == receipt


FAR = ("bin", "*", ("col", "x"), ("col", "x"))  # up to 24 whole digits per row
SCALES = ("case", "<", ("col", "x"), ("col", "x"), ("lit", "0.333333333333333333"), FAR)


@exact.EXACT
@given(
    numerator=exact.trees().filter(lambda n: not exact.divides(n)),
    denominator=exact.trees().filter(lambda n: not exact.divides(n)),
    mode=st.sampled_from(sorted(exact.MODES)),
    places=st.integers(0, 4),
    zero=st.sampled_from(["null", "reject"]),
    generated=exact.rows(),
)
@example(
    numerator=("col", "i"),
    denominator=("col", "j"),
    mode="half_away_from_zero",
    places=0,
    zero="reject",
    generated=[{"x": None, "y": None, "i": "-5", "j": "0"}],
)
@example(  # a denominator of scale 24: the components unscaled without arithmetic (#450 review)
    numerator=("col", "x"),
    denominator=("bin", "*", ("lit", "1.000001"), ("lit", "0.333333333333333333")),
    mode="half_away_from_zero",
    places=0,
    zero="null",
    generated=[{"x": "1.000000", "y": None, "i": None, "j": None}],
)
@example(  # a component computed at another scale than declared: read at the declared one
    numerator=("col", "x"),
    denominator=SCALES,
    mode="half_away_from_zero",
    places=0,
    zero="null",
    generated=[{"x": "-1.000000", "y": None, "i": None, "j": None}],
)
@example(  # the same component past its declared type: an engine error, never an answer
    numerator=("col", "x"),
    denominator=SCALES,
    mode="half_away_from_zero",
    places=0,
    zero="null",
    generated=[
        {"x": None, "y": None, "i": None, "j": None},
        {"x": "999999999999.999999", "y": None, "i": None, "j": None},
    ],
)
@example(
    numerator=FAR,
    denominator=("lit", "0.333333"),
    mode="half_even",
    places=4,
    zero="null",
    generated=[{"x": "999999999999.999999", "y": None, "i": None, "j": None}],
)
def test_the_declared_division_equals_the_oracle_at_its_scale_and_mode(
    numerator: exact.Node,
    denominator: exact.Node,
    mode: str,
    places: int,
    zero: str,
    generated: list[dict[str, str | None]],
) -> None:
    rows = exact.typed(generated)
    try:
        compiled = exact.ratio_model(numerator, denominator, mode, places, zero)
    except SemanticError:
        return  # refused when the model compiles, before any engine is chosen
    policy = read_policies(
        yaml.safe_dump({"version": 1}), compiled, groups=frozenset(), managed_tables={}
    )
    question = exact.QUESTION.replace('"total"', '"ratio"')
    with tempfile.TemporaryDirectory() as folder, exact.lake(Path(folder), generated) as lake:
        reads = Reads(lake.read)
        try:
            found: Any = answer(
                question, compiled, policy, ce.viewer_context(), ce.engine_for(lake, read=reads)
            )
        except (SemanticError, StoreError) as error:
            found = error
    if isinstance(found, SemanticError) and found.code in BEFORE_READ:
        assert reads.calls == 0
        return

    def computed() -> tuple[Any, Any, Any]:
        pair = (exact.total(numerator, rows), exact.total(denominator, rows))
        return pair, exact.quotient(*pair, mode, places, zero), None

    readings = exact.readings(computed)
    if isinstance(found, SemanticError) and found.code == "DIVISION_POLICY_VIOLATION":
        assert any(isinstance(r, exact.Rejected) for r in readings), readings
        return
    if isinstance(found, StoreError):  # an engine error, never an answer
        assert any(isinstance(r, exact.Undefined) for r in readings), readings
        return
    assert not isinstance(found, SemanticError), found
    value = ce.rows_of(found)[0][0]
    answered = None if value is None else Decimal(value)
    defined = [r for r in readings if not isinstance(r, ArithmeticError)]
    assert answered in [q for _, q, _ in defined], readings
    for components, expected, _ in defined:
        top: exact.Value = components[0]
        bottom: exact.Value = components[1]
        if expected == answered and exact.in_domain(top, bottom, mode, places):
            assert top is not None
            assert bottom is not None
            second = exact.decimal_division(top, bottom, places)
            assert second is None or second == expected


def test_the_engine_s_own_division_is_only_an_oracle_inside_its_domain() -> None:
    connection = duckdb.connect()

    def division(sql: str) -> object:
        return connection.execute(f"SELECT decimal_division({sql})").fetchall()[0][0]

    assert division("NULL::DECIMAL(9,0), 3::DECIMAL(9,0)") is None
    with pytest.raises(duckdb.InvalidInputException, match="division by zero"):
        division("1::DECIMAL(9,0), 0::DECIMAL(9,0)")
    huge = "99999999999999999999999999999999999999::DECIMAL(38,0), 0.1::DECIMAL(38,1)"
    with pytest.raises(duckdb.OutOfRangeException):
        division(huge)
    # The oracle alone decides past 38 digits: outside the domain, never compared.
    big, small = Decimal("9" * 38), Decimal("0.1")
    assert not exact.in_domain(big, small, "half_even", 0)
    with pytest.raises(exact.Undefined):
        exact.quotient(big, small, "half_even", 0, "null")
