"""The conformance-kit format and its run (#486 G1, G3): every case, a recorded count, no skips,
idempotency, the envelope cases, and N against N-1 in both directions."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any

import pytest
from orvex.contracts.envelope import CallEnvelope, envelope_refusal
from orvex.contracts.interface import InterfaceContract, Operation
from orvex.contracts.interface_errors import ContractCallRefused, InterfaceContractError
from orvex.contracts.kit import Idempotent, Kit, KitCase
from orvex.contracts.kit_client import KitClient
from orvex.contracts.kit_run import consumers, kit_problems, run_kit, verify_kit
from orvex.contracts.version import Version

from .sample_contract import (
    LEDGER,
    LEDGER_KIT,
    MemoryLedger,
    MemoryLedgerV10,
    Reading,
    observe_readings,
    record_once,
)

V10, V11 = Version(1, 0), Version(1, 1)


def code_of(build: Callable[[], object]) -> str:
    with pytest.raises(InterfaceContractError) as raised:
        build()
    return raised.value.code


def failure(make: Callable[[], object], provided: Version | None = None) -> str:
    with pytest.raises(InterfaceContractError) as raised:
        verify_kit(LEDGER_KIT, make, provided)
    assert raised.value.code == "KIT_FAILED"
    return raised.value.message


class RoundingLedger(MemoryLedger):
    """A planted implementation that fails one case: it drops the amount's scale."""

    def record(self, envelope: CallEnvelope, reading: Reading) -> int:
        normalized = dataclasses.replace(reading, amount=reading.amount.normalize())
        return super().record(envelope, normalized)


class ForgetfulLedger(MemoryLedger):
    """A planted violation: it changes state twice for one idempotency key."""

    def record(self, envelope: CallEnvelope, reading: Reading) -> int:
        unique = dataclasses.replace(envelope, idempotency_key=f"{len(self._readings)}")
        return super().record(unique, reading)


class InertLedger(MemoryLedger):
    """An implementation whose `record` has no effect: its idempotency case proves nothing."""

    def record(self, envelope: CallEnvelope, reading: Reading) -> int:
        return 0


class NewOnlyLedger(MemoryLedger):
    """A planted violation: version 1.1 broke its 1.0 consumers, whose names come back empty."""

    def names(self, envelope: CallEnvelope) -> tuple[str, ...]:
        return super().names(envelope) if envelope.contract_version == "1.1" else ()


class WrongCountV10(MemoryLedgerV10):
    """A planted older provider that answers the newer operation wrongly instead of refusing."""

    def count(self, envelope: CallEnvelope) -> int:
        return -1


class TestRun:
    def test_a_correct_implementation_passes_every_case_both_ways_by_n_and_n_1(self) -> None:
        reports = verify_kit(LEDGER_KIT, MemoryLedger)
        assert [(r.consumer, r.serialized) for r in reports] == [
            (V10, False),
            (V10, True),
            (V11, False),
            (V11, True),
        ]
        for report in reports:
            assert report.contract == "ledger"
            assert report.provided == V11
            expected = [
                c.id for c in LEDGER_KIT.cases if report.consumer.provides(LEDGER_KIT.since(c))
            ]
            assert [r.id for r in report.results] == expected
            assert all(r.passed for r in (*report.results, *report.envelope_results))
        assert len(reports[0].results) == 5
        assert len(reports[2].results) == 6

    def test_a_planted_implementation_failing_one_case_fails_the_kit(self) -> None:
        report = run_kit(LEDGER_KIT, RoundingLedger, serialized=False)
        assert [r.id for r in report.results if not r.passed] == ["record.round-trip-exact"]
        message = failure(RoundingLedger)
        for binding_name in ("in process", "serialized"):
            assert (
                f"KIT_CASE_FAILED: record.round-trip-exact ({binding_name}, consumer 1.1, "
                "provider 1.1)" in message
            )

    def test_each_case_gets_a_fresh_implementation(self) -> None:
        made: list[MemoryLedger] = []

        def make() -> MemoryLedger:
            made.append(MemoryLedger())
            return made[-1]

        report = run_kit(LEDGER_KIT, make, serialized=False)
        assert len(made) == LEDGER_KIT.recorded + len(report.envelope_results)

    def test_a_run_with_one_case_fewer_fails(self) -> None:
        report = run_kit(LEDGER_KIT, MemoryLedger, serialized=False)
        shorter = dataclasses.replace(report, results=report.results[:-1])
        assert kit_problems(LEDGER_KIT, shorter) == [
            "KIT_RUN_INCOMPLETE: count.readings did not run (in process, consumer 1.1, "
            "provider 1.1)"
        ]
        fewer = dataclasses.replace(report, envelope_results=report.envelope_results[:-1])
        assert kit_problems(LEDGER_KIT, fewer)[0].startswith("KIT_RUN_INCOMPLETE")

    def test_a_skip_is_a_failure_since_the_kit_has_no_skips(self) -> None:
        def skipping(ledger: KitClient) -> None:
            pytest.skip("not today")

        cases = (dataclasses.replace(LEDGER_KIT.cases[0], check=skipping), *LEDGER_KIT.cases[1:])
        kit = dataclasses.replace(LEDGER_KIT, cases=cases)
        report = run_kit(kit, MemoryLedger, serialized=False)
        assert [r.passed for r in report.results] == [False, True, True, True, True, True]
        assert "not today" in report.results[0].detail


class TestIdempotency:
    def test_an_operation_that_changes_state_twice_for_one_key_fails(self) -> None:
        report = run_kit(LEDGER_KIT, ForgetfulLedger, serialized=False)
        failed = [r for r in report.results if not r.passed]
        assert [r.id for r in failed] == ["record.idempotent"]
        assert "KIT_IDEMPOTENCY_BROKEN" in failed[0].detail
        assert "KIT_CASE_FAILED: record.idempotent" in failure(ForgetfulLedger)

    def test_a_call_without_effect_proves_nothing_and_fails(self) -> None:
        report = run_kit(LEDGER_KIT, InertLedger, serialized=True)
        idempotent = next(r for r in report.results if r.id == "record.idempotent")
        assert "KIT_IDEMPOTENCY_NO_EFFECT" in idempotent.detail

    def test_each_call_gets_a_fresh_key_and_a_repeat_sends_the_same_envelope(self) -> None:
        sent: list[CallEnvelope] = []

        class Spy(MemoryLedger):
            def record(self, envelope: CallEnvelope, reading: Reading) -> int:
                sent.append(envelope)
                return super().record(envelope, reading)

        run_kit(LEDGER_KIT, Spy, serialized=False)
        # round-trip 0, empty name 1, names.sorted 2-4, idempotent 5-6, count 7-8
        assert len(sent) == 9
        assert sent[5] == sent[6]
        assert len({envelope.idempotency_key for envelope in sent[2:5]}) == 3
        assert all(e.correlation_id.startswith("kit-") and e.actor for e in sent)


class TestEnvelopeCases:
    def test_every_operation_gets_its_envelope_cases(self) -> None:
        report = run_kit(LEDGER_KIT, MemoryLedger, serialized=False)
        ids = {r.id for r in report.envelope_results}
        assert "envelope.record.idempotency_key-missing" in ids
        assert "envelope.names.idempotency_key-missing" not in ids
        assert "envelope.count.correlation_id-blank" in ids
        assert "envelope.readings.major-2" in ids
        assert len(ids) == 4 * 20 + 1
        older = run_kit(LEDGER_KIT, MemoryLedger, serialized=False, consumer=V10)
        assert not any(r.id.startswith("envelope.count.") for r in older.envelope_results)

    def test_a_binding_that_accepts_an_incompatible_major_silently_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def lax(*arguments: Any) -> ContractCallRefused | None:  # noqa: ANN401
            refusal = envelope_refusal(*arguments)
            if refusal is not None and refusal.details.get("requested") == "2.0":
                return None
            return refusal

        monkeypatch.setattr("orvex.contracts.binding.envelope_refusal", lax)
        message = failure(MemoryLedger)
        assert "KIT_ENVELOPE_CASE_FAILED: envelope.names.major-2" in message
        assert "expected CONTRACT_VERSION_UNSUPPORTED" in message

    def test_a_binding_that_accepts_a_call_missing_a_field_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def lax(*arguments: Any) -> ContractCallRefused | None:  # noqa: ANN401
            refusal = envelope_refusal(*arguments)
            if refusal is not None and refusal.details.get("field") == "correlation_id":
                return None
            return refusal

        monkeypatch.setattr("orvex.contracts.binding.envelope_refusal", lax)
        message = failure(MemoryLedger)
        assert "KIT_ENVELOPE_CASE_FAILED: envelope.names.correlation_id-missing" in message
        assert "reached ['names']" in message


class FakingV10(MemoryLedgerV10):
    """A planted older provider that dodges a newer guarantee by faking the version refusal."""

    def names(self, envelope: CallEnvelope) -> tuple[str, ...]:
        if envelope.contract_version == "1.1":
            raise ContractCallRefused("CONTRACT_VERSION_UNSUPPORTED", "faked", {})
        return super().names(envelope)


def newer_names_case(check: Callable[[KitClient], None]) -> Kit:
    case = KitCase("names.newer", "names", "a newer guarantee on an old operation", check, V11)
    return Kit(LEDGER, recorded=7, cases=(*LEDGER_KIT.cases, case))


class TestFakedRefusals:
    def test_a_refusal_faked_by_an_older_provider_is_a_failure(self) -> None:
        kit = newer_names_case(_noop)
        with pytest.raises(InterfaceContractError) as raised:
            verify_kit(kit, FakingV10, provided=V10)
        message = raised.value.message
        assert "KIT_CASE_FAILED: names.newer (in process, consumer 1.1, provider 1.0)" in message
        assert "INTERFACE_ERROR_UNDECLARED" in message

    def test_a_refusal_down_the_cause_chain_is_not_a_refusal(self) -> None:
        def wrapped(ledger: KitClient) -> None:
            try:
                ledger.count()
            except ContractCallRefused as error:
                raise AssertionError("the case swallowed the refusal") from error

        kit = newer_names_case(wrapped)
        report = run_kit(kit, MemoryLedgerV10, serialized=False, provided=V10)
        assert next(r for r in report.results if r.id == "names.newer").outcome == "failed"


class TestVersions:
    def test_consumers_are_n_1_n_and_the_newest(self) -> None:
        assert consumers(LEDGER, V11) == (V10, V11)
        assert consumers(LEDGER, V10) == (V10, V11)
        grown = dataclasses.replace(LEDGER, version=Version(1, 2))
        assert consumers(grown, V10) == (V10, Version(1, 2))
        assert consumers(grown, V11) == (V10, V11, Version(1, 2))

    def test_a_provider_at_n_1_passes_the_kit_of_n(self) -> None:
        reports = verify_kit(LEDGER_KIT, MemoryLedgerV10, provided=V10)
        newest = [r for r in reports if r.consumer == V11]
        for report in newest:
            outcome = {r.id: r.outcome for r in report.results}
            assert outcome.pop("count.readings") == "refused"
            assert set(outcome.values()) == {"passed"}

    def test_a_consumer_at_n_1_broken_by_an_additive_change_fails(self) -> None:
        message = failure(NewOnlyLedger)
        assert "KIT_CASE_FAILED: names.sorted (in process, consumer 1.0, provider 1.1)" in message
        assert "consumer 1.1" not in message

    def test_a_provider_at_n_1_that_answers_a_newer_operation_is_still_refused(self) -> None:
        reports = verify_kit(LEDGER_KIT, WrongCountV10, provided=V10)
        newest = next(r for r in reports if r.consumer == V11)
        assert next(r for r in newest.results if r.id == "count.readings").outcome == "refused"

    def test_a_provider_at_n_1_with_a_wrong_answer_fails(self) -> None:
        def wrong(ledger: KitClient) -> None:
            if ledger.names() != ("z",):
                raise AssertionError("not z")

        kit = newer_names_case(wrong)
        with pytest.raises(InterfaceContractError) as raised:
            verify_kit(kit, MemoryLedgerV10, provided=V10)
        message = raised.value.message
        assert "KIT_CASE_FAILED: names.newer (in process, consumer 1.1, provider 1.0)" in message

    def test_a_version_the_contract_does_not_describe_cannot_run(self) -> None:
        newer = Version(2, 0)
        assert code_of(
            lambda: run_kit(LEDGER_KIT, MemoryLedger, serialized=False, consumer=newer)
        ) == ("KIT_INVALID")
        assert code_of(lambda: verify_kit(LEDGER_KIT, MemoryLedger, Version(1, 2))) == (
            "CONTRACT_VERSION_UNSUPPORTED"
        )


def test_the_description_lists_every_case_and_the_recorded_count() -> None:
    described = LEDGER_KIT.describe()
    assert described["recorded"] == 6
    cases = described["cases"]
    assert isinstance(cases, dict)
    assert list(cases) == [case.id for case in LEDGER_KIT.cases]
    assert cases["record.idempotent"] == {
        "operation": "record",
        "expectation": LEDGER_KIT.cases[4].expectation,
        "idempotency": True,
        "since": "1.0",
    }
    assert cases["count.readings"]["since"] == "1.1"
    assert cases["names.sorted"]["idempotency"] is False


def _noop(ledger: KitClient) -> None:
    ledger.names()


CASE = KitCase("names.extra", "names", "an extra case", _noop)


class TestDeclarationRefusals:
    def test_a_recorded_count_that_differs_from_the_cases_is_refused(self) -> None:
        for recorded in (5, 7):
            with pytest.raises(InterfaceContractError) as raised:
                dataclasses.replace(LEDGER_KIT, recorded=recorded)
            assert raised.value.code == "KIT_CASE_COUNT_CHANGED"

    def test_an_operation_without_a_case_is_refused(self) -> None:
        cases = tuple(case for case in LEDGER_KIT.cases if case.operation != "names")
        assert code_of(lambda: Kit(LEDGER, recorded=len(cases), cases=cases)) == (
            "KIT_OPERATION_UNCOVERED"
        )

    def test_an_operation_that_changes_state_without_an_idempotency_case_is_refused(
        self,
    ) -> None:
        cases = tuple(case for case in LEDGER_KIT.cases if case.id != "record.idempotent")
        assert code_of(lambda: Kit(LEDGER, recorded=len(cases), cases=cases)) == (
            "KIT_IDEMPOTENCY_UNCOVERED"
        )

    @pytest.mark.parametrize(
        "case",
        [
            dataclasses.replace(CASE, id="record.round-trip-exact"),
            dataclasses.replace(CASE, id="Bad Id"),
            dataclasses.replace(CASE, operation="drop"),
            dataclasses.replace(CASE, expectation=" "),
            dataclasses.replace(CASE, since=Version(1, 2)),
            dataclasses.replace(CASE, since=Version(2, 0)),
            dataclasses.replace(CASE, operation="count", since=V10),
            dataclasses.replace(CASE, check=Idempotent(record_once, observe_readings)),
        ],
        ids=[
            "duplicate",
            "id",
            "operation",
            "expectation",
            "since-newer",
            "since-major",
            "before-operation",
            "idempotency-on-read",
        ],
    )
    def test_an_invalid_case_is_refused(self, case: KitCase) -> None:
        cases = (*LEDGER_KIT.cases, case)
        assert code_of(lambda: Kit(LEDGER, recorded=len(cases), cases=cases)) == "KIT_INVALID"

    def test_an_extra_valid_case_is_accepted(self) -> None:
        cases = (*LEDGER_KIT.cases, CASE)
        assert Kit(LEDGER, recorded=7, cases=cases).recorded == 7


def test_a_contract_of_one_read_needs_no_idempotency_case() -> None:
    read_only = InterfaceContract(
        name="names",
        version=V10,
        protocol=type(
            "Names",
            (),
            {"names": LEDGER.protocol.names},  # type: ignore[attr-defined]
        ),
        error_type=LEDGER.error_type,
        capabilities=("read",),
        errors=(),
        operations=(Operation("names", "read"),),
    )
    assert Kit(read_only, recorded=1, cases=(CASE,)).recorded == 1
