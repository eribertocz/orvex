"""The call envelope (#486 §2 item 6): what every cross-module call carries, and why a binding
refuses one before the implementation runs.

Every operation of an interface contract takes a `CallEnvelope` as its first argument:

- `contract_name` and `contract_version`, the contract and the `MAJOR.MINOR` version the caller
  speaks (never the module's version or a physical format's version);
- `correlation_id`, propagated end to end;
- `actor`, the effective actor or a verifiable reference to it;
- `project_id` and `revision`, the project and the revision the call acts on;
- `idempotency_key`, required on every operation that changes state.

A field that is absent, not a string or blank, a key missing on an operation that changes
state, or an envelope for another contract is refused with CONTRACT_ENVELOPE_INVALID. A version
the provider cannot serve is refused with CONTRACT_VERSION_UNSUPPORTED: another major version,
or an operation the provider's version or the caller's version does not have. A caller at a
newer minor version is served for the operations the provider has (N against N-1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .interface_errors import ContractCallRefused
from .version import VERSION, Version

if TYPE_CHECKING:
    from .interface import InterfaceContract, Operation

REQUIRED = (
    "contract_name",
    "contract_version",
    "correlation_id",
    "actor",
    "project_id",
    "revision",
)
IDEMPOTENCY_KEY = "idempotency_key"


@dataclass(frozen=True, slots=True)
class CallEnvelope:
    """The envelope of one call; see the module's docstring for each field."""

    contract_name: str
    contract_version: str
    correlation_id: str
    actor: str
    project_id: str
    revision: str
    idempotency_key: str | None = None


def _present(value: object) -> bool:
    return type(value) is str and bool(value.strip())


def envelope_refusal(
    envelope: object, contract: InterfaceContract, operation: Operation, provided: Version
) -> ContractCallRefused | None:
    """Why a call of `operation`, served at version `provided`, is refused; None when it is not."""
    where = {"contract": contract.name, "operation": operation.name}

    def invalid(field: str, text: str) -> ContractCallRefused:
        return ContractCallRefused(
            "CONTRACT_ENVELOPE_INVALID",
            f"{contract.name}.{operation.name}: {text}",
            {**where, "field": field},
        )

    if type(envelope) is not CallEnvelope:
        return invalid("envelope", "a call carries a CallEnvelope as its first argument")
    for field in REQUIRED:
        if not _present(getattr(envelope, field, None)):
            return invalid(field, f"the envelope has no {field}")
    if operation.changes_state and not _present(envelope.idempotency_key):
        return invalid(IDEMPOTENCY_KEY, "an operation that changes state needs an idempotency key")
    if envelope.idempotency_key is not None and type(envelope.idempotency_key) is not str:
        return invalid(IDEMPOTENCY_KEY, "an idempotency key is a string")
    if envelope.contract_name != contract.name:
        return invalid("contract_name", f"the envelope is for {envelope.contract_name!r}")
    match = VERSION.fullmatch(envelope.contract_version)
    if match is None:
        return invalid("contract_version", f"{envelope.contract_version!r} is not MAJOR.MINOR")
    spoken = Version(int(match[1]), int(match[2]))
    since = operation.since or Version(contract.version.major, 0)
    problem = (
        f"the caller speaks {spoken} and the provider serves {provided}"
        if spoken.major != provided.major
        else f"{operation.name} exists from {since}; the provider serves {provided}"
        if not provided.provides(since)
        else f"{operation.name} exists from {since}; the caller speaks {spoken}"
        if not spoken.provides(since)
        else None
    )
    if problem is None:
        return None
    return ContractCallRefused(
        "CONTRACT_VERSION_UNSUPPORTED",
        f"{contract.name}.{operation.name}: {problem}",
        {**where, "requested": str(spoken), "provided": str(provided)},
    )
