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
    if isinstance(found, (SemanticError, StoreError)):
        # Only a value the data makes undefined may stop a question after its read: an engine
        # error, stable, never an internal one and never an answer.
        assert isinstance(found, StoreError), found.code
        with pytest.raises(exact.Undefined):
            exact.total(node, exact.typed(generated))
        return
    assert not exact.divides(node), "an exact `/` was answered"
    column = found.result.envelope()["columns"][0]
    assert column["exactness"] == "exact"
    assert column["encoding"] not in ("FLOAT", "DOUBLE")
    assert " / " not in found.physical.sql  # no float operation on exact inputs
    value = ce.rows_of(found)[0][0]
    expected = exact.total(node, exact.typed(generated))
    assert (None if value is None else Decimal(value)) == expected


@exact.EXACT
@given(node=exact.trees(), generated=exact.rows(), seed=st.integers(0, 2**32 - 1))
def test_the_same_exact_question_gives_the_same_result_and_reproducible_receipt(
    node: exact.Node, generated: list[dict[str, str | None]], seed: int
) -> None:
    first, _ = ask(node, generated)
    shuffled = list(generated)
    random.Random(seed).shuffle(shuffled)
    second, _ = ask(node, shuffled)
    if not hasattr(first, "result") or not hasattr(second, "result"):
        assert type(first) is type(second)
        assert getattr(first, "code", None) == getattr(second, "code", None)
        return
    first_result, first_receipt = stable(first)
    second_result, second_receipt = stable(second)
    assert first_result == second_result
    # The data's fingerprint differs when the rows are stored in another order on purpose:
    # everything else the receipt can reproduce is equal.
    first_receipt.pop("data"), second_receipt.pop("data")
    assert first_receipt == second_receipt


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
