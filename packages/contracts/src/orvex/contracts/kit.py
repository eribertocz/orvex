"""The conformance-kit format (#486 G1): the cases every implementation of a contract must pass,
with their recorded count and no skip list.

A kit belongs to one interface contract. Each case has a stable id, the operation it exercises,
its expectation in words (the guarantee the compatibility check holds to, #486 G3), the version
it exists from, and either a check that receives a `KitClient` and raises when the guarantee
fails, or an `Idempotent` pair for an operation that changes state: the call is sent twice with
one idempotency key, and the effect must happen exactly once (#486 G3). The kit records how
many cases it has; a kit whose cases differ from that count, that leaves an operation without a
case, or an operation that changes state without an idempotency case, is refused when built.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from .envelope import CallEnvelope
from .interface import InterfaceContract
from .interface_errors import InterfaceContractError
from .kit_client import KitClient
from .version import Version

CASE_ID = re.compile(r"[a-z][a-z0-9]*([.-][a-z0-9]+)*")


@dataclass(frozen=True, slots=True)
class Idempotent:
    """An idempotency check: `call` sends the operation with the envelope it is given, and
    `observe` reads the effect through the contract's other operations."""

    call: Callable[[KitClient, CallEnvelope], object]
    observe: Callable[[KitClient], object]


@dataclass(frozen=True, slots=True)
class KitCase:
    id: str
    operation: str
    expectation: str
    check: Callable[[KitClient], None] | Idempotent
    since: Version | None = None


@dataclass(frozen=True, slots=True)
class Kit:
    contract: InterfaceContract
    recorded: int
    cases: tuple[KitCase, ...]

    def __post_init__(self) -> None:
        name = self.contract.name
        operations = {operation.name for operation in self.contract.operations}
        seen: set[str] = set()
        for case in self.cases:
            problem = (_case_problem(self, case) if case.operation in operations else None) or (
                "its id is not lowercase, dotted or kebab-case"
                if not CASE_ID.fullmatch(case.id)
                else "its id is used twice"
                if case.id in seen
                else f"operation {case.operation!r} is not the contract's"
                if case.operation not in operations
                else "it states no expectation"
                if not case.expectation.strip()
                else None
            )
            if problem is not None:
                raise InterfaceContractError(
                    "KIT_INVALID", f"kit {name}, case {case.id!r}: {problem}"
                )
            seen.add(case.id)
        uncovered = sorted(operations - {case.operation for case in self.cases})
        if uncovered:
            raise InterfaceContractError(
                "KIT_OPERATION_UNCOVERED", f"kit {name}: operations {uncovered} have no case"
            )
        idempotent = {c.operation for c in self.cases if isinstance(c.check, Idempotent)}
        unchecked = sorted(
            o.name for o in self.contract.operations if o.changes_state and o.name not in idempotent
        )
        if unchecked:
            raise InterfaceContractError(
                "KIT_IDEMPOTENCY_UNCOVERED",
                f"kit {name}: operations {unchecked} change state and have no idempotency case",
            )
        if len(self.cases) != self.recorded:
            raise InterfaceContractError(
                "KIT_CASE_COUNT_CHANGED",
                f"kit {name} records {self.recorded} cases and has {len(self.cases)}",
            )

    def since(self, case: KitCase) -> Version:
        """The version `case` exists from."""
        return case.since or Version(self.contract.version.major, 0)

    def describe(self) -> dict[str, object]:
        """The kit as plain data: what the compatibility check compares (#486 G3)."""
        cases = {
            case.id: {
                "operation": case.operation,
                "expectation": case.expectation,
                "idempotency": isinstance(case.check, Idempotent),
                "since": str(self.since(case)),
            }
            for case in self.cases
        }
        return {"recorded": self.recorded, "cases": cases}


def _case_problem(kit: Kit, case: KitCase) -> str | None:
    """What is wrong with a case of a declared operation: its version or its idempotency."""
    contract = kit.contract
    operation = contract.operation(case.operation)
    since = kit.since(case)
    if not (contract.version.provides(since) and since >= contract.since(operation)):
        return f"since {since} is not between {contract.since(operation)} and {contract.version}"
    if isinstance(case.check, Idempotent) and not operation.changes_state:
        return "an idempotency case needs an operation that changes state"
    return None
