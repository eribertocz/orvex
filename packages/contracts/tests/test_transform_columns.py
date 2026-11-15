"""The consumer's columns exchange of the transform contract: each relation asked for is
described once, and an implementation that asks again fails closed."""

from __future__ import annotations

import pytest
from orvex.contracts.transform.analysis import ColumnsNeeded
from orvex.contracts.transform.columns import ColumnsExchange
from orvex.contracts.transform.interface import Known, TransformError


def test_columns_are_described_once_each_until_the_answer() -> None:
    described: list[tuple[str, ...]] = []

    def ask(known: Known) -> str | ColumnsNeeded:
        given = {entry.relation: entry.columns for entry in known}
        if ("a",) not in given:
            return ColumnsNeeded((("a",),))
        if ("b",) not in given:
            return ColumnsNeeded((("b",),))
        return f"{given[('a',)]} {given[('b',)]}"

    def describe(relation: tuple[str, ...]) -> list[str] | None:
        described.append(relation)
        return ["x"] if relation == ("a",) else None

    assert ColumnsExchange[str](describe).answer(ask) == "('x',) None"
    assert described == [("a",), ("b",)]


@pytest.mark.parametrize("asked", [(("a",),), ()])
def test_asking_again_or_for_nothing_fails_closed(asked: tuple[tuple[str, ...], ...]) -> None:
    calls: list[Known] = []

    def ask(known: Known) -> ColumnsNeeded:
        calls.append(known)
        return ColumnsNeeded(asked if known else (("a",),))

    with pytest.raises(TransformError) as raised:
        ColumnsExchange[str](lambda relation: ["x"]).answer(ask)
    assert raised.value.code == "TRANSFORM_COLUMNS_REPEATED"
    assert len(calls) == 2
