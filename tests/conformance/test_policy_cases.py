"""Every normative security scenario, run by the security runner (#447): one test per case,
engine and catalog, each the runner's own execution (`security_scenarios.execute`), so pytest
and the runner's report are one verdict (AC5) and nothing here checks an answer its own way.

A test fails on every status the runner fails on (`FAILING`): a wrong answer or control, a leak,
a broken run, a stale `pending` marker or a missing execution. ClickHouse runs on the server
`SEMANTIC_TEST_CLICKHOUSE` names (skipped locally without one, required in CI); the workflow
`security-scenarios` runs both versions through the runner itself.
"""

from __future__ import annotations

import pytest

from conformance.runners import corpus_execution as ce
from conformance.runners import security_fixtures as sf
from conformance.runners import security_scenarios as runner
from conformance.runners.security_cases import cases
from conformance.runners.security_engines import default_clickhouse


def _all() -> list[tuple[str, str, str]]:
    """(case, engine, catalog): every case on every engine it lists, every catalog of its lake."""
    found = []
    for name, case in sorted(cases().items()):
        for engine in case["engines"]:
            if engine == "existing-ducklake":
                settings = sf.fixture(case["fixture"], ce.VENDORS[0]).lake_settings()
                found += [(name, engine, c) for c in settings["catalogs"]]
            else:
                found.append((name, engine, ""))
    return found


ALL = _all()


def _instance(engine: str, catalog: str) -> runner.Instance:
    if engine == "managed":
        return runner.Instance("managed", "managed")
    if engine == "existing-ducklake":
        return runner.Instance(engine, f"existing-ducklake/{catalog}", catalog=catalog)
    server = default_clickhouse()
    return runner.Instance(engine, f"clickhouse/{server.version()}", clickhouse=server)


@pytest.mark.parametrize(
    ("name", "engine", "catalog"), ALL, ids=[f"{n}-{e}{'-' + c if c else ''}" for n, e, c in ALL]
)
def test_a_case_holds_on_its_engine(name: str, engine: str, catalog: str) -> None:
    execution = runner.execute(name, cases()[name], _instance(engine, catalog), set())
    assert execution.status not in runner.FAILING, (execution.status, execution.reason)


def test_every_logical_record_is_run() -> None:
    """The 92 logical records (case, listed engine), none dropped (Owner precision 3)."""
    assert len({(n, e) for n, e, _ in ALL}) == sum(len(c["engines"]) for c in cases().values())
    assert len({(n, e) for n, e, _ in ALL}) == 92


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
