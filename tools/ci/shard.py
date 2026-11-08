"""Run one part of the test suite, for CI jobs and local runs that split it (`-p tools.ci.shard`).

`PYTEST_SHARD=i/n` keeps the i-th (0-based) of n disjoint parts, which together are the whole
suite. Without `PYTEST_SHARD` every test runs.

The parts are balanced by measured time (#375): `tools/ci/test-durations.json`, written by
`python -m tools.ci.durations report … --record`, gives each known test its median seconds. The
tests are placed longest first, each in the part with the least time so far (ties to the lowest
part), with a test the record does not know counted at the record's median. The placement
depends only on the collected tests and the record, so every part computes the same one. With
no record, every n-th collected test is kept, starting at the i-th.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import os
import statistics
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

RECORD = Path(__file__).with_name("test-durations.json")


def key(nodeid: str) -> str:
    """A test's name in the record: its parameters become a digest, since they may hold
    synthetic credentials that secret scanning (rightly) refuses to see committed."""
    name, bracket, parameters = nodeid.partition("[")
    if not bracket:
        return nodeid
    return f"{name}[{hashlib.sha256(parameters.encode('utf-8')).hexdigest()[:12]}]"


def durations(path: Path = RECORD) -> dict[str, float]:
    if not path.is_file():
        return {}
    found = json.loads(path.read_text("utf-8"))
    return {str(test): float(seconds) for test, seconds in found.items()}


def assign(tests: Sequence[str], total: int, known: Mapping[str, float]) -> list[int]:
    """The part of each test, in the order given: balanced by `known` seconds, or round robin
    when nothing is known."""
    if not known:
        return [position % total for position in range(len(tests))]
    default = statistics.median(known.values())
    cost = [known.get(key(test), default) for test in tests]
    order = sorted(range(len(tests)), key=lambda i: (-cost[i], tests[i]))
    parts = [(0.0, part) for part in range(total)]  # (time so far, part): a min-heap
    heapq.heapify(parts)
    placed = [0] * len(tests)
    for index in order:
        load, part = heapq.heappop(parts)
        placed[index] = part
        heapq.heappush(parts, (load + cost[index], part))
    return placed


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    shard = os.environ.get("PYTEST_SHARD", "")
    if not shard:
        return
    index, total = (int(part) for part in shard.split("/"))
    if not 0 <= index < total:
        raise pytest.UsageError(f"PYTEST_SHARD={shard}: the part must be in 0..{total - 1}")
    placed = assign([item.nodeid for item in items], total, durations())
    kept = [item for item, part in zip(items, placed, strict=True) if part == index]
    left = [item for item, part in zip(items, placed, strict=True) if part != index]
    config.hook.pytest_deselected(items=left)
    items[:] = kept
