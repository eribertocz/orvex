"""Running one kit case on a fresh implementation (#486 G1, G3): through one binding, by a
consumer at one version, against a provider at one version, with its outcome.

A case passes, fails, or is refused: refused only when the binding itself raised
CONTRACT_VERSION_UNSUPPORTED at the top of the case, never when a refusal is found down a
`__cause__` chain or was raised by an implementation (which the binding turns into
INTERFACE_ERROR_UNDECLARED). A skip is a failure: the kit has no skips. An idempotency case
sends its call twice with one key and requires the effect exactly once.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from .binding import Bound
from .interface_errors import ContractCallRefused
from .kit import Idempotent, Kit, KitCase
from .kit_client import KitClient
from .version import Version

Outcome = Literal["passed", "refused", "failed"]
UNSUPPORTED = "CONTRACT_VERSION_UNSUPPORTED"


@dataclass(frozen=True, slots=True)
class CaseResult:
    id: str
    outcome: Outcome
    detail: str

    @property
    def passed(self) -> bool:
        return self.outcome == "passed"


def _refused(error: BaseException) -> bool:
    """A refusal by the binding itself, at the top of the case."""
    return type(error) is ContractCallRefused and error.code == UNSUPPORTED


def _idempotent(client: KitClient, case: KitCase, check: Idempotent) -> None:
    before = check.observe(client)
    envelope = client.envelope(case.operation)
    first = check.call(client, envelope)
    once = check.observe(client)
    second = check.call(client, envelope)
    twice = check.observe(client)
    if once == before:
        raise AssertionError("KIT_IDEMPOTENCY_NO_EFFECT: the first call changed nothing")
    if twice != once:
        raise AssertionError(
            "KIT_IDEMPOTENCY_BROKEN: one idempotency key had its effect twice: "
            f"{once!r} became {twice!r}"
        )
    if second != first:
        raise AssertionError(
            f"KIT_IDEMPOTENCY_BROKEN: the repeated call answered {second!r}, first {first!r}"
        )


def _outcome(run: Callable[[], None]) -> tuple[Outcome, str]:
    try:
        run()
    except KeyboardInterrupt, SystemExit:
        raise
    except BaseException as error:  # noqa: BLE001 - a failure or a skip: the kit has no skips
        outcome: Outcome = "refused" if _refused(error) else "failed"
        return outcome, f"{type(error).__name__}: {error}"
    return "passed", ""


def run_case(
    kit: Kit,
    case: KitCase,
    make: Callable[[], object],
    serialized: bool,
    consumer: Version,
    provided: Version,
) -> CaseResult:
    """One case, here, on a fresh implementation."""
    bound = Bound(kit.contract, make(), serialized=serialized, provided=provided)
    client = KitClient(bound, consumer, case.id)
    check = case.check
    if isinstance(check, Idempotent):
        outcome, detail = _outcome(functools.partial(_idempotent, client, case, check))
    else:
        outcome, detail = _outcome(functools.partial(check, client))
    return CaseResult(case.id, outcome, detail)
