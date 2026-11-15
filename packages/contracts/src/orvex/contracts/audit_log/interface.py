"""The interface contract of the audit log (#486 §5 step 4, #437): record an event, read the
events, verify the chain and its signatures, retain, rotate the signing key.

Its implementation serves the instances the `access` contract opens (`orvex.contracts.access`),
and opens them too, so the audit log can be used and checked on its own. Each operation takes
the call envelope first; the envelope's `actor` is the event's actor. `record` writes one event
with no change of its own: when it cannot commit, `AUDIT_UNAVAILABLE`, and what it would have
recorded must not happen. `refusal` records a refusal and never raises for its own failure: it
is counted (`unrecorded`). `events` returns each event's `audit-event` v1 document as canonical
JSON text, in order.
"""

from __future__ import annotations

from typing import Protocol

from orvex.contracts.access.errors import AccessError
from orvex.contracts.access.values import Configuration, Instance, Outcome
from orvex.contracts.envelope import CallEnvelope
from orvex.contracts.interface import InterfaceContract, Operation
from orvex.contracts.version import Version

# Retention keeps at least this many days (#437): a shorter one is AUDIT_RETENTION_INVALID.
MINIMUM_RETENTION_DAYS = 30


class AuditLog(Protocol):
    def open(self, envelope: CallEnvelope, configuration: Configuration) -> Instance: ...

    def close(self, envelope: CallEnvelope, instance: Instance) -> None: ...

    def record(
        self,
        envelope: CallEnvelope,
        instance: Instance,
        kind: str,
        details: dict[str, str],
        outcome: Outcome,
        project: str | None,
    ) -> str: ...

    def refusal(
        self,
        envelope: CallEnvelope,
        instance: Instance,
        operation: str,
        code: str,
        project: str | None,
        target: str | None,
    ) -> None: ...

    def count_unrecorded(self, envelope: CallEnvelope, instance: Instance) -> int: ...

    def unrecorded(self, envelope: CallEnvelope, instance: Instance) -> int: ...

    def events(self, envelope: CallEnvelope, instance: Instance) -> tuple[str, ...]: ...

    def verify(
        self, envelope: CallEnvelope, instance: Instance, anchors: tuple[str, ...]
    ) -> int: ...

    def verify_at_start(self, envelope: CallEnvelope, instance: Instance) -> int: ...

    def retain(
        self, envelope: CallEnvelope, instance: Instance, retention_days: int
    ) -> tuple[int, int] | None: ...

    def rotate_key(self, envelope: CallEnvelope, instance: Instance) -> str: ...


_INSTANCE = ("ACCESS_INSTANCE_UNKNOWN",)
_WRITE = (*_INSTANCE, "AUDIT_UNAVAILABLE", "AUDIT_EVENT_INVALID", "AUDIT_KEY_UNSAFE")
_VERIFY = (*_INSTANCE, "AUDIT_CHAIN_BROKEN", "AUDIT_CHECKPOINT_INVALID", "AUDIT_KEY_UNSAFE")
_OPEN = (
    *_WRITE,
    "ACCESS_STORE_NEWER",
    "ACCESS_STORE_UNSAFE",
    "VALIDATION_FAILED",
    "OIDC_NOT_CONFIGURED",
)

AUDIT_LOG = InterfaceContract(
    name="audit-log",
    version=Version(1, 0),
    protocol=AuditLog,
    error_type=AccessError,
    capabilities=("instance", "record", "verify", "maintain"),
    errors=(
        *_OPEN,
        "AUDIT_CHAIN_BROKEN",
        "AUDIT_CHECKPOINT_INVALID",
        "AUDIT_RETENTION_INVALID",
    ),
    operations=(
        Operation("open", "instance", _OPEN, changes_state=True),
        Operation("close", "instance", _INSTANCE, changes_state=True),
        Operation("record", "record", _WRITE, changes_state=True),
        Operation("refusal", "record", _INSTANCE, changes_state=True),
        Operation("count_unrecorded", "record", _INSTANCE, changes_state=True),
        Operation("unrecorded", "record", _INSTANCE),
        Operation("events", "verify", _VERIFY),
        Operation("verify", "verify", _VERIFY),
        Operation("verify_at_start", "verify", _VERIFY),
        Operation(
            "retain",
            "maintain",
            (*_WRITE, *_VERIFY, "AUDIT_RETENTION_INVALID"),
            changes_state=True,
        ),
        Operation("rotate_key", "maintain", (*_WRITE, *_VERIFY), changes_state=True),
    ),
)
