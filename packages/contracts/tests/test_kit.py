"""The conformance-kit format and its run (#486 G1): every case, a recorded count, no skips."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable

import pytest
from orvex.contracts.binding import Bound
from orvex.contracts.interface_errors import InterfaceContractError
from orvex.contracts.kit import Kit, KitCase
from orvex.contracts.kit_run import kit_problems, run_kit, verify_kit

from .sample_contract import LEDGER, LEDGER_KIT, MemoryLedger, Reading


def code_of(build: Callable[[], object]) -> str:
    with pytest.raises(InterfaceContractError) as raised:
        build()
    return raised.value.code


class RoundingLedger(MemoryLedger):
    """A planted implementation that fails one case: it drops the amount's scale."""

    def put(self, reading: Reading) -> None:
        super().put(dataclasses.replace(reading, amount=reading.amount.normalize()))


def test_a_correct_implementation_passes_every_case_in_both_bindings() -> None:
    in_process, serialized = verify_kit(LEDGER_KIT, MemoryLedger)
    assert not in_process.serialized
    assert serialized.serialized
    for report in (in_process, serialized):
        assert report.contract == "ledger"
        assert [r.id for r in report.results] == [c.id for c in LEDGER_KIT.cases]
        assert all(r.passed for r in report.results)


def test_a_planted_implementation_failing_one_case_fails_the_kit() -> None:
    report = run_kit(LEDGER_KIT, RoundingLedger, serialized=False)
    assert [r.id for r in report.results if not r.passed] == ["put.round-trip-exact"]
    with pytest.raises(InterfaceContractError) as raised:
        verify_kit(LEDGER_KIT, RoundingLedger)
    assert raised.value.code == "KIT_FAILED"
    assert "KIT_CASE_FAILED: put.round-trip-exact (in process)" in raised.value.message
    assert "KIT_CASE_FAILED: put.round-trip-exact (serialized)" in raised.value.message


def test_each_case_gets_a_fresh_implementation() -> None:
    made: list[MemoryLedger] = []

    def make() -> MemoryLedger:
        made.append(MemoryLedger())
        return made[-1]

    run_kit(LEDGER_KIT, make, serialized=False)
    assert len(made) == LEDGER_KIT.recorded


def test_a_run_with_one_case_fewer_fails() -> None:
    report = run_kit(LEDGER_KIT, MemoryLedger, serialized=False)
    shorter = dataclasses.replace(report, results=report.results[:-1])
    problems = kit_problems(LEDGER_KIT, shorter)
    assert problems == ["KIT_RUN_INCOMPLETE: names.sorted did not run (in process)"]


def test_a_skip_is_a_failure_since_the_kit_has_no_skips() -> None:
    def skipping(ledger: Bound) -> None:
        pytest.skip("not today")

    cases = (dataclasses.replace(LEDGER_KIT.cases[0], check=skipping), *LEDGER_KIT.cases[1:])
    kit = dataclasses.replace(LEDGER_KIT, cases=cases)
    report = run_kit(kit, MemoryLedger, serialized=False)
    assert [r.passed for r in report.results] == [False, True, True, True]
    assert "not today" in report.results[0].detail


def test_the_description_lists_every_case_and_the_recorded_count() -> None:
    assert LEDGER_KIT.describe() == {
        "recorded": 4,
        "cases": {
            case.id: {"operation": case.operation, "expectation": case.expectation}
            for case in LEDGER_KIT.cases
        },
    }


def _noop(ledger: Bound) -> None:
    ledger.get("x")


CASE = KitCase("get.extra", "get", "an extra case", _noop)


class TestDeclarationRefusals:
    def test_a_recorded_count_that_differs_from_the_cases_is_refused(self) -> None:
        assert code_of(lambda: dataclasses.replace(LEDGER_KIT, recorded=3)) == (
            "KIT_CASE_COUNT_CHANGED"
        )
        assert code_of(lambda: dataclasses.replace(LEDGER_KIT, recorded=5)) == (
            "KIT_CASE_COUNT_CHANGED"
        )

    def test_an_operation_without_a_case_is_refused(self) -> None:
        cases = tuple(case for case in LEDGER_KIT.cases if case.operation != "names")
        assert code_of(lambda: Kit(LEDGER, recorded=len(cases), cases=cases)) == (
            "KIT_OPERATION_UNCOVERED"
        )

    @pytest.mark.parametrize(
        "case",
        [
            dataclasses.replace(CASE, id="put.round-trip-exact"),
            dataclasses.replace(CASE, id="Bad Id"),
            dataclasses.replace(CASE, operation="drop"),
            dataclasses.replace(CASE, expectation=" "),
        ],
        ids=["duplicate", "id", "operation", "expectation"],
    )
    def test_an_invalid_case_is_refused(self, case: KitCase) -> None:
        cases = (*LEDGER_KIT.cases, case)
        assert code_of(lambda: Kit(LEDGER, recorded=len(cases), cases=cases)) == "KIT_INVALID"

    def test_an_extra_valid_case_is_accepted(self) -> None:
        cases = (*LEDGER_KIT.cases, CASE)
        assert Kit(LEDGER, recorded=5, cases=cases).recorded == 5
