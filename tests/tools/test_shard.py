"""The CI test split (tools.ci.shard): disjoint parts that together are the whole suite."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from tools.ci import shard as shard_module
from tools.ci.shard import assign, key, pytest_collection_modifyitems


def _items(count: int) -> list[SimpleNamespace]:
    return [SimpleNamespace(nodeid=f"tests/test_x.py::test_{n}") for n in range(count)]


def _split(monkeypatch: pytest.MonkeyPatch, shard: str, count: int) -> tuple[list[str], list[str]]:
    monkeypatch.setenv("PYTEST_SHARD", shard)
    deselected: list[SimpleNamespace] = []
    hook = SimpleNamespace(pytest_deselected=lambda items: deselected.extend(items))
    items = _items(count)
    pytest_collection_modifyitems(
        cast(pytest.Config, SimpleNamespace(hook=hook)), cast(list[pytest.Item], items)
    )
    return [i.nodeid for i in items], [i.nodeid for i in deselected]


@pytest.mark.parametrize("record", [{}, {"tests/test_x.py::test_3": 40.0}], ids=["none", "some"])
def test_the_parts_are_disjoint_and_cover_every_test(
    monkeypatch: pytest.MonkeyPatch, record: dict[str, float]
) -> None:
    monkeypatch.setattr(shard_module, "durations", lambda: record)
    every = sorted(i.nodeid for i in _items(100))
    seen: list[str] = []
    for index in range(7):
        kept, left = _split(monkeypatch, f"{index}/7", 100)
        assert sorted(kept + left) == every
        seen += kept
    assert sorted(seen) == every


def test_the_parts_are_balanced_by_measured_time() -> None:
    """Longest first into the lightest part: the slow tests are spread, not stacked."""
    tests = [f"t{n}" for n in range(8)]
    known = {"t0": 10.0, "t1": 10.0, "t2": 10.0, "t3": 1.0, "t4": 1.0, "t5": 1.0}
    placed = assign(tests, 3, known)
    loads = [0.0, 0.0, 0.0]
    for test, part in zip(tests, placed, strict=True):
        loads[part] += known.get(test, 5.5)  # an unknown test counts at the record's median
    assert sorted(placed[:3]) == [0, 1, 2]  # one slow test per part
    assert max(loads) - min(loads) <= 5.5  # within one test of the ideal split
    assert assign(tests, 3, known) == placed  # the same placement in every part


def test_the_committed_record_is_valid() -> None:
    record = shard_module.durations()
    assert all("::" in test and seconds >= 0 for test, seconds in record.items())


def test_without_a_shard_every_test_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTEST_SHARD", raising=False)
    items = _items(5)
    hook = SimpleNamespace(pytest_deselected=lambda items: None)
    pytest_collection_modifyitems(
        cast(pytest.Config, SimpleNamespace(hook=hook)), cast(list[pytest.Item], items)
    )
    assert [i.nodeid for i in items] == [i.nodeid for i in _items(5)]


def test_a_part_outside_the_split_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(pytest.UsageError):
        _split(monkeypatch, "7/7", 10)


def test_parameters_are_recorded_as_a_digest() -> None:
    assert key("a.py::t") == "a.py::t"
    hidden = key("a.py::t[key-abc123]")
    assert hidden.startswith("a.py::t[")
    assert "abc123" not in hidden
    known = {key("a.py::t[x]"): 9.0, "a.py::u": 1.0, "a.py::v": 1.0}
    placed = assign(["a.py::t[x]", "a.py::u", "a.py::v"], 2, known)
    assert placed[1] == placed[2] != placed[0]  # the digest finds the long test
