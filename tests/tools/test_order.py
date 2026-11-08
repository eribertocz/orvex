"""The random order (#376): reproducible from its seed, module by module, and off by default."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from tools.ci import order


def _items(*nodeids: str) -> list[pytest.Item]:
    return [cast(pytest.Item, SimpleNamespace(nodeid=n)) for n in nodeids]


ITEMS = [f"m{m}.py::t{t}" for m in range(5) for t in range(6)]


def test_the_same_seed_gives_the_same_order_and_another_seed_another() -> None:
    first = [i.nodeid for i in order.shuffled(_items(*ITEMS), 7)]
    again = [i.nodeid for i in order.shuffled(_items(*ITEMS), 7)]
    other = [i.nodeid for i in order.shuffled(_items(*ITEMS), 8)]
    assert first == again
    assert first != other
    assert sorted(first) == sorted(ITEMS)
    assert first != ITEMS


def test_each_module_stays_together_so_its_fixtures_run_once() -> None:
    result = [i.nodeid.split("::")[0] for i in order.shuffled(_items(*ITEMS), 3)]
    runs = [m for index, m in enumerate(result) if index == 0 or result[index - 1] != m]
    assert len(runs) == len(set(runs)) == 5


def test_without_the_variable_nothing_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(order.VARIABLE, raising=False)
    items = _items(*ITEMS)
    order.pytest_collection_modifyitems(cast(pytest.Config, None), items)
    assert [i.nodeid for i in items] == ITEMS
    assert order.pytest_report_header(cast(pytest.Config, None)) is None


def test_the_seed_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(order.VARIABLE, "42")
    items = _items(*ITEMS)
    order.pytest_collection_modifyitems(cast(pytest.Config, None), items)
    assert [i.nodeid for i in items] == [i.nodeid for i in order.shuffled(_items(*ITEMS), 42)]
    assert order.pytest_report_header(cast(pytest.Config, None)) == (
        "random order: PYTEST_RANDOM_ORDER=42"
    )
