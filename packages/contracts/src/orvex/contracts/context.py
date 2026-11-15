"""The call context (#486 §2 item 6): who calls, for which project and revision, under which
correlation id; built once at each entry point and carried, per call, into the envelope.

A context holds real values only: a field that is absent, not a string, blank or a placeholder
(`envelope.PLACEHOLDERS`) is refused when the context is built (CONTRACT_CONTEXT_INVALID), and
the binding refuses the same in every envelope, so a placeholder never reaches an
implementation either way. This file is the only one that builds a `CallEnvelope`
(`tools/checks/envelopes.py`): `CallContext.envelope` for every call, and `planted_envelope` for
a kit's envelope cases, which prove that a binding refuses a broken one.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any

from .envelope import CallEnvelope, real_value
from .interface_errors import ContractCallRefused

if TYPE_CHECKING:
    from .interface import InterfaceContract
    from .version import Version


@dataclass(frozen=True, slots=True)
class CallContext:
    """The caller's side of every envelope: the same for each call of one request or command."""

    correlation_id: str
    actor: str
    project_id: str
    revision: str

    def __post_init__(self) -> None:
        for field in fields(self):
            if not real_value(getattr(self, field.name)):
                raise ContractCallRefused(
                    "CONTRACT_CONTEXT_INVALID",
                    f"a call context needs a real {field.name}, not a missing or placeholder one",
                    {"field": field.name},
                )

    def envelope(
        self,
        contract: InterfaceContract,
        operation: str,
        idempotency_key: str | None = None,
        version: Version | None = None,
    ) -> CallEnvelope:
        """The envelope of one call of `operation`, at `version` (the contract's by default);
        an operation that changes state needs its idempotency key."""
        declared = contract.operation(operation)
        if declared.changes_state and not real_value(idempotency_key):
            raise ContractCallRefused(
                "CONTRACT_ENVELOPE_INVALID",
                f"{contract.name}.{operation} changes state and needs an idempotency key",
                {"contract": contract.name, "operation": operation, "field": "idempotency_key"},
            )
        return CallEnvelope(
            contract_name=contract.name,
            contract_version=str(contract.version if version is None else version),
            correlation_id=self.correlation_id,
            actor=self.actor,
            project_id=self.project_id,
            revision=self.revision,
            idempotency_key=idempotency_key,
        )


def planted_envelope(envelope: CallEnvelope, **changes: Any) -> CallEnvelope:  # noqa: ANN401
    """`envelope` with fields changed to any value, a missing or placeholder one included: what
    a kit sends to prove that a binding refuses it before the implementation runs."""
    return dataclasses.replace(envelope, **changes)
