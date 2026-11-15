"""The server's calls to transform go through its contract with a complete envelope, and fail
closed without one (#486 §2 item 6)."""

from __future__ import annotations

import pytest
from orvex.contracts.interface_errors import ContractCallRefused
from orvex.server.transform_calls import ACTOR, REVISION, calling, envelope, transform


def test_a_call_outside_a_project_is_refused_before_transform_runs() -> None:
    with pytest.raises(ContractCallRefused) as raised:
        transform().lineage_project_key(envelope(), "Meters")
    assert raised.value.code == "CONTRACT_ENVELOPE_INVALID"
    assert raised.value.details["field"] == "correlation_id"


def test_a_call_inside_a_project_carries_its_envelope() -> None:
    with calling("meters"):
        sent = envelope()
        assert transform().lineage_project_key(envelope(), "Meters") == "meters"
        with calling("other"):
            assert envelope().project_id == "other"
        assert envelope() == sent
    assert (sent.project_id, sent.actor, sent.revision) == ("meters", ACTOR, REVISION)
    assert (sent.contract_name, sent.contract_version) == ("transform", "1.0")
    assert sent.correlation_id
    assert envelope().project_id == ""


def test_only_the_contract_operations_can_be_called() -> None:
    with pytest.raises(AttributeError):
        getattr(transform(), "run_everything")  # noqa: B009
