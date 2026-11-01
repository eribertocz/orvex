"""Run one part of the test suite, for CI jobs that split it (`-p tools.ci.shard`).

`PYTEST_SHARD=i/n` keeps every n-th collected test starting at the i-th (0-based), so the n
parts are disjoint and together are the whole suite. Collection order interleaves the
parametrized catalogs of one test, which spreads the slow lake tests across the parts. Without
`PYTEST_SHARD` every test runs.
"""

from __future__ import annotations

import os

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    shard = os.environ.get("PYTEST_SHARD", "")
    if not shard:
        return
    index, total = (int(part) for part in shard.split("/"))
    if not 0 <= index < total:
        raise pytest.UsageError(f"PYTEST_SHARD={shard}: the part must be in 0..{total - 1}")
    kept = [item for position, item in enumerate(items) if position % total == index]
    left = [item for position, item in enumerate(items) if position % total != index]
    config.hook.pytest_deselected(items=left)
    items[:] = kept
