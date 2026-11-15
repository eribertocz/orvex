"""Running a conformance kit (#486 G1, G3, G5): every case, each on a fresh implementation, in
process and serialized, by consumers at N-1 and N, with nothing skipped.

A case that declares its own environment runs in its own process built from it
(`kit_process`); every other case runs here (`kit_case`). A run serves the implementation at
the version it provides (`provided`) to a consumer at version `consumer`, which runs the cases
that exist at its version. A case that exists at the
provided version must pass. A newer case (a consumer at N against a provider at N-1) must pass
or be refused with CONTRACT_VERSION_UNSUPPORTED: never a wrong answer. Each run also sends the
envelope cases the contract implies, which no implementation can pass by itself: a call whose
envelope field is absent, blank or a placeholder, or that lacks its idempotency key where it
changes state, is refused with CONTRACT_ENVELOPE_INVALID, and a call at another major version
with CONTRACT_VERSION_UNSUPPORTED, both before the implementation is reached.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass

from .binding import Bound
from .envelope import IDEMPOTENCY_KEY, REQUIRED
from .interface import InterfaceContract, Operation
from .interface_errors import InterfaceContractError
from .kit import Kit
from .kit_case import UNSUPPORTED, CaseResult, run_case
from .kit_client import KitClient
from .kit_process import run_case_in_its_own_process
from .version import Version


@dataclass(frozen=True, slots=True)
class KitReport:
    contract: str
    serialized: bool
    consumer: Version
    provided: Version
    results: tuple[CaseResult, ...]
    envelope_results: tuple[CaseResult, ...]


class _Reached:
    """Wraps an implementation and records whether any operation of it was reached."""

    def __init__(self, implementation: object) -> None:
        self.implementation = implementation
        self.reached: list[str] = []

    def __getattr__(self, name: str) -> object:
        self.reached.append(name)
        return getattr(self.implementation, name)


def binding_name(report: KitReport) -> str:
    binding = "serialized" if report.serialized else "in process"
    return f"{binding}, consumer {report.consumer}, provider {report.provided}"


def _envelope_changes(
    contract: InterfaceContract, operation: Operation, consumer: Version
) -> Iterator[tuple[str, str, dict[str, str | None]]]:
    """Each envelope case of one operation: its name, the code it expects, the change made."""
    for field in REQUIRED:
        yield f"{field}-missing", "CONTRACT_ENVELOPE_INVALID", {field: None}
        yield f"{field}-blank", "CONTRACT_ENVELOPE_INVALID", {field: " "}
        yield f"{field}-placeholder", "CONTRACT_ENVELOPE_INVALID", {field: "unknown"}
    if operation.changes_state:
        yield f"{IDEMPOTENCY_KEY}-missing", "CONTRACT_ENVELOPE_INVALID", {IDEMPOTENCY_KEY: None}
    yield "other-contract", "CONTRACT_ENVELOPE_INVALID", {"contract_name": f"{contract.name}-x"}
    for major in (consumer.major + 1, consumer.major - 1):
        if major >= 1:
            yield f"major-{major}", UNSUPPORTED, {"contract_version": f"{major}.0"}


def _envelope_cases(
    kit: Kit, consumer: Version
) -> Iterator[tuple[Operation, str, str, dict[str, str | None]]]:
    """Each envelope case a consumer at `consumer` sends: its operation, id, code and change."""
    for operation in kit.contract.operations:
        if consumer.provides(kit.contract.since(operation)):
            for name, expected, change in _envelope_changes(kit.contract, operation, consumer):
                yield operation, f"envelope.{operation.name}.{name}", expected, change


def _envelope_results(
    kit: Kit, make: Callable[[], object], serialized: bool, consumer: Version, provided: Version
) -> list[CaseResult]:
    results = []
    for operation, case_id, expected, change in _envelope_cases(kit, consumer):
        spy = _Reached(make())
        bound = Bound(kit.contract, spy, serialized=serialized, provided=provided)
        client = KitClient(bound, consumer, case_id)
        envelope = client.envelope(operation.name, **change)
        try:
            client.send(operation.name, envelope)
        except InterfaceContractError as error:
            if error.code == expected and not spy.reached:
                results.append(CaseResult(case_id, "passed", ""))
                continue
            detail = f"{error.code}, reached {spy.reached}"
        except Exception as error:  # noqa: BLE001 - anything but the refusal fails the case
            detail = f"{type(error).__name__}: {error}, reached {spy.reached}"
        else:
            detail = f"accepted, reached {spy.reached}"
        results.append(CaseResult(case_id, "failed", f"expected {expected}; {detail}"))
    return results


def run_kit(
    kit: Kit,
    make: Callable[[], object],
    *,
    serialized: bool,
    consumer: Version | None = None,
    provided: Version | None = None,
) -> KitReport:
    """One run of the kit: one binding, one consumer version, one provided version."""
    served = kit.contract.version if provided is None else provided
    speaking = kit.contract.version if consumer is None else consumer
    if not kit.contract.version.provides(speaking):
        raise InterfaceContractError(
            "KIT_INVALID", f"kit {kit.contract.name} has no consumer at {speaking}"
        )
    results = []
    for case in kit.cases:
        if not speaking.provides(kit.since(case)):
            continue
        run = run_case if case.environment is None else run_case_in_its_own_process
        results.append(run(kit, case, make, serialized, speaking, served))
    envelope = _envelope_results(kit, make, serialized, speaking, served)
    return KitReport(
        kit.contract.name, serialized, speaking, served, tuple(results), tuple(envelope)
    )


def kit_problems(kit: Kit, report: KitReport) -> list[str]:
    """Every way `report` falls short of the whole kit passing for its consumer and provider."""
    binding = binding_name(report)
    expected = [case for case in kit.cases if report.consumer.provides(kit.since(case))]
    ran = [result.id for result in report.results]
    problems = [
        f"KIT_RUN_INCOMPLETE: {case.id} did not run ({binding})"
        for case in expected
        if case.id not in ran
    ]
    if len(ran) != len(expected) and not problems:
        problems.append(
            f"KIT_RUN_INCOMPLETE: {len(ran)} results for {len(expected)} cases ({binding})"
        )
    since = {case.id: kit.since(case) for case in kit.cases}
    for result in report.results:
        newer = result.id in since and not report.provided.provides(since[result.id])
        if result.outcome == "failed" or (result.outcome == "refused" and not newer):
            problems.append(f"KIT_CASE_FAILED: {result.id} ({binding}): {result.detail}")
    sent = [result.id for result in report.envelope_results]
    problems += [
        f"KIT_RUN_INCOMPLETE: {case_id} did not run ({binding})"
        for _, case_id, _, _ in _envelope_cases(kit, report.consumer)
        if case_id not in sent
    ]
    problems += [
        f"KIT_ENVELOPE_CASE_FAILED: {result.id} ({binding}): {result.detail}"
        for result in report.envelope_results
        if not result.passed
    ]
    return problems


def consumers(contract: InterfaceContract, provided: Version) -> tuple[Version, ...]:
    """The consumer versions a provider at `provided` is run against: N-1, N and the newest."""
    versions = {provided, contract.version}
    previous = provided.previous()
    if previous is not None:
        versions.add(previous)
    return tuple(sorted(versions))


def verify_kit(
    kit: Kit, make: Callable[[], object], provided: Version | None = None
) -> tuple[KitReport, ...]:
    """Run the whole kit in process and serialized, by every consumer of `consumers`; raise
    KIT_FAILED unless every run passes and both bindings give the same results."""
    served = kit.contract.version if provided is None else provided
    reports: list[KitReport] = []
    problems: list[str] = []
    for consumer in consumers(kit.contract, served):
        pair = [
            run_kit(kit, make, serialized=serialized, consumer=consumer, provided=served)
            for serialized in (False, True)
        ]
        outcomes = [
            [(r.id, r.outcome) for r in (*report.results, *report.envelope_results)]
            for report in pair
        ]
        if outcomes[0] != outcomes[1]:
            problems.append(
                f"KIT_BINDINGS_DIFFER: the serialized run differs from the run in process "
                f"(consumer {consumer}, provider {served})"
            )
        for report in pair:
            problems += kit_problems(kit, report)
        reports += pair
    if problems:
        raise InterfaceContractError(
            "KIT_FAILED", "; ".join(problems), {"contract": kit.contract.name}
        )
    return tuple(reports)
