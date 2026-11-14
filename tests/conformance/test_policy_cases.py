"""Every normative security scenario, run by the security runner (#447): one test per case,
engine and catalog, each the runner's own execution (`security_scenarios.execute`), so pytest
and the runner's report are one verdict (AC5) and nothing here checks an answer its own way.

A test fails on every status the runner fails on (`FAILING`): a wrong answer or control, a leak,
a broken run, a stale `pending` marker or a missing execution. ClickHouse runs on the server
`SEMANTIC_TEST_CLICKHOUSE` names (skipped locally without one, required in CI); the workflow
`security-scenarios` runs both versions through the runner itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from conformance.runners import security_scenarios as runner
from conformance.runners.security_cases import cases
from conformance.runners.security_engines import default_clickhouse


def _all() -> list[tuple[str, str, runner.Instance | None]]:
    """(case, engine, instance): every case on every engine it lists, every catalog of its lake,
    every variant; the ClickHouse instance is made by the test (None), on the server it finds."""
    found: list[tuple[str, str, runner.Instance | None]] = []
    for name, case in sorted(cases().items()):
        for engine in case["engines"]:
            if engine == "clickhouse":
                found += [(name, engine, None)]
            else:
                found += [(name, engine, one) for one in runner.instances(engine, case, [])]
    return found


ALL = _all()


def _clickhouse(case: Mapping[str, Any]) -> list[runner.Instance]:
    server = default_clickhouse()
    return runner.instances("clickhouse", case, [(server.version(), server)])


@pytest.mark.parametrize(
    ("name", "engine", "instance"),
    ALL,
    ids=[f"{n}-{i.name if i else e}" for n, e, i in ALL],
)
def test_a_case_holds_on_its_engine(
    name: str, engine: str, instance: runner.Instance | None
) -> None:
    case = cases()[name]
    for one in [instance] if instance is not None else _clickhouse(case):
        execution = runner.execute(name, case, one, set())
        assert execution.status not in runner.FAILING, (
            one.name,
            execution.status,
            execution.reason,
        )


def test_every_logical_record_is_run() -> None:
    """The 92 logical records (case, listed engine), none dropped (Owner precision 3)."""
    records = {(n, e) for n, e, _ in ALL}
    assert len(records) == sum(len(c["engines"]) for c in cases().values()) == 92


def test_what_waits_for_detail_pages_on_an_existing_lake_is_p5_14() -> None:
    """Detail pages on an existing DuckLake are P5-14: the cases that need them wait for it,
    on that engine alone (`pending_engines`) or, listing no other, as a whole (`pending`)."""
    waiting = {n: c["pending_engines"]["existing-ducklake"]
               for n, c in cases().items() if "pending_engines" in c}  # fmt: skip
    assert waiting == dict.fromkeys(
        [*(f"SEC-DQ-00{i}" for i in range(1, 6)), "SEC-DQ-017", "SEC-DQ-020", "SEC-CACHE-004"],
        "P5-14",
    )
    whole = {n for n, c in cases().items() if c.get("pending") == "P5-14"}
    assert whole == {f"SEC-DQ-0{i:02d}" for i in (6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 18, 19)}
    for name in whole:
        assert cases()[name]["engines"] == ["existing-ducklake"], name
