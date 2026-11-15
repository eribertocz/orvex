"""Running a conformance kit (#486 G1, G5): every case, in process and serialized, each on a fresh
implementation, with nothing skipped and the same results through both bindings."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .binding import Bound
from .interface_errors import InterfaceContractError
from .kit import Kit


@dataclass(frozen=True, slots=True)
class CaseResult:
    id: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class KitReport:
    contract: str
    serialized: bool
    results: tuple[CaseResult, ...]


def binding_name(serialized: bool) -> str:
    return "serialized" if serialized else "in process"


def run_kit(kit: Kit, make: Callable[[], object], *, serialized: bool) -> KitReport:
    results: list[CaseResult] = []
    for case in kit.cases:
        bound = Bound(kit.contract, make(), serialized=serialized)
        try:
            case.check(bound)
        except KeyboardInterrupt, SystemExit:
            raise
        except BaseException as error:  # noqa: BLE001 - a failure or a skip: the kit has no skips
            results.append(CaseResult(case.id, False, f"{type(error).__name__}: {error}"))
        else:
            results.append(CaseResult(case.id, True, ""))
    return KitReport(kit.contract.name, serialized, tuple(results))


def kit_problems(kit: Kit, report: KitReport) -> list[str]:
    """Every way `report` falls short of the whole kit passing."""
    binding = binding_name(report.serialized)
    ran = [result.id for result in report.results]
    problems = [
        f"KIT_RUN_INCOMPLETE: {case.id} did not run ({binding})"
        for case in kit.cases
        if case.id not in ran
    ]
    if len(ran) != kit.recorded and not problems:
        problems.append(
            f"KIT_RUN_INCOMPLETE: {len(ran)} results for {kit.recorded} cases ({binding})"
        )
    problems += [
        f"KIT_CASE_FAILED: {result.id} ({binding}): {result.detail}"
        for result in report.results
        if not result.passed
    ]
    return problems


def verify_kit(kit: Kit, make: Callable[[], object]) -> tuple[KitReport, KitReport]:
    """Run the whole kit in process and serialized; raise KIT_FAILED unless every case passes
    through both bindings with the same results."""
    in_process = run_kit(kit, make, serialized=False)
    serialized = run_kit(kit, make, serialized=True)
    problems = kit_problems(kit, in_process) + kit_problems(kit, serialized)
    outcomes = [[(r.id, r.passed) for r in report.results] for report in (in_process, serialized)]
    if outcomes[0] != outcomes[1]:
        problems.insert(
            0, "KIT_BINDINGS_DIFFER: the serialized run differs from the run in process"
        )
    if problems:
        raise InterfaceContractError(
            "KIT_FAILED", "; ".join(problems), {"contract": kit.contract.name}
        )
    return in_process, serialized
