"""The call envelope (#486 §2 item 6, G3): every binding refuses a call missing a required field,
or at a version the provider cannot serve, before the implementation runs."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from orvex.contracts.binding import Bound
from orvex.contracts.envelope import REQUIRED, CallEnvelope
from orvex.contracts.interface_errors import ContractCallRefused
from orvex.contracts.version import Version

from .sample_contract import ENVELOPE, LEDGER, READING, MemoryLedger, MemoryLedgerV10, Reading

BINDINGS = pytest.mark.parametrize("serialized", [False, True], ids=["in-process", "serialized"])


class Untouchable:
    """An implementation that fails the test if any operation of it is reached."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"the implementation was reached: {name}")


def refused(
    envelope: object,
    operation: str = "names",
    provided: Version | None = None,
    *,
    serialized: bool,
) -> ContractCallRefused:
    bound = Bound(LEDGER, Untouchable(), serialized=serialized, provided=provided)
    arguments: tuple[object, ...] = (READING,) if operation == "record" else ()
    with pytest.raises(ContractCallRefused) as raised:
        bound.call(operation, envelope, *arguments)
    return raised.value


@BINDINGS
@pytest.mark.parametrize("field", REQUIRED)
@pytest.mark.parametrize("value", [None, "", "  ", 7], ids=["absent", "empty", "blank", "int"])
def test_a_call_missing_a_required_field_is_refused(
    serialized: bool, field: str, value: object
) -> None:
    planted: dict[str, Any] = {field: value}
    envelope = dataclasses.replace(ENVELOPE, **planted)
    error = refused(envelope, serialized=serialized)
    assert error.code == "CONTRACT_ENVELOPE_INVALID"
    assert error.details == {"contract": "ledger", "operation": "names", "field": field}


@BINDINGS
@pytest.mark.parametrize("key", [None, "", " "])
def test_an_operation_that_changes_state_needs_an_idempotency_key(
    serialized: bool, key: str | None
) -> None:
    envelope = dataclasses.replace(ENVELOPE, idempotency_key=key)
    error = refused(envelope, "record", serialized=serialized)
    assert error.code == "CONTRACT_ENVELOPE_INVALID"
    assert error.details["field"] == "idempotency_key"
    names = Bound(LEDGER, MemoryLedger(), serialized=serialized).names(envelope)
    assert names == ()


@BINDINGS
@pytest.mark.parametrize(
    "envelope",
    [None, "ledger", {"contract_name": "ledger"}, dataclasses.astuple(ENVELOPE)],
    ids=["none", "text", "dict", "tuple"],
)
def test_anything_but_an_envelope_is_refused(serialized: bool, envelope: object) -> None:
    error = refused(envelope, serialized=serialized)
    assert error.code == "CONTRACT_ENVELOPE_INVALID"
    assert error.details["field"] == "envelope"


@BINDINGS
def test_an_envelope_for_another_contract_is_refused(serialized: bool) -> None:
    error = refused(dataclasses.replace(ENVELOPE, contract_name="store"), serialized=serialized)
    assert error.code == "CONTRACT_ENVELOPE_INVALID"
    assert error.details["field"] == "contract_name"


@BINDINGS
@pytest.mark.parametrize("version", ["1", "1.0.0", "v1.1", "01.1"])
def test_a_version_that_is_not_major_minor_is_refused(serialized: bool, version: str) -> None:
    error = refused(dataclasses.replace(ENVELOPE, contract_version=version), serialized=serialized)
    assert error.code == "CONTRACT_ENVELOPE_INVALID"
    assert error.details["field"] == "contract_version"


@BINDINGS
@pytest.mark.parametrize("version", ["2.0", "2.1", "3.4"])
def test_an_incompatible_major_version_is_refused_explicitly(
    serialized: bool, version: str
) -> None:
    error = refused(dataclasses.replace(ENVELOPE, contract_version=version), serialized=serialized)
    assert error.code == "CONTRACT_VERSION_UNSUPPORTED"
    assert error.details["requested"] == version
    assert error.details["provided"] == "1.1"


@BINDINGS
def test_an_operation_newer_than_the_provider_is_refused(serialized: bool) -> None:
    error = refused(ENVELOPE, "count", Version(1, 0), serialized=serialized)
    assert error.code == "CONTRACT_VERSION_UNSUPPORTED"
    assert "count exists from 1.1; the provider serves 1.0" in error.message


@BINDINGS
def test_an_operation_newer_than_the_caller_s_version_is_refused(serialized: bool) -> None:
    older = dataclasses.replace(ENVELOPE, contract_version="1.0")
    error = refused(older, "count", serialized=serialized)
    assert error.code == "CONTRACT_VERSION_UNSUPPORTED"
    assert "the caller speaks 1.0" in error.message


@BINDINGS
def test_a_newer_caller_is_served_by_an_older_provider_for_what_it_has(serialized: bool) -> None:
    bound = Bound(LEDGER, MemoryLedgerV10(), serialized=serialized, provided=Version(1, 0))
    assert bound.record(ENVELOPE, READING) == 1
    assert bound.readings(ENVELOPE, READING.name) == (READING,)


@BINDINGS
def test_an_older_caller_is_served_by_a_newer_provider(serialized: bool) -> None:
    older = dataclasses.replace(ENVELOPE, contract_version="1.0")
    bound = Bound(LEDGER, MemoryLedger(), serialized=serialized)
    assert bound.record(older, Reading("a", READING.amount, READING.at)) == 1
    assert bound.names(older) == ("a",)


@pytest.mark.parametrize("provided", [Version(1, 2), Version(2, 0)])
def test_a_binding_never_serves_a_version_the_contract_does_not_describe(
    provided: Version,
) -> None:
    with pytest.raises(ContractCallRefused) as raised:
        Bound(LEDGER, MemoryLedger(), serialized=False, provided=provided)
    assert raised.value.code == "CONTRACT_VERSION_UNSUPPORTED"


def test_the_envelope_holds_the_required_fields_and_the_key() -> None:
    assert dataclasses.fields(CallEnvelope)[-1].name == "idempotency_key"
    assert [f.name for f in dataclasses.fields(CallEnvelope)][:-1] == list(REQUIRED)
