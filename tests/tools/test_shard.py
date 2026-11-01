"""The CI test split (tools.ci.shard): disjoint parts that together are the whole suite."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from tools.ci.shard import pytest_collection_modifyitems


def _split(monkeypatch: pytest.MonkeyPatch, shard: str, count: int) -> tuple[list[int], list[int]]:
    monkeypatch.setenv("PYTEST_SHARD", shard)
    deselected: list[int] = []
    hook = SimpleNamespace(pytest_deselected=lambda items: deselected.extend(items))
    items = list(range(count))
    pytest_collection_modifyitems(
        cast(pytest.Config, SimpleNamespace(hook=hook)), cast(list[pytest.Item], items)
    )
    return items, deselected


def test_the_parts_are_disjoint_and_cover_every_test(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []
    for index in range(7):
        kept, left = _split(monkeypatch, f"{index}/7", 100)
        assert sorted(kept + left) == list(range(100))
        seen += kept
    assert sorted(seen) == list(range(100))


def test_without_a_shard_every_test_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTEST_SHARD", raising=False)
    items = list(range(5))
    hook = SimpleNamespace(pytest_deselected=lambda items: None)
    pytest_collection_modifyitems(
        cast(pytest.Config, SimpleNamespace(hook=hook)), cast(list[pytest.Item], items)
    )
    assert items == list(range(5))


def test_a_part_outside_the_split_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(pytest.UsageError):
        _split(monkeypatch, "7/7", 10)
