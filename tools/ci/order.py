"""Run the tests in a random order with a recorded seed (#376, #373 §5): each test must pass
alone and after any other, so an order dependency fails in CI instead of hiding.

    PYTEST_RANDOM_ORDER=<seed> python -m pytest -p tools.ci.order

The modules are shuffled, then the tests within each module, so module fixtures are still set up
once per module. The seed is printed in the report header; the same seed (and the same part)
replays the same order. Without the variable the plugin changes nothing.
"""

from __future__ import annotations

import os
import random

import pytest

VARIABLE = "PYTEST_RANDOM_ORDER"


def seed() -> int | None:
    value = os.environ.get(VARIABLE, "").strip()
    return int(value) if value else None


def shuffled(items: list[pytest.Item], chosen: int) -> list[pytest.Item]:
    generator = random.Random(chosen)
    modules: dict[str, list[pytest.Item]] = {}
    for item in items:
        modules.setdefault(item.nodeid.split("::", 1)[0], []).append(item)
    order = sorted(modules)
    generator.shuffle(order)
    result: list[pytest.Item] = []
    for module in order:
        tests = modules[module]
        generator.shuffle(tests)
        result += tests
    return result


def pytest_report_header(config: pytest.Config) -> str | None:
    chosen = seed()
    return None if chosen is None else f"random order: {VARIABLE}={chosen}"


@pytest.hookimpl(trylast=True)  # after the shard plugin chose this part's tests
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    chosen = seed()
    if chosen is not None:
        items[:] = shuffled(items, chosen)
