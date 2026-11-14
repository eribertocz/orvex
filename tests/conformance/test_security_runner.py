"""The security runner's own rules (#447): every status earned (AC2), excuses checked (AC3),
the HTTP revocations deterministic and their callers provisioned (AC4), records kept apart from
executions and a missing ClickHouse version reported (AC1, AC6), and a report that holds no
data value. Cases are planted by changing a real case in memory; nothing here is normative.
"""

from __future__ import annotations

import copy
import json
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from orvex.access.server_context import ServerContextProvider, ServerRequest

from conformance.runners import corpus_execution as ce
from conformance.runners import security_fixtures as sf
from conformance.runners import security_http
from conformance.runners import security_scenarios as runner
from conformance.runners.security_cases import cases
from conformance.runners.security_engines import ClickHouseServer

MANAGED = runner.Instance("managed", "managed")
POLICY = "sha256:" + "0" * 64  # any policy model: the context is what is compared
SQLITE_LAKE = runner.Instance("existing-ducklake", "existing-ducklake/sqlite", catalog="sqlite")


def planted(name: str, **changes: Any) -> dict[str, Any]:  # noqa: ANN401
    """A copy of case `name` with `changes` (a key set to None is removed)."""
    case = copy.deepcopy(dict(cases()[name]))
    for key, value in changes.items():
        if value is None:
            case.pop(key, None)
        else:
            case[key] = value
    return case


def status(case: Mapping[str, Any], instance: runner.Instance = MANAGED, **kw: Any) -> str:  # noqa: ANN401
    return runner.execute(str(case["id"]), case, instance, kw.get("present", set())).status


def with_rows(expectation: Mapping[str, Any], rows: str) -> dict[str, Any]:
    return {**expectation, "rows": rows}


# --- AC2: every status earned ----------------------------------------------------------------


def test_a_passing_case_passes() -> None:
    assert status(planted("SEC-ROWS-001")) == "pass"


def test_a_wrong_answer_fails() -> None:
    case = planted("SEC-ROWS-001")
    case["expect"] = with_rows(case["expect"], "readings.zone_id,total_kwh\nZ1,30.70\n")
    assert status(case) == "fail"


def test_a_wrong_control_fails() -> None:
    case = planted("SEC-ROWS-001")
    case["control"] = with_rows(case["control"], "readings.zone_id,total_kwh\nZ1,30.75\n")
    assert status(case) == "fail"


def test_a_value_leaked_into_the_rows_or_an_error_detail_fails() -> None:
    rows = planted("SEC-ROWS-001")
    rows["expect"] = {**rows["expect"], "forbidden": ["30.75"]}  # in the caller's own rows
    detail = planted("SEC-COLUMNS-005")
    detail["expect"] = {**detail["expect"], "forbidden": ["consumers.phone"]}  # in the error
    for case in (rows, detail):
        execution = runner.execute(case["id"], case, MANAGED, set())
        assert (execution.status, execution.reason) == ("fail", "leaked 1 forbidden values")


def test_a_pending_case_refused_for_its_limitation_is_an_expected_failure() -> None:
    assert status(planted("SEC-ROWS-009")) == "expected-fail"


def test_a_pending_case_that_leaks_fails() -> None:
    case = planted("SEC-ROWS-009")
    case["expect"] = {**case["expect"], "forbidden": ["UNSUPPORTED_OPERATION"]}
    assert status(case) == "fail"


def test_a_pending_case_with_a_wrong_control_or_another_code_fails() -> None:
    control = planted("SEC-ROWS-001", pending="P4-02")
    control["expect"] = with_rows(control["expect"], "readings.zone_id,total_kwh\nZ1,1.00\n")
    another = planted("SEC-COLUMNS-005", pending="P4-02")
    another["expect"] = {**another["expect"], "code": "POLICY_INVALID"}
    for case in (control, another):
        assert status(case) == "fail", case["id"]


def test_a_pending_case_whose_run_breaks_fails() -> None:
    assert status(planted("SEC-ROWS-009", fixture="no-such-fixture")) == "fail"


def test_a_pending_case_that_passes_is_stale() -> None:
    assert status(planted("SEC-ROWS-001", pending="P4-02")) == "STALE_PENDING"


def test_a_case_whose_surface_does_not_exist_is_not_runnable_until_it_does() -> None:
    case = planted("SEC-PAGES-001")
    assert status(case) == "not-runnable"
    assert status(case, present={"/api/v1/pages"}) == "STALE_NOT_RUNNABLE"


def test_a_case_pending_on_one_engine_is_refused_there_before_any_read() -> None:
    execution = runner.execute("SEC-DQ-001", cases()["SEC-DQ-001"], SQLITE_LAKE, set())
    assert execution.status == "engine-pending"
    assert execution.protection == "proven"  # its aggregate counterpart passes


# --- the protection a refused run shows (Owner condition) ------------------------------------


def lake(name: str, instance: runner.Instance = SQLITE_LAKE, **case: Any) -> runner.Execution:  # noqa: ANN401
    planted_case = planted(name, **case)
    return runner.execute(name, planted_case, instance, set())


def test_a_counterpart_that_leaks_fails_though_the_detail_run_is_refused() -> None:
    case = planted("SEC-DQ-008")
    case["expect"] = {**case["expect"], "forbidden": [*case["expect"]["forbidden"], "15.00"]}
    execution = runner.execute("SEC-DQ-008", case, SQLITE_LAKE, set())
    assert (execution.status, execution.reason) == (
        "fail",
        "aggregate counterpart: leaked 1 forbidden values",
    )


def test_a_counterpart_with_a_wrong_answer_shows_nothing_and_says_why() -> None:
    case = planted("SEC-DQ-008")
    header = "outages.outage_id,outages.zone_id,outages.minutes"
    case["expect"] = with_rows(case["expect"], f"{header}\n1,Z1,15.00\n")
    execution = runner.execute("SEC-DQ-008", case, SQLITE_LAKE, set())
    assert execution.status == "expected-fail"
    assert execution.protection == "not-demonstrated"
    assert execution.shown.startswith("aggregate: the rows differ")


def test_a_counterpart_that_passes_proves_the_protection() -> None:
    execution = lake("SEC-DQ-008")
    assert (execution.status, execution.protection) == ("expected-fail", "proven")


def test_an_attack_no_query_reaches_is_not_shown_by_a_pass() -> None:
    for name in ("SEC-DQ-006", "SEC-DQ-007"):
        execution = lake(name)
        assert execution.status == "expected-fail", name
        assert execution.protection == "not-demonstrated", name
        assert execution.shown.startswith("unreached: "), name


def test_each_variant_is_an_execution_and_one_not_made_is_never_a_pass() -> None:
    names = [i.name for i in runner.instances("existing-ducklake", cases()["SEC-DQ-012"], [])]
    assert names == [
        f"existing-ducklake/{c}[by={b}]"
        for c in ("sqlite", "postgres")
        for b in ("absolute_path", "hard_link")
    ]
    (hard,) = [i for i in runner.instances("existing-ducklake", cases()["SEC-DQ-012"], [])
               if i.name == "existing-ducklake/sqlite[by=hard_link]"]  # fmt: skip
    execution = runner.execute("SEC-DQ-012", cases()["SEC-DQ-012"], hard, set())
    assert (execution.status, execution.protection) == ("not-reproduced", "not-demonstrated")
    assert "hard link" in execution.reason


# --- AC3: excuses checked --------------------------------------------------------------------


def test_a_case_without_its_surface_and_without_pending_fails() -> None:
    assert status(planted("SEC-PAGES-001", pending=None)) == "fail"


def test_a_pending_marker_that_is_not_a_later_row_fails() -> None:
    for pending in ("P3-01", "P9-99"):
        assert status(planted("SEC-ROWS-009", pending=pending)) == "fail", pending


# --- AC1, AC6: records, executions, versions -------------------------------------------------


def test_a_case_the_run_cannot_place_is_a_failed_record_of_its_own() -> None:
    planted_cases = {
        "SEC-X-1": planted("SEC-ROWS-001", id="SEC-X-2"),
        "SEC-ROWS-001": planted("SEC-ROWS-001", engines=["managed", "managed"]),
        "SEC-ROWS-002": planted("SEC-ROWS-002", engines=["managed", "warehouse"]),
    }
    records = runner.run(planted_cases, ["managed"], [], [], set())
    assert [(r.case, r.status) for r in records] == [
        ("SEC-ROWS-001", "fail"),
        ("SEC-ROWS-002", "fail"),
        ("SEC-X-1", "fail"),
    ]


def test_a_required_clickhouse_version_that_did_not_run_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "execute", lambda _n, _c, i, _p: runner.Execution(i.name, "pass"))
    case = planted("SEC-DQ-017")
    server = ClickHouseServer("localhost", 1, "nobody", "")
    one = runner.run(
        {"SEC-DQ-017": case}, ["clickhouse"], [("26.10.2.103", server)], ["26.10", "26.8"]
    )
    (record,) = one
    assert record.status == "EXECUTION_MISSING"
    assert [e.instance for e in record.executions] == ["clickhouse/26.10.2.103", "clickhouse/26.8"]
    none = runner.run({"SEC-DQ-017": case}, ["clickhouse"], [], [])
    assert none[0].status == "EXECUTION_MISSING"
    both = [("26.10.2.103", server), ("26.8.30.8", server)]
    assert (
        runner.run({"SEC-DQ-017": case}, ["clickhouse"], both, ["26.10", "26.8"])[0].status
        == "pass"
    )


def test_the_exit_status_and_the_report_tell_records_from_executions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runner, "cases", lambda: {"SEC-ROWS-001": cases()["SEC-ROWS-001"]})
    written = tmp_path / "report.json"
    lines: list[str] = []
    assert runner.main(["--engine", "managed", "--json", str(written)], lines.append) == 0
    document = json.loads(written.read_text("utf-8"))
    assert (document["kind"], document["version"]) == ("security_scenarios_report", 1)
    assert (document["logical_records"], document["executions"]) == (1, 1)
    assert document["statuses"] == {"pass": 1}
    assert document["protection"] == {"proven": 1}
    monkeypatch.setattr(
        runner, "cases", lambda: {"SEC-PAGES-001": planted("SEC-PAGES-001", pending=None)}
    )
    assert runner.main(["--engine", "managed"], lines.append) == 1


# --- the report holds no data value ----------------------------------------------------------


def test_no_forbidden_value_is_in_the_report_even_when_one_leaks() -> None:
    leaking = planted("SEC-ROWS-001")
    leaking["expect"] = {**leaking["expect"], "forbidden": ["30.75", "Z1"]}
    wrong = planted("SEC-ROWS-001")
    wrong["expect"] = with_rows(wrong["expect"], "readings.zone_id,total_kwh\nZ9,1.00\n")
    selected = {"SEC-ROWS-001": leaking, "SEC-ROWS-002": {**wrong, "id": "SEC-ROWS-002"}}
    text = json.dumps(runner.report(runner.run(selected, ["managed"], [], [], set())))
    for value in ("30.75", "Z1", "Z9", "1.00", "30.00", "40.75"):
        assert value not in text, value


# --- AC4: the HTTP revocations ---------------------------------------------------------------


def test_the_http_revocations_give_the_same_report_twenty_times() -> None:
    selected = {n: cases()[n] for n in ("SEC-REVOKE-001", "SEC-REVOKE-002")}
    first = runner.report(runner.run(selected, ["managed"], [], [], set()))
    assert first["statuses"] == {"pass": 2}
    for _ in range(19):
        assert runner.report(runner.run(selected, ["managed"], [], [], set())) == first


def test_the_server_computes_the_context_each_revocation_case_describes() -> None:
    for name in ("SEC-REVOKE-001", "SEC-REVOKE-002"):
        case = cases()[name]
        found = sf.fixture(case["fixture"], ce.VENDORS[0])
        callers = [step["caller"] for step in case["request"]["steps"]] + [
            case["control"]["caller"]
        ]
        groups = {g for caller in callers for g in caller["groups"]}
        with sf.managed_lake(found) as lake, tempfile.TemporaryDirectory() as folder:
            instance = security_http.Instance(found, lake, Path(folder), groups)
            provider = ServerContextProvider(instance.store, instance.audit, POLICY, {"lake": "0"})
            for caller in callers:
                instance.caller(caller)
                built = provider.context_for(ServerRequest(caller["user"], instance.project))
                document = built.document()
                assert (document["user"], document["role"]) == (caller["user"], caller["role"])
                assert sorted(document["groups"]) == sorted(caller["groups"]), name
                assert document["attributes"] == caller["attributes"], name
