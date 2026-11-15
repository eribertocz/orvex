"""The call context (#486 §2 item 6): real values only, and the one way a consumer makes an
envelope."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from orvex.contracts.binding import Bound
from orvex.contracts.context import CallContext
from orvex.contracts.envelope import PLACEHOLDERS
from orvex.contracts.interface_errors import ContractCallRefused
from orvex.contracts.version import Version

from .sample_contract import LEDGER, READING, MemoryLedger

CONTEXT = CallContext(
    correlation_id="4f1c2a9e",
    actor="user:ana",
    project_id="billing",
    revision="a1b2c3d",
)
FIELDS = [field.name for field in dataclasses.fields(CallContext)]


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("value", [None, "", " ", 1, *sorted(PLACEHOLDERS), " Unknown "])
def test_a_missing_or_placeholder_value_is_refused(field: str, value: object) -> None:
    planted: dict[str, Any] = {field: value}
    with pytest.raises(ContractCallRefused) as raised:
        dataclasses.replace(CONTEXT, **planted)
    assert raised.value.code == "CONTRACT_CONTEXT_INVALID"
    assert raised.value.details == {"field": field}


def test_the_envelope_carries_the_context_and_the_contract_s_version() -> None:
    envelope = CONTEXT.envelope(LEDGER, "names")
    assert envelope.contract_name == "ledger"
    assert envelope.contract_version == "1.1"
    assert (envelope.correlation_id, envelope.actor, envelope.project_id, envelope.revision) == (
        "4f1c2a9e",
        "user:ana",
        "billing",
        "a1b2c3d",
    )
    assert envelope.idempotency_key is None
    assert CONTEXT.envelope(LEDGER, "names", version=Version(1, 0)).contract_version == "1.0"


@pytest.mark.parametrize("key", [None, "", "unknown"])
def test_an_operation_that_changes_state_needs_a_real_key(key: str | None) -> None:
    with pytest.raises(ContractCallRefused) as raised:
        CONTEXT.envelope(LEDGER, "record", key)
    assert raised.value.code == "CONTRACT_ENVELOPE_INVALID"
    assert raised.value.details["field"] == "idempotency_key"


@pytest.mark.parametrize("serialized", [False, True])
def test_an_envelope_from_the_context_is_served(serialized: bool) -> None:
    bound = Bound(LEDGER, MemoryLedger(), serialized=serialized)
    assert bound.record(CONTEXT.envelope(LEDGER, "record", "order-17"), READING) == 1
    assert bound.names(CONTEXT.envelope(LEDGER, "names")) == (READING.name,)
